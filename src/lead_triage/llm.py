"""Model provider factory and structured-output calls.

The model is only ever called with plain messages. It is never bound to tools,
so nothing it says can trigger a CRM call directly.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .config import Settings

T = TypeVar("T", bound=BaseModel)
MAX_OUTPUT_TOKENS = 1024  # a JSON score or a short draft never needs more; stops runaway generation
LLM_TIMEOUT_S = 120


class StructuredOutputError(Exception):
    def __init__(self, message: str, attempts: int):
        super().__init__(message)
        self.attempts = attempts


def make_model(settings: Settings) -> BaseChatModel:
    p, name = settings.model_provider, settings.model_name
    if p == "ollama":
        from langchain_ollama import ChatOllama
        # reasoning=False turns off qwen3 "thinking" so outputs are plain JSON and fast.
        return ChatOllama(model=name, base_url=settings.ollama_base_url, temperature=0,
                          reasoning=False, num_ctx=8192, num_predict=MAX_OUTPUT_TOKENS,
                          keep_alive="15m", client_kwargs={"timeout": LLM_TIMEOUT_S})
    if p == "anthropic":  # optional, untested without a key
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=name, temperature=0, max_tokens=1500)
    if p == "openai":  # optional, untested without a key
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=name, temperature=0)
    if p == "gemini":  # optional, untested without a key
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=name, temperature=0)
    raise ValueError(f"unknown MODEL_PROVIDER {p!r} (use ollama, anthropic, openai or gemini)")


def _parse(text: str, schema: type[T]) -> T:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "").strip()
    m = re.search(r"(?s)```(?:json)?\s*(.*?)```", text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            text = text[i:j + 1]
    return schema.model_validate(json.loads(text))


def _content(msg: Any) -> str:
    c = msg.content if isinstance(msg, AIMessage) else getattr(msg, "content", msg)
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


async def structured_call(model: BaseChatModel, system: str, user: str, schema: type[T],
                          provider: str = "ollama") -> tuple[T, int]:
    """Call the model for JSON matching `schema`. Retry once on invalid output, then raise."""
    messages: list = [SystemMessage(system), HumanMessage(user)]
    kwargs: dict[str, Any] = {}
    if provider == "ollama":
        kwargs["format"] = schema.model_json_schema()
    last_err = ""
    for attempt in (1, 2):
        try:
            resp = await asyncio.wait_for(model.ainvoke(messages, **kwargs), timeout=LLM_TIMEOUT_S + 5)
        except Exception as exc:  # timeouts and transport errors count as a failed attempt
            last_err = f"model call failed: {type(exc).__name__}"
            continue
        raw = _content(resp)
        try:
            return _parse(raw, schema), attempt
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:300]}"
            messages = messages + [AIMessage(raw[:2000]), HumanMessage(
                "That was not valid JSON for the required schema. Error: " + last_err +
                "\nReply again with only one JSON object that matches the schema exactly.")]
    raise StructuredOutputError(last_err, attempts=2)
