"""Embedding failures report the real cause, never UnboundLocalError on 'e'."""
import pytest
from unittest.mock import MagicMock

from zotpilot.embeddings.gemini import EmbeddingError, GeminiEmbedder


def _make_gemini_embedder(monkeypatch, side_effect, max_retries=2, timeout=1.0):
    """Create a GeminiEmbedder with a mocked google.genai client."""
    # Patch google.genai at the import level so GeminiEmbedder.__init__ uses it
    mock_genai = MagicMock()
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_client.models.embed_content.side_effect = side_effect
    monkeypatch.setattr("zotpilot.embeddings.gemini.time.sleep", lambda _: None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "google.genai",
        mock_genai,
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "google",
        MagicMock(genai=mock_genai),
    )
    # Also patch types so the import inside _embed_batch_with_timeout works
    mock_types = MagicMock()
    monkeypatch.setitem(
        __import__("sys").modules,
        "google.genai.types",
        mock_types,
    )

    emb = GeminiEmbedder.__new__(GeminiEmbedder)
    emb.client = mock_client
    emb.model = "gemini-embedding-001"
    emb.dimensions = 768
    emb.timeout = timeout
    emb.max_retries = max_retries

    # Also patch time.sleep in the gemini module to avoid real backoff
    import zotpilot.embeddings.gemini as gemini_mod
    monkeypatch.setattr(gemini_mod, "time", __import__("zotpilot.embeddings.gemini", fromlist=["time"]).time)
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda _: None)

    return emb


def test_gemini_surfaces_real_api_error(monkeypatch):
    """UnboundLocalError must NOT appear; real cause (RuntimeError / 400) must appear."""
    import zotpilot.embeddings.gemini as gemini_mod
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda _: None)

    real_error = RuntimeError("API key not valid: 400")

    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = real_error

    emb = GeminiEmbedder.__new__(GeminiEmbedder)
    emb.client = mock_client
    emb.model = "gemini-embedding-001"
    emb.dimensions = 768
    emb.timeout = 1.0
    emb.max_retries = 2

    # Patch google.genai.types so _embed_batch_with_timeout's `from google.genai import types` works
    mock_types = MagicMock()
    import sys
    sys.modules.setdefault("google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    msg = str(ei.value)
    assert "UnboundLocalError" not in msg, f"Masked as UnboundLocalError: {msg}"
    assert "API key not valid" in msg or "400" in msg or "RuntimeError" in msg, (
        f"Real cause not surfaced in: {msg}"
    )


def test_gemini_error_chains_original_exception(monkeypatch):
    """The raised EmbeddingError must chain the original exception via __cause__."""
    import zotpilot.embeddings.gemini as gemini_mod
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda _: None)

    real_error = RuntimeError("API key not valid: 400")

    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = real_error

    emb = GeminiEmbedder.__new__(GeminiEmbedder)
    emb.client = mock_client
    emb.model = "gemini-embedding-001"
    emb.dimensions = 768
    emb.timeout = 1.0
    emb.max_retries = 2

    import sys
    mock_types = MagicMock()
    sys.modules.setdefault("google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    # Check exception chaining
    assert ei.value.__cause__ is not None, "EmbeddingError should chain the original exception"
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_gemini_timeout_surfaces_timeout_error(monkeypatch):
    """TimeoutError path should not produce UnboundLocalError either."""
    import concurrent.futures
    import zotpilot.embeddings.gemini as gemini_mod
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda _: None)

    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = concurrent.futures.TimeoutError()

    emb = GeminiEmbedder.__new__(GeminiEmbedder)
    emb.client = mock_client
    emb.model = "gemini-embedding-001"
    emb.dimensions = 768
    emb.timeout = 1.0
    emb.max_retries = 2

    import sys
    mock_types = MagicMock()
    sys.modules.setdefault("google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    msg = str(ei.value)
    assert "UnboundLocalError" not in msg, f"Masked as UnboundLocalError: {msg}"
