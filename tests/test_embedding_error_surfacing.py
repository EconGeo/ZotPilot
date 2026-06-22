"""Embedding failures report the real cause, never UnboundLocalError on 'e'."""
import sys
import pytest
from unittest.mock import MagicMock

from zotpilot.embeddings.gemini import EmbeddingError, GeminiEmbedder
import zotpilot.embeddings.gemini as gemini_mod


def test_gemini_surfaces_real_api_error(monkeypatch):
    """UnboundLocalError must NOT appear; real cause (RuntimeError / 400) must appear."""
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
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    msg = str(ei.value)
    assert "UnboundLocalError" not in msg, f"Masked as UnboundLocalError: {msg}"
    assert "API key not valid" in msg or "400" in msg or "RuntimeError" in msg, (
        f"Real cause not surfaced in: {msg}"
    )


def test_gemini_error_chains_original_exception(monkeypatch):
    """The raised EmbeddingError must chain the original exception via __cause__."""
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

    mock_types = MagicMock()
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    # Check exception chaining
    assert ei.value.__cause__ is not None, "EmbeddingError should chain the original exception"
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_gemini_timeout_surfaces_timeout_error(monkeypatch):
    """TimeoutError path should not produce UnboundLocalError either."""
    import concurrent.futures
    monkeypatch.setattr(gemini_mod.time, "sleep", lambda _: None)

    mock_client = MagicMock()
    mock_client.models.embed_content.side_effect = concurrent.futures.TimeoutError()

    emb = GeminiEmbedder.__new__(GeminiEmbedder)
    emb.client = mock_client
    emb.model = "gemini-embedding-001"
    emb.dimensions = 768
    emb.timeout = 1.0
    emb.max_retries = 2

    mock_types = MagicMock()
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_types)

    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])

    msg = str(ei.value)
    assert "UnboundLocalError" not in msg, f"Masked as UnboundLocalError: {msg}"
