import httpx
import pytest

from lead_triage.config import USER_AGENT
from lead_triage.research import MAX_BYTES, FetchError, HttpFetcher, _check_public, extract, research_site

HOME = '<html><body><h1>Acme</h1><a href="/about">About</a><a href="/pricing">Pricing</a>' \
       '<a href="/blog">Blog</a><a href="/careers">Careers</a><a href="https://other.example/about">x</a></body></html>'


def transport(routes: dict, seen: list):
    def handler(req: httpx.Request):
        seen.append((req.url.path, req.headers.get("user-agent")))
        r = routes.get(req.url.path)
        if r is None:
            return httpx.Response(404)
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, text=r, headers={"content-type": "text/html"})
    return httpx.MockTransport(handler)


async def run(routes):
    seen = []
    f = HttpFetcher(transport=transport(routes, seen), resolve=False)
    try:
        return await research_site(f, "https://acme-demo.example"), seen
    finally:
        await f.aclose()


async def test_max_three_pages_home_about_pricing_only():
    r, seen = await run({"/robots.txt": "", "/": HOME, "/about": "<p>about</p>", "/pricing": "<p>price</p>",
                         "/blog": "<p>blog</p>", "/careers": "<p>c</p>"})
    assert [p.url for p in r.pages] == ["https://acme-demo.example/", "https://acme-demo.example/about",
                                        "https://acme-demo.example/pricing"]
    assert "/blog" not in [p for p, _ in seen] and "/careers" not in [p for p, _ in seen]
    assert all(ua == USER_AGENT for _, ua in seen)


async def test_robots_disallow_all_fetches_nothing():
    r, seen = await run({"/robots.txt": "User-agent: *\nDisallow: /\n", "/": HOME})
    assert r.pages == []
    assert [p for p, _ in seen] == ["/robots.txt"]
    assert any("robots.txt" in s for s in r.skipped)


async def test_robots_disallow_one_path():
    r, seen = await run({"/robots.txt": "User-agent: *\nDisallow: /pricing\n", "/": HOME, "/about": "a", "/pricing": "p"})
    assert [p.url.rsplit("/", 1)[-1] for p in r.pages] == ["", "about"]
    assert "/pricing" not in [p for p, _ in seen]


async def test_robots_server_error_means_no_crawl():
    r, seen = await run({"/robots.txt": httpx.Response(503), "/": HOME})
    assert r.pages == [] and [p for p, _ in seen] == ["/robots.txt"]


async def test_size_cap_streamed_body():
    big = "<p>" + "x" * (MAX_BYTES + 10) + "</p>"
    r, _ = await run({"/robots.txt": "", "/": big})
    assert r.pages == []
    assert any("larger than" in s for s in r.skipped)


async def test_size_cap_declared_content_length():
    resp = httpx.Response(200, headers={"content-length": str(MAX_BYTES * 3)}, content=b"<p>small lie</p>")
    r, _ = await run({"/robots.txt": "", "/": resp})
    assert r.pages == [] and any("larger than" in s for s in r.skipped)


async def test_redirect_to_private_address_blocked():
    r, _ = await run({"/robots.txt": "", "/": httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})})
    assert r.pages == [] and any("non-public" in s for s in r.skipped)


@pytest.mark.parametrize("url", ["http://localhost/x", "http://10.0.0.5/", "file:///etc/passwd",
                                 "https://www.linkedin.com/in/someone", "https://www.google.com/search?q=x"])
def test_disallowed_targets(url):
    with pytest.raises(FetchError):
        _check_public(url, resolve=False)


def test_extract_drops_hidden_script_and_comments():
    html = ('<p>Visible</p><div style="display: none">hidden1</div><span hidden>hidden2</span>'
            '<div aria-hidden="true">hidden3</div><script>evil()</script><!-- hidden4 -->')
    text, _ = extract(html)
    assert text == "Visible"
