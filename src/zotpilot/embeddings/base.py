"""Embedding protocol definition."""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmbedderProtocol(Protocol):
    """Interface for text embedding."""

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


def truncate_to_token_budget(text: str, max_tokens: int, est_chars_per_token: int = 3) -> str:
    """Conservatively cap text to a token budget using a chars-per-token estimate.

    Uses a deliberately low chars/token ratio (3, not 4) so dense/technical text
    stays under the model limit without a tokenizer dependency. Phase B replaces
    this estimate with the model's real tokenizer.

    Emits a WARNING when truncation actually occurs, naming how many characters
    were removed, so callers can detect over-long inputs in logs.
    """
    max_chars = max_tokens * est_chars_per_token
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    logger.warning(
        "truncate_to_token_budget: truncated %d chars (from %d to %d, max_tokens=%d)",
        cut, len(text), max_chars, max_tokens,
    )
    return text[:max_chars]
