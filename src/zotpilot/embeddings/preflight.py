"""Fast reachability/auth check for the configured embedder."""
import logging

from .gemini import EmbeddingError

logger = logging.getLogger(__name__)


def check_embedder(embedder) -> None:
    """Embed a tiny probe so misconfiguration fails in seconds, not hours.

    No-op when ``embedder`` is None (no-RAG mode).
    """
    if embedder is None:
        return
    try:
        vec = embedder.embed(["ping"])
    except Exception as exc:
        raise EmbeddingError(
            f"Embedding preflight failed: {type(exc).__name__}: {exc}. "
            f"Check embedding_provider, model, API key, and that the backend is reachable."
        ) from exc
    if not vec or not vec[0]:
        raise EmbeddingError("Embedding preflight returned no vector; check provider/model config.")
