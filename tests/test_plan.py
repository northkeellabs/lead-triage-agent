import datetime as dt

import pytest
from pydantic import ValidationError

from lead_triage.graph import build_plan
from lead_triage.schema import ActionPlan, Lead, Score

CC = {"action": "create_contact", "name": "A", "email": "a@b.example"}
NOTE = {"action": "add_note", "note": "hello"}
TASK = {"action": "create_task", "name": "Reply to A", "due_date": "2026-11-11"}


def test_valid_plan():
    p = ActionPlan.model_validate({"actions": [CC, NOTE, TASK]})
    assert [a.action for a in p.actions] == ["create_contact", "add_note", "create_task"]


@pytest.mark.parametrize("actions", [
    [CC, NOTE, TASK, {"action": "delete_contact", "contact_id": "1"}],  # unknown action
    [{"action": "send_email", "to": "x@y.example", "body": "hi"}],      # unknown action
    [CC, dict(CC, name="B")],                                            # two contacts
    [CC, NOTE, dict(NOTE, note="again")],                                # two notes
    [CC, NOTE, TASK, TASK],                                              # more than 3
    [NOTE],                                                              # note with no contact
])
def test_plan_rejects_extra_or_disallowed_actions(actions):
    with pytest.raises(ValidationError):
        ActionPlan.model_validate({"actions": actions})


@pytest.mark.parametrize("bad", [
    dict(NOTE, note="x" * 2001),
    dict(CC, name="x" * 121),
    dict(CC, background_info="x" * 1001),
    dict(TASK, name="x" * 201),
    dict(TASK, description="x" * 1001),
    dict(CC, email="not an email"),
    dict(CC, extra_field="nope"),
])
def test_plan_rejects_oversized_or_unknown_fields(bad):
    actions = [CC, bad] if bad["action"] != "create_contact" else [bad]
    with pytest.raises(ValidationError):
        ActionPlan.model_validate({"actions": actions})


def test_plan_rejects_unknown_top_level_field():
    with pytest.raises(ValidationError):
        ActionPlan.model_validate({"actions": [CC], "also": "this"})


def test_build_plan_for_spam_is_empty():
    lead = Lead(name="A", email="a@b.example", company="C", message="m")
    assert build_plan(lead, Score(score=1, tier="spam"), None, dt.date(2026, 11, 10)).actions == []
