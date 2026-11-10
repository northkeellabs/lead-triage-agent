"""Untrusted-data wrapping, URL stripping, redaction and hashing."""
from __future__ import annotations

import hashlib
import re
from typing import Any

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")
_BARE_DOMAIN_RE = re.compile(r"(?i)\b[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.(?:com|net|org|io|ai|co|xyz|info|biz|ru|top|click|link|example)(?:/[^\s]*)?\b")
# Long opaque tokens that look like API keys or bearer tokens.
_KEYLIKE_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9_-]{40,})\b")
_INJECTION_RE = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:the |all )?(?:previous|prior|above)|"
    r"you are (?:now )?(?:an? )?(?:ai|assistant|agent|language model)|system prompt|"
    r"(?:mark|score|rate|classify) (?:this|the) (?:lead|company|prospect) as|"
    r"(?:create|add) \d+ (?:contacts|notes|tasks)|note to (?:the )?(?:ai|assistant|agent)|"
    r"instructions? (?:for|to) (?:the )?(?:ai|assistant|agent|llm))"
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def wrap_untrusted(label: str, text: str) -> str:
    """Wrap outside text so the model treats it as data. Neutralise any attempt to close the tag."""
    safe = re.sub(r"(?i)</?\s*untrusted[^>]*>", "[tag removed]", text or "")
    return f"<untrusted_{label}>\n{safe}\n</untrusted_{label}>"


def looks_like_injection(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _INJECTION_RE.finditer(text or "")})


def strip_urls(text: str) -> str:
    text = _URL_RE.sub("[link removed]", text or "")
    return _BARE_DOMAIN_RE.sub("[link removed]", text)


def find_urls(text: str) -> list[str]:
    return _URL_RE.findall(text or "")


def redact(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    """Recursively redact known secrets and key-like tokens from strings in a structure."""
    if isinstance(value, str):
        out = value
        for s in secrets:
            if s and len(s) >= 6:
                out = out.replace(s, "[REDACTED]")
        return _KEYLIKE_RE.sub("[REDACTED]", out)
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    return value
