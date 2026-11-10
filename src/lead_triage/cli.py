"""Command line: triage run | pending | approve | serve | eval."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import Settings, known_secrets, lacrm_api_key
from .runtime import open_runner, summarize
from .security import redact


def _emit(text: str, out: Path | None) -> None:
    text = redact(text, known_secrets())
    print(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def _load_leads(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("leads", [data.get("lead", data)])
    return data


def _ask(prompt: dict, settings: Settings) -> dict:
    """Interactive approval. Edit writes the plan to a JSON file for you to change, then validates it."""
    while True:
        if prompt.get("error"):
            print(f"!! {prompt['error']}")
        mode = "LIVE: approving writes to the CRM" if prompt.get("live") else "dry-run: no CRM writes"
        ans = input(f"[{mode}] approve / edit / reject? [a/e/r]: ").strip().lower()
        if ans in ("a", "approve"):
            return {"decision": "approve"}
        if ans in ("r", "reject"):
            return {"decision": "reject"}
        if ans in ("e", "edit"):
            f = settings.data_dir / f"edit-{prompt['run_id']}.json"
            f.write_text(json.dumps({"plan": prompt["plan"], "draft": prompt.get("draft")}, indent=2), encoding="utf-8")
            input(f"Edit {f} (plan and/or draft), save it, then press Enter: ")
            data = json.loads(f.read_text(encoding="utf-8"))
            return {"decision": "edit", "plan": data.get("plan"), "draft": data.get("draft")}


async def _run(args, settings: Settings) -> int:
    leads = _load_leads(Path(args.file))
    if args.live and not lacrm_api_key():
        print("LACRM_API_KEY is not set; cannot run --live.", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    async with open_runner(settings, fixtures=Path(args.fixtures) if args.fixtures else None) as runner:
        for raw in leads:
            res = await runner.start(raw, live=args.live)
            _emit("=" * 70 + "\n" + summarize(res), out)
            while res["status"] == "pending_approval" and not args.no_prompt:
                res = await runner.resume(res["run_id"], _ask(res["approval"], settings))
                _emit("-" * 70 + "\n" + summarize(res), out)
            if res["status"] == "pending_approval":
                _emit(f"pending: approve later with `triage approve {res['run_id']} --decision approve|reject`", out)
    return 0


async def _pending(args, settings: Settings) -> int:
    async with open_runner(settings) as runner:
        rows = runner.index.pending()
        if not rows:
            print("no pending approvals")
        for run_id, company, created in rows:
            p = await runner.pending_payload(run_id)
            tier = (p or {}).get("score", {}).get("tier")
            print(f"{run_id}  {company}  tier={tier}  live={(p or {}).get('live')}  created={created}")
    return 0


async def _approve(args, settings: Settings) -> int:
    resp: dict = {"decision": args.decision}
    if args.decision == "edit":
        if not args.plan:
            print("--plan FILE is required with --decision edit", file=sys.stderr)
            return 2
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        resp.update({"plan": data.get("plan", data), "draft": data.get("draft")})
    out = Path(args.out) if args.out else None
    async with open_runner(settings) as runner:
        payload = await runner.pending_payload(args.run_id)
        if payload is None:
            print(f"{args.run_id} is not waiting for approval", file=sys.stderr)
            return 1
        if payload.get("live") and not lacrm_api_key():
            print("LACRM_API_KEY is not set; cannot execute a live plan.", file=sys.stderr)
            return 2
        res = await runner.resume(args.run_id, resp)
        _emit("-" * 70 + "\n" + summarize(res), out)
    return 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="triage", description="Inbound lead triage agent (drafts only; never sends email).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="triage leads from a JSON file")
    r.add_argument("file")
    r.add_argument("--live", action="store_true", help="write to the CRM after approval (default: dry run, no writes)")
    r.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    r.add_argument("--fixtures", help="serve websites from local fixture folders instead of the network")
    r.add_argument("--no-prompt", action="store_true", help="leave approvals pending instead of asking now")
    r.add_argument("--out", help="also append redacted output to this file")
    sub.add_parser("pending", help="list runs waiting for approval")
    a = sub.add_parser("approve", help="approve, edit or reject a pending run")
    a.add_argument("run_id")
    a.add_argument("--decision", choices=["approve", "edit", "reject"], required=True)
    a.add_argument("--plan", help="JSON file with the edited plan (for --decision edit)")
    a.add_argument("--out")
    s = sub.add_parser("serve", help="run the webhook API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8088)
    e = sub.add_parser("eval", help="run the offline eval set")
    e.add_argument("--leads", default="evals/leads")
    e.add_argument("--sites", default="evals/sites")
    e.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    settings = Settings()
    if args.cmd == "run" and args.live and args.dry_run:
        ap.error("--live and --dry-run are mutually exclusive")
    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    if args.cmd == "eval":
        from .evals import run_eval
        raise SystemExit(asyncio.run(run_eval(settings, Path(args.leads), Path(args.sites), args.limit)))
    fn = {"run": _run, "pending": _pending, "approve": _approve}[args.cmd]
    raise SystemExit(asyncio.run(fn(args, settings)))


if __name__ == "__main__":
    main()
