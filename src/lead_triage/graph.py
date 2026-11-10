"""The LangGraph workflow.

validate -> dedupe -> research -> score -> draft -> plan -> approval (interrupt) -> execute -> report

The LLM is used in exactly two nodes (score, draft) and has no tools. The plan is
built in code, validated by pydantic, shown to a human, and only then executed.
"""
from __future__ import annotations

import datetime as dt
import functools
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from . import prompts
from .audit import AuditLog
from .crm import Crm, CrmError
from .llm import StructuredOutputError, structured_call
from .research import Fetcher, research_site
from .schema import ActionPlan, AddNote, CreateContact, CreateTask, Draft, Lead, Score
from .security import find_urls, looks_like_injection, sha256, strip_urls

ALLOWED_DRAFT_DOMAINS = ("northkeellabs.com",)
MAX_EDIT_ATTEMPTS = 3


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class TriageState(TypedDict, total=False):
    run_id: str
    raw_lead: dict
    live: bool
    lead: dict
    status: str  # running | invalid | needs_human | pending_approval | rejected | executed | dry_run | no_action | failed | partial
    reason: str
    existing_contact_id: str | None
    company_matches: list[str]
    crm_note: str
    research: dict
    injection_signals: list[str]
    llm_tier: str
    score: dict
    draft: dict | None
    draft_removed_urls: list[str]
    plan: dict
    decision: str
    results: list[dict]
    timings: Annotated[dict, _merge]


@dataclass
class Deps:
    model: BaseChatModel
    fetcher: Fetcher
    crm: Crm
    icp: dict
    audit: AuditLog
    provider: str = "ollama"
    today: Callable[[], dt.date] = field(default=dt.date.today)


def next_business_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _timed(name: str, deps: Deps):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(state: TriageState) -> dict:
            t0 = time.perf_counter()
            out = await fn(state) or {}
            secs = round(time.perf_counter() - t0, 3)
            deps.audit.write(state.get("run_id", "?"), "node", node=name, seconds=secs,
                             status=out.get("status", state.get("status")))
            return {**out, "timings": {name: secs}}
        return wrapper
    return deco


def build_plan(lead: Lead, score: Score, existing_id: str | None, today: dt.date) -> ActionPlan:
    if score.tier == "spam":
        return ActionPlan(actions=[])
    reasons = "; ".join(strip_urls(r) for r in score.reasons) or "none given"
    flags = "; ".join(strip_urls(r) for r in score.red_flags) or "none"
    note = (f"Lead triage (automated score, human-approved): {score.score}/100, tier {score.tier}. "
            f"Source: {lead.source}. Reasons: {reasons}. Red flags: {flags}. "
            "A draft reply was prepared for human review; nothing was sent.")[:2000]
    actions: list = []
    if not existing_id:
        actions.append(CreateContact(action="create_contact", name=lead.name, email=lead.email,
                                     company_name=lead.company,
                                     background_info=f"Inbound lead via {lead.source}."[:1000]))
    actions.append(AddNote(action="add_note", contact_id=existing_id, note=note))
    actions.append(CreateTask(action="create_task", contact_id=existing_id, name=f"Reply to {lead.name}"[:200],
                              due_date=next_business_day(today),
                              description=f"Tier {score.tier} ({score.score}/100). Review the draft reply and send it yourself."))
    return ActionPlan(actions=actions)


