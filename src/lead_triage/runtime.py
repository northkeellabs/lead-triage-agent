"""Shared runner for the CLI and the webhook: SQLite checkpointer + run index."""
from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from .audit import AuditLog
from .config import Settings, known_secrets, load_icp
from .crm import Crm, McpCrm
from .graph import Deps, build_graph
from .llm import make_model
from .research import Fetcher, FixtureFetcher, HttpFetcher


def new_run_id(company: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (company or "lead").lower()).strip("-")[:24] or "lead"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


class RunIndex:
    """Tiny table of runs so pending approvals can be listed after a restart."""

    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, company TEXT, status TEXT, "
                      "created TEXT DEFAULT CURRENT_TIMESTAMP)")

    def set(self, run_id: str, company: str, status: str) -> None:
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO runs (run_id, company, status) VALUES (?,?,?) "
                      "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status", (run_id, company, status))

    def pending(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT run_id, company, created FROM runs WHERE status='pending_approval' "
                             "ORDER BY created").fetchall()


class Runner:
    def __init__(self, graph, index: RunIndex | None):
        self.graph, self.index = graph, index

    async def start(self, raw_lead: dict, live: bool = False, run_id: str | None = None) -> dict:
        run_id = run_id or new_run_id(str(raw_lead.get("company", "")))
        state = await self.graph.ainvoke({"run_id": run_id, "raw_lead": raw_lead, "live": live, "timings": {}},
                                         {"configurable": {"thread_id": run_id}})
        return await self._result(run_id, state)

    async def resume(self, run_id: str, response: dict) -> dict:
        state = await self.graph.ainvoke(Command(resume=response), {"configurable": {"thread_id": run_id}})
        return await self._result(run_id, state)

    async def pending_payload(self, run_id: str) -> dict | None:
        snap = await self.graph.aget_state({"configurable": {"thread_id": run_id}})
        if snap.interrupts:
            return snap.interrupts[0].value
        return None

    async def _result(self, run_id: str, state: dict) -> dict:
        interrupts = state.get("__interrupt__") if isinstance(state, dict) else None
        status = "pending_approval" if interrupts else state.get("status", "unknown")
        if self.index:
            self.index.set(run_id, (state.get("lead") or state.get("raw_lead") or {}).get("company", ""), status)
        return {"run_id": run_id, "status": status, "state": state,
                "approval": interrupts[0].value if interrupts else None}


@asynccontextmanager
async def open_runner(settings: Settings, *, fixtures: Path | None = None, crm: Crm | None = None,
                      fetcher: Fetcher | None = None, model=None) -> AsyncIterator[Runner]:
    settings.ensure_dirs()
    http = None
    if fetcher is None:
        if fixtures:
            fetcher = FixtureFetcher(fixtures)
        else:
            fetcher = http = HttpFetcher()
    deps = Deps(model=model or make_model(settings), fetcher=fetcher, crm=crm or McpCrm(settings),
                icp=load_icp(settings.icp_path), audit=AuditLog(settings.data_dir / "audit", known_secrets()),
                provider=settings.model_provider)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as saver:
            yield Runner(build_graph(deps, saver), RunIndex(settings.data_dir / "runs.sqlite"))
    finally:
        if http:
            await http.aclose()


def summarize(result: dict[str, Any]) -> str:
    """Human-readable summary of a run (used by CLI, saved outputs)."""
    st = result.get("state") or {}
    ap = result.get("approval") or {}
    lines = [f"run_id: {result['run_id']}", f"status: {result['status']}"]
    sc = st.get("score") or ap.get("score")
    if sc:
        lines.append(f"score: {sc['score']}/100  tier: {sc['tier']}" + (f"  (model said {st['llm_tier']})" if st.get("llm_tier") and st.get("llm_tier") != sc["tier"] else ""))
        lines += [f"  reason: {r}" for r in sc.get("reasons", [])]
        lines += [f"  red flag: {r}" for r in sc.get("red_flags", [])]
    if st.get("reason"):
        lines.append(f"reason: {st['reason']}")
    if st.get("crm_note") or ap.get("crm_note"):
        lines.append(f"crm: {st.get('crm_note') or ap.get('crm_note')}")
    res = st.get("research") or {}
    if res:
        lines.append(f"pages fetched: {', '.join(res.get('pages') or []) or 'none'}")
        lines += [f"  skipped: {s}" for s in res.get("skipped") or []]
    d = st.get("draft") if "draft" in st else ap.get("draft")
    if d:
        lines += ["draft reply (NOT sent):", f"  Subject: {d['subject']}"] + ["  " + l for l in d["body"].splitlines()]
    plan = st.get("plan") or ap.get("plan")
    if plan:
        lines.append("action plan:")
        for a in plan.get("actions", []):
            lines.append("  - " + ", ".join(f"{k}={v}" for k, v in a.items()))
    if st.get("results"):
        lines.append("execution:")
        lines += [f"  - {r}" for r in st["results"]]
    if st.get("timings"):
        lines.append("timings (s): " + ", ".join(f"{k}={v}" for k, v in st["timings"].items()))
    return "\n".join(lines)
