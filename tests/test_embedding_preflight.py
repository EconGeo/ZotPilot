import pytest

from zotpilot.embeddings.gemini import EmbeddingError
from zotpilot.embeddings.preflight import check_embedder


class _OK:
    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [[0.0] * 8 for _ in texts]


class _Broken:
    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("connection refused")


def test_preflight_passes_for_working_embedder():
    check_embedder(_OK())  # no raise


def test_preflight_none_is_noop():
    check_embedder(None)  # no-RAG mode


def test_preflight_raises_clear_error_for_broken_embedder():
    with pytest.raises(EmbeddingError) as ei:
        check_embedder(_Broken())
    assert "connection refused" in str(ei.value)


# --- Ollama readiness -------------------------------------------------------

import httpx  # noqa: E402

from zotpilot.embeddings.ollama import OllamaEmbedder, ollama_status  # noqa: E402


def _tags(*names):
    request = httpx.Request("GET", "http://localhost:11434/api/tags")
    return httpx.Response(200, json={"models": [{"name": n} for n in names]}, request=request)


def _refuse(*_args, **_kwargs):
    raise httpx.ConnectError("[Errno 61] Connection refused")


def test_ollama_status_says_start_it_when_server_is_down(monkeypatch):
    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.get", _refuse)

    problem = ollama_status("http://localhost:11434", "bge-large")

    assert problem is not None
    assert "not running" in problem
    assert "ollama serve" in problem


def test_ollama_status_says_pull_it_when_model_is_missing(monkeypatch):
    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.get", lambda *a, **k: _tags("nomic-embed-text:latest"))

    problem = ollama_status("http://localhost:11434", "bge-large")

    assert problem is not None
    assert "ollama pull bge-large" in problem


@pytest.mark.parametrize("model", ["bge-large", "bge-large:latest"])
def test_ollama_status_accepts_an_untagged_or_latest_model(monkeypatch, model):
    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.get", lambda *a, **k: _tags("bge-large:latest"))

    assert ollama_status("http://localhost:11434", model) is None


def test_preflight_names_the_ollama_fix_before_embedding(monkeypatch):
    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.get", _refuse)

    def _must_not_embed(*_args, **_kwargs):
        raise AssertionError("embed probe ran although the readiness check failed")

    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.post", _must_not_embed)

    with pytest.raises(EmbeddingError) as ei:
        check_embedder(OllamaEmbedder())
    assert "Ollama is not running" in str(ei.value)