def build_graph(deps: Deps, checkpointer=None):
    audit = deps.audit

    @_timed("validate", deps)
    async def validate(state: TriageState) -> dict:
        try:
            lead = Lead.model_validate(state["raw_lead"])
        except ValidationError as exc:
            errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[:500]
            audit.write(state["run_id"], "invalid_input", errors=errs)
            return {"status": "invalid", "reason": errs}
        audit.write(state["run_id"], "input", email_hash=sha256(lead.email.lower()), name_hash=sha256(lead.name),
                    company=lead.company, message_hash=sha256(lead.message), source=lead.source, live=state.get("live", False))
        return {"lead": lead.model_dump(), "status": "running"}

    @_timed("dedupe", deps)
    async def dedupe(state: TriageState) -> dict:
        lead = Lead.model_validate(state["lead"])
        existing, companies, note = None, [], "no existing contact found"
        try:
            for r in await deps.crm.search(lead.email):
                if (r.get("email") or "").lower() == lead.email.lower() and not r.get("is_company"):
                    existing = str(r.get("contact_id"))
                    break
            if not existing:
                for r in await deps.crm.search(lead.company):
                    if lead.company.lower() in (r.get("company_name") or r.get("name") or "").lower():
                        companies.append(str(r.get("contact_id")))
            audit.write(state["run_id"], "tool_call", tool="search_contacts", mode="read-only",
                        existing=bool(existing), company_matches=len(companies))
        except (CrmError, Exception) as exc:  # dedupe failure must not crash triage
            note = f"CRM lookup failed ({type(exc).__name__}); duplicates not checked"
            audit.write(state["run_id"], "tool_error", tool="search_contacts", error=type(exc).__name__)
        if existing:
            note = "an existing contact has this email; the plan updates it instead of creating a new one"
        elif companies:
            note = f"{len(companies)} existing record(s) mention this company"
        return {"existing_contact_id": existing, "company_matches": companies, "crm_note": note}

    @_timed("research", deps)
    async def research(state: TriageState) -> dict:
        lead = Lead.model_validate(state["lead"])
        r = await research_site(deps.fetcher, lead.website)
        audit.write(state["run_id"], "research", pages=[p.url for p in r.pages], skipped=r.skipped)
        signals = looks_like_injection(lead.message) + [f"website: {s}" for s in looks_like_injection(r.as_text())]
        return {"research": {"pages": [p.url for p in r.pages], "skipped": r.skipped, "text": r.as_text()[:9000]},
                "injection_signals": signals}

    @_timed("score", deps)
    async def score(state: TriageState) -> dict:
        lead = Lead.model_validate(state["lead"])
        try:
            s, attempts = await structured_call(
                deps.model, prompts.score_system(deps.icp),
                prompts.score_user(lead, state["research"]["text"], state.get("crm_note", "")), Score, deps.provider)
        except StructuredOutputError as exc:
            audit.write(state["run_id"], "llm_invalid_output", node="score", attempts=exc.attempts, error=str(exc)[:300])
            return {"status": "needs_human", "reason": "model returned invalid JSON twice while scoring"}
        llm_tier = s.tier
        flags = [strip_urls(f) for f in s.red_flags]
        if state.get("injection_signals"):
            flags = (flags + ["possible prompt injection in lead or website text (detected by rule, not by the model)"])[-6:]
            if s.tier == "hot":  # policy: a lead that tries to steer the scorer never auto-ranks hot
                s = s.model_copy(update={"tier": "warm", "score": min(s.score, 74)})
        s = s.model_copy(update={"reasons": [strip_urls(r) for r in s.reasons], "red_flags": flags})
        audit.write(state["run_id"], "decision", node="score", score=s.score, tier=s.tier, llm_tier=llm_tier,
                    attempts=attempts, injection_signals=len(state.get("injection_signals") or []))
        return {"score": s.model_dump(), "llm_tier": llm_tier}

    @_timed("draft", deps)
    async def draft(state: TriageState) -> dict:
        s = Score.model_validate(state["score"])
        if s.tier == "spam":
            return {"draft": None}
        lead = Lead.model_validate(state["lead"])
        try:
            d, attempts = await structured_call(deps.model, prompts.draft_system(s.tier),
                                                prompts.draft_user(lead, s.reasons), Draft, deps.provider)
        except StructuredOutputError as exc:
            audit.write(state["run_id"], "llm_invalid_output", node="draft", attempts=exc.attempts, error=str(exc)[:300])
            return {"status": "needs_human", "reason": "model returned invalid JSON twice while drafting"}
        removed = [u for u in find_urls(d.body + " " + d.subject)
                   if not any(dom in u.lower() for dom in ALLOWED_DRAFT_DOMAINS)]
        if removed:
            d = Draft(subject=strip_urls(d.subject), body=strip_urls(d.body))
        audit.write(state["run_id"], "decision", node="draft", attempts=attempts, removed_urls=len(removed))
        return {"draft": d.model_dump(), "draft_removed_urls": removed}

    @_timed("plan", deps)
    async def plan(state: TriageState) -> dict:
        p = build_plan(Lead.model_validate(state["lead"]), Score.model_validate(state["score"]),
                       state.get("existing_contact_id"), deps.today())
        audit.write(state["run_id"], "plan", actions=[a.action for a in p.actions])
        return {"plan": p.model_dump(mode="json"), "status": "pending_approval" if p.actions else "no_action"}

    async def approval(state: TriageState) -> dict:
        # No @_timed here: the wait for a human is not processing time.
        prompt = {"run_id": state["run_id"], "lead": state["lead"], "score": state["score"], "draft": state.get("draft"),
                  "plan": state["plan"], "crm_note": state.get("crm_note"), "live": state.get("live", False)}
        error = None
        for _ in range(MAX_EDIT_ATTEMPTS):
            resp = interrupt({**prompt, "error": error})
            decision = (resp or {}).get("decision") if isinstance(resp, dict) else resp
            if decision == "approve":
                audit.write(state["run_id"], "approval", decision="approve")
                return {"decision": "approve"}
            if decision == "reject":
                audit.write(state["run_id"], "approval", decision="reject")
                return {"decision": "reject", "status": "rejected"}
            if decision == "edit":
                try:
                    p = ActionPlan.model_validate(resp.get("plan"))
                    _check_contact_ids(p, state.get("existing_contact_id"))
                except (ValidationError, ValueError) as exc:
                    error = f"edited plan rejected: {str(exc)[:300]}"
                    audit.write(state["run_id"], "approval", decision="edit_invalid", error=error)
                    continue
                out: dict[str, Any] = {"decision": "edit", "plan": p.model_dump(mode="json")}
                if isinstance(resp.get("draft"), dict):
                    out["draft"] = Draft.model_validate(resp["draft"]).model_dump()
                audit.write(state["run_id"], "approval", decision="edit", actions=[a.action for a in p.actions])
                return out
            error = f"unknown decision {decision!r}; use approve, edit or reject"
        audit.write(state["run_id"], "approval", decision="reject", reason="too many invalid responses")
        return {"decision": "reject", "status": "rejected", "reason": "too many invalid approval responses"}

    @_timed("execute", deps)
    async def execute(state: TriageState) -> dict:
        p = ActionPlan.model_validate(state["plan"])  # re-validate right before acting
        _check_contact_ids(p, state.get("existing_contact_id"))
        if not state.get("live"):
            results = [{"action": a.action, "dry_run": True} for a in p.actions]
            audit.write(state["run_id"], "execute", mode="dry-run", write_calls=0, actions=[a.action for a in p.actions])
            return {"results": results, "status": "dry_run"}
        results: list[dict] = []
        contact_id = state.get("existing_contact_id")
        try:
            async with deps.crm.writer() as w:
                for a in p.actions:
                    if isinstance(a, CreateContact):
                        args = a.model_dump(exclude={"action"}, exclude_none=True)
                        r = await w.call("create_contact", args)
                        contact_id = str(r.get("contact_id") or "") or None
                        if not contact_id:
                            raise CrmError("create_contact returned no contact id")
                    elif isinstance(a, AddNote):
                        r = await w.call("add_note", {"contact_id": a.contact_id or contact_id, "note": a.note})
                    else:
                        args = {"name": a.name, "due_date": a.due_date.isoformat(), "contact_id": a.contact_id or contact_id}
                        if a.description:
                            args["description"] = a.description
                        r = await w.call("create_task", args)
                    results.append({"action": a.action, "ok": True, **{k: v for k, v in r.items() if k.endswith("_id")}})
                    audit.write(state["run_id"], "tool_call", tool=a.action, mode="write", ok=True)
        except Exception as exc:  # do not retry non-idempotent writes
            msg = str(exc)[:300] if isinstance(exc, CrmError) else type(exc).__name__
            audit.write(state["run_id"], "tool_error", node="execute", error=msg, completed=len(results))
            return {"results": results + [{"error": msg}], "status": "partial" if results else "failed", "reason": msg}
        return {"results": results, "status": "executed"}

    @_timed("report", deps)
    async def report(state: TriageState) -> dict:
        audit.write(state["run_id"], "report", status=state.get("status"), tier=(state.get("score") or {}).get("tier"),
                    timings=state.get("timings"))
        return {}

    def after_validate(s: TriageState) -> str:
        return "report" if s.get("status") == "invalid" else "dedupe"

    def after_llm(next_node: str):
        def route(s: TriageState) -> str:
            return "report" if s.get("status") == "needs_human" else next_node
        return route

    def after_plan(s: TriageState) -> str:
        return "approval" if (s.get("plan") or {}).get("actions") else "report"

    def after_approval(s: TriageState) -> str:
        return "execute" if s.get("decision") in ("approve", "edit") else "report"

    g = StateGraph(TriageState)
    for name, fn in [("validate", validate), ("dedupe", dedupe), ("research", research), ("score", score),
                     ("draft", draft), ("plan", plan), ("approval", approval), ("execute", execute), ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "validate")
    g.add_conditional_edges("validate", after_validate, ["dedupe", "report"])
    g.add_edge("dedupe", "research")
    g.add_edge("research", "score")
    g.add_conditional_edges("score", after_llm("draft"), ["draft", "report"])
    g.add_conditional_edges("draft", after_llm("plan"), ["plan", "report"])
    g.add_conditional_edges("plan", after_plan, ["approval", "report"])
    g.add_conditional_edges("approval", after_approval, ["execute", "report"])
    g.add_edge("execute", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def _check_contact_ids(p: ActionPlan, existing: str | None) -> None:
    allowed = {existing} if existing else set()
    bad = p.contact_ids() - allowed
    if bad:
        raise ValueError("plan targets a contact other than this lead's own record")
