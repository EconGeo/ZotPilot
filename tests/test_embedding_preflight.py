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
