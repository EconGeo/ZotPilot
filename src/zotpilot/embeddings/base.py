"""Embedding protocol, exception hierarchy, and retry-delay parsing."""
from __future__ import annotations

import re
from typing import Protocol


class EmbedderProtocol(Protocol):
    """Interface for text embedding."""

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class EmbeddingError(Exception):
    """Raised when embedding fails after retries."""


class RateLimitError(EmbeddingError):
    """Raised when a provider returns a quota/rate-limit (HTTP 429) signal."""

    def __init__(self, message: str, *, provider: str, retry_after: float | None = None):
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


# `retryDelay` (Gemini RPC RetryInfo) — accepts either quote style and an optional
# trailing `s`, matching both `retryDelay: "42s"` and the dict-repr `'retryDelay': '42s'`.
_RETRY_DELAY_RE = re.compile(r"""ret(?:ry)?[-_]?delay['"]?\s*[:=]\s*['"]?(\d+(?:\.\d+)?)\s*s?""", re.IGNORECASE)
# `Retry-After: 42` / `"retry_after": 42` (header or body field).
_RETRY_AFTER_RE = re.compile(r"""retry[-_]?after['"]?\s*[:=]\s*['"]?(\d+(?:\.\d+)?)""", re.IGNORECASE)
# A bare numeric seconds value (e.g. a raw `Retry-After` header value "42").
_BARE_SECONDS_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_retry_delay(detail: str | None) -> float | None:
    """Extract a retry-after seconds hint from a provider error detail string.

    Pure function, no I/O. Returns None when no hint is present.

    Parses ``retryDelay: "42s"``, the Gemini dict-repr ``'retryDelay': '42s'``,
    ``Retry-After: 42``, ``"retry_after": 42``, and a bare numeric-seconds value
    (a raw ``Retry-After`` header). An HTTP-date ``Retry-After`` yields None.
    """
    if detail is None:
        return None
    text = str(detail).strip()
    if not text:
        return None
    # A raw header value is the entire string ("42" / "42.0"); avoid matching a
    # year embedded in an HTTP-date by requiring a full-string numeric match.
    if _BARE_SECONDS_RE.fullmatch(text):
        return float(text)
    for pattern in (_RETRY_DELAY_RE, _RETRY_AFTER_RE):
        m = pattern.search(text)
        if m:
            return float(m.group(1))
    return None
