"""Polite, bounded website research.

Rules: public pages only, at most 3 pages per lead (home, plus about and pricing
only if the home page links to them on the same host), robots.txt respected,
10 s timeout, 500 KB cap per response, clear User-Agent, no search engines.
Extracted text is returned for wrapping as untrusted data.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Comment

from .config import USER_AGENT

MAX_PAGES = 3
MAX_BYTES = 500 * 1024
TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
TEXT_PER_PAGE = 3500
_ABOUT_RE = re.compile(r"(?i)^/(about|about-us|company|who-we-are)(\.html?)?/?$")
_PRICING_RE = re.compile(r"(?i)^/(pricing|plans|prices)(\.html?)?/?$")
_HIDDEN_STYLE_RE = re.compile(r"(?i)(display\s*:\s*none|visibility\s*:\s*hidden)")


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    text: str


@dataclass
class Research:
    pages: list[Page] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # reasons (robots, size, errors)

    def as_text(self) -> str:
        return "\n\n".join(f"[page] {p.url}\n{p.text}" for p in self.pages)


class Fetcher(Protocol):
    async def get(self, url: str) -> tuple[int, str]: ...


# ------------------------------------------------------------ extraction

def extract(html: str) -> tuple[str, list[str]]:
    """Return visible text (hidden elements, scripts and comments removed) and same-page links."""
    soup = BeautifulSoup(html, "html.parser")
    links = [a.get("href", "") for a in soup.find_all("a")]
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup(["script", "style", "noscript", "template", "iframe", "svg", "head"]):
        t.decompose()
    hidden = [t for t in soup.find_all(True)
              if "hidden" in (t.attrs or {}) or str(t.attrs.get("aria-hidden", "")).lower() == "true"
              or _HIDDEN_STYLE_RE.search(str(t.attrs.get("style", "")))]
    for t in hidden:
        if not t.decomposed:
            t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:TEXT_PER_PAGE], links


def pick_extra_pages(base: str, links: list[str]) -> list[str]:
    host = urlparse(base).netloc
    about = pricing = None
    for href in links:
        u = urlparse(urljoin(base, href))
        if u.scheme not in ("http", "https") or u.netloc != host:
            continue
        if about is None and _ABOUT_RE.match(u.path or ""):
            about = f"{u.scheme}://{u.netloc}{u.path}"
        elif pricing is None and _PRICING_RE.match(u.path or ""):
            pricing = f"{u.scheme}://{u.netloc}{u.path}"
    return [p for p in (about, pricing) if p]


# --------------------------------------------------------------- fetchers

def _check_public(url: str, resolve: bool = True) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("only http(s) URLs are allowed")
    host = u.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise FetchError("non-public host")
    if any(b in host for b in ("linkedin.", "google.", "bing.", "duckduckgo.", "facebook.")):
        raise FetchError("host not allowed (no social networks or search engines)")
    addrs: list[str] = []
    try:
        addrs = [str(ipaddress.ip_address(host))]
    except ValueError:
        if resolve:
            try:
                addrs = list({i[4][0] for i in socket.getaddrinfo(host, None)})
            except socket.gaierror as exc:
                raise FetchError("DNS lookup failed") from exc
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise FetchError("host resolves to a non-public address")


class HttpFetcher:
    """Real network fetcher with size cap, timeout and manual, re-checked redirects."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, resolve: bool = True):
        self._client = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(TIMEOUT_S), follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8"})
        self._resolve = resolve

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str) -> tuple[int, str]:
        for _ in range(MAX_REDIRECTS + 1):
            _check_public(url, self._resolve)
            async with self._client.stream("GET", url) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    continue
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise FetchError(f"response larger than {MAX_BYTES} bytes")
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise FetchError(f"response larger than {MAX_BYTES} bytes")
                return resp.status_code, buf.decode(resp.encoding or "utf-8", errors="replace")
        raise FetchError("too many redirects")


class FixtureFetcher:
    """Offline fetcher for tests and evals: https://<host>/<path> -> <root>/<host>/<path>.html."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.requested: list[str] = []

    async def get(self, url: str) -> tuple[int, str]:
        self.requested.append(url)
        u = urlparse(url)
        path = (u.path or "/").strip("/")
        base = self.root / (u.hostname or "")
        if path == "robots.txt":
            f = base / "robots.txt"
        else:
            name = path or "index"
            f = base / (name if name.endswith((".html", ".htm")) else name + ".html")
        if not f.resolve().is_relative_to(self.root.resolve()) or not f.is_file():
            return 404, ""
        data = f.read_bytes()
        if len(data) > MAX_BYTES:
            raise FetchError(f"response larger than {MAX_BYTES} bytes")
        return 200, data.decode("utf-8", errors="replace")


# --------------------------------------------------------------- research

async def _robots(fetcher: Fetcher, site: str) -> RobotFileParser | None:
    """Parsed robots.txt; None means 'disallow everything' (robots unreachable for a server error)."""
    rp = RobotFileParser()
    try:
        status, body = await fetcher.get(urljoin(site, "/robots.txt"))
    except FetchError:
        return None
    except (httpx.HTTPError, OSError):
        return None
    if status >= 500:
        return None
    rp.parse(body.splitlines() if status == 200 else [])
    return rp


async def research_site(fetcher: Fetcher, website: str | None) -> Research:
    out = Research()
    if not website:
        out.skipped.append("no website given")
        return out
    u = urlparse(website)
    site = f"{u.scheme}://{u.netloc}"
    try:
        _check_public(site, resolve=False)
    except FetchError as exc:
        out.skipped.append(f"{site}: {exc}")
        return out
    rp = await _robots(fetcher, site)
    if rp is None:
        out.skipped.append(f"{site}/robots.txt unreachable; not crawling")
        return out
    queue = [site + "/"]
    seen: set[str] = set()
    while queue and len(out.pages) < MAX_PAGES and len(seen) < MAX_PAGES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not rp.can_fetch(USER_AGENT, url):
            out.skipped.append(f"{url}: disallowed by robots.txt")
            continue
        try:
            status, body = await asyncio.wait_for(fetcher.get(url), timeout=TIMEOUT_S + 2)
        except (FetchError, httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            out.skipped.append(f"{url}: {type(exc).__name__}: {exc}"[:200])
            continue
        if status != 200:
            out.skipped.append(f"{url}: HTTP {status}")
            continue
        text, links = extract(body)
        out.pages.append(Page(url, text))
        if len(out.pages) == 1:
            queue.extend(pick_extra_pages(url, links))
    return out
