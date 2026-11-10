"""Pydantic models: the lead, the LLM's structured outputs, and the action plan.

The action plan is the only thing the execute node will act on. It is strict:
three allowed action types, at most one of each, capped field lengths, and no
unknown fields. Anything else fails validation and nothing is written.
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EMAIL_RE = r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$"
Tier = Literal["hot", "warm", "cold", "spam"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ input

class Lead(Strict):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254, pattern=EMAIL_RE)
    company: str = Field(min_length=1, max_length=160)
    website: str | None = Field(default=None, max_length=300)
    message: str = Field(min_length=1, max_length=5000)
    source: str = Field(default="unknown", max_length=60)

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v


# --------------------------------------------------------- LLM outputs

class Score(Strict):
    score: int = Field(ge=0, le=100)
    tier: Tier
    reasons: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=6)
    red_flags: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=6)


class Draft(Strict):
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=2500)


# --------------------------------------------------------- action plan

class CreateContact(Strict):
    action: Literal["create_contact"]
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254, pattern=EMAIL_RE)
    company_name: str | None = Field(default=None, max_length=160)
    background_info: str | None = Field(default=None, max_length=1000)


class AddNote(Strict):
    action: Literal["add_note"]
    contact_id: str | None = Field(default=None, max_length=40, description="Existing contact id from dedupe; None = the contact created by this plan")
    note: str = Field(min_length=1, max_length=2000)


class CreateTask(Strict):
    action: Literal["create_task"]
    contact_id: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    due_date: dt.date
    description: str | None = Field(default=None, max_length=1000)


Action = Annotated[Union[CreateContact, AddNote, CreateTask], Field(discriminator="action")]
ALLOWED_ACTIONS = ("create_contact", "add_note", "create_task")


class ActionPlan(Strict):
    actions: list[Action] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _one_of_each(self) -> "ActionPlan":
        kinds = [a.action for a in self.actions]
        for k in ALLOWED_ACTIONS:
            if kinds.count(k) > 1:
                raise ValueError(f"at most one {k} per lead (got {kinds.count(k)})")
        creates = "create_contact" in kinds
        for a in self.actions:
            if isinstance(a, (AddNote, CreateTask)) and a.contact_id is None and not creates:
                raise ValueError(f"{a.action} has no contact_id and the plan creates no contact")
        return self

    def contact_ids(self) -> set[str]:
        return {a.contact_id for a in self.actions if isinstance(a, (AddNote, CreateTask)) and a.contact_id}
