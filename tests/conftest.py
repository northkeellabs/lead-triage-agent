import datetime as dt
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from lead_triage.audit import AuditLog
from lead_triage.config import ROOT, load_icp
from lead_triage.graph import Deps, build_graph
from lead_triage.research import FixtureFetcher

FIXTURES = ROOT / "evals" / "sites"


class FakeWriter:
    def __init__(self, crm):
        self.crm = crm

    async def call(self, tool, args):
        self.crm.write_calls.append((tool, args))
        if self.crm.fail_on == tool:
            raise RuntimeError(f"boom with key {self.crm.secret}")
        n = len(self.crm.write_calls)
        return {"create_contact": {"contact_id": "C900"}, "add_note": {"note_id": f"N{n}"},
                "create_task": {"task_id": f"T{n}"}}[tool]


class FakeCrm:
    """Mocked MCP client: records every call; writes only through writer()."""

    def __init__(self, existing=None, fail_on=None, secret=""):
        self.existing = existing or []
        self.search_calls, self.write_calls = [], []
        self.writer_opened = 0
        self.fail_on, self.secret = fail_on, secret

    async def search(self, query):
        self.search_calls.append(query)
        return [r for r in self.existing if query.lower() in json.dumps(r).lower()]

    @asynccontextmanager
    async def writer(self):
        self.writer_opened += 1
        yield FakeWriter(self)


def score_json(tier="hot", score=85, reasons=("B2B SaaS with a concrete MCP project", "budget stated"), flags=()):
    return json.dumps({"score": score, "tier": tier, "reasons": list(reasons), "red_flags": list(flags)})


DRAFT_JSON = json.dumps({"subject": "Re: your MCP server project", "body": "Hi,\n\nThanks for reaching out.\n\n[Your name], Northkeel Labs"})

LEAD = {"name": "Amelia Hart", "email": "amelia.hart@ledgerline-demo.example", "company": "Ledgerline (Demo)",
        "website": "https://ledgerline-demo.example", "message": "We want an MCP server for our API.", "source": "test"}


@pytest.fixture
def make_graph(tmp_path):
    def _make(responses, crm=None, audit_dir=None, secrets=()):
        crm = crm or FakeCrm()
        audit = AuditLog(audit_dir or tmp_path / "audit", list(secrets))
        deps = Deps(model=FakeListChatModel(responses=list(responses)), fetcher=FixtureFetcher(FIXTURES), crm=crm,
                    icp=load_icp(ROOT / "icp.yaml"), audit=audit, provider="fake", today=lambda: dt.date(2026, 11, 13))
        return build_graph(deps, InMemorySaver()), crm, audit
    return _make


def cfg(tid="t1"):
    return {"configurable": {"thread_id": tid}}


def start_state(lead=None, live=False, run_id="t1"):
    return {"run_id": run_id, "raw_lead": dict(lead or LEAD), "live": live, "timings": {}}
