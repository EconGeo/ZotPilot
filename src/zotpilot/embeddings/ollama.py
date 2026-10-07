"""Ollama local embedding provider."""
import logging

import httpx

from .base import truncate_to_token_budget

logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


def ollama_status(base_url: str, model: str, timeout: float = 3.0) -> str | None:
    """Return why Ollama cannot serve ``model``, or None when it can.

    One quick GET of /api/tags: says whether the server is up and whether the
    model is pulled, so callers can stop before any indexing work starts.
    """
    base_url = base_url.rstrip("/")
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
    except (httpx.ConnectError, httpx.TimeoutException):
        return (
            f"Ollama is not running at {base_url}. Start it (`open -a Ollama` on macOS, "
            f"or `ollama serve`), wait for `curl -s {base_url}/api/tags` to answer, then retry."
        )
    except httpx.HTTPError as exc:
        return f"Ollama at {base_url} answered /api/tags with an error: {exc}"
    names = {m.get("name", "") for m in resp.json().get("models", [])}
    wanted = model if ":" in model else f"{model}:latest"
    if model not in names and wanted not in names:
        return f"Ollama is running but model '{model}' is not pulled. Run `ollama pull {model}`, then retry."
    return None


class OllamaEmbedder:
    """
    Local embeddings via Ollama (http://localhost:11434).
    Recommended model: bge-large (1024 dims, retrieval-optimized, academic corpora).
    Other options: nomic-embed-text (768 dims), snowflake-arctic-embed:l (1024 dims),
    mxbai-embed-large (1024 dims), all-minilm (384 dims).

    BGE models automatically prepend the BAAI retrieval instruction prefix to queries.
    """

    def __init__(
        self,
        model: str = "bge-large",
        base_url: str = OLLAMA_DEFAULT_URL,
        timeout: float = 120.0,
        dimensions: int = 1024,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.dimensions = dimensions
        self.max_input_tokens = 512
        self.embed_batch_size = 16

    def check_ready(self) -> str | None:
        """Return why this embedder cannot run, or None when it can."""
        return ollama_status(self.base_url, self.model)

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if not texts:
            return []
        safe = [truncate_to_token_budget(t, self.max_input_tokens) for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(safe), self.embed_batch_size):
            batch = safe[i : i + self.embed_batch_size]
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": batch},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            out.extend(resp.json()["embeddings"])
        return out

    def embed_query(self, query: str) -> list[float]:
        if "bge" in self.model.lower():
            query = f"Represent this sentence for searching relevant passages: {query}"
        return self.embed([query])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)
