"""Unit tests for issue #15 — embedding 429/quota classification.

Covers parse_retry_delay shapes, the exception hierarchy & backward-compat
re-exports, the Gemini/DashScope 429 classifiers, the DashScope no-degrade
guard, and the embed-before-add ordering invariant in VectorStore.
"""
from unittest.mock import MagicMock

import pytest


class TestParseRetryDelay:
    def test_gemini_retry_delay_double_quote(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay('retryDelay: "42s"') == 42.0

    def test_gemini_dict_repr_single_quote(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("{'retryDelay': '42s'}") == 42.0

    def test_retry_after_field(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("Retry-After: 42") == 42.0

    def test_retry_after_json_field(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay('"retry_after": 42') == 42.0

    def test_bare_header_seconds(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("42") == 42.0
        assert parse_retry_delay("42.5") == 42.5

    def test_realistic_client_error_details_payload(self):
        """HIGH regression: a stringified ClientError.details must yield 42.0,
        proving e.details/str(e) parsing rather than e.message (which drops it)."""
        from zotpilot.embeddings.base import parse_retry_delay
        payload = (
            "{'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', "
            "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
            "'retryDelay': '42s'}]}}"
        )
        assert parse_retry_delay(payload) == 42.0

    def test_no_hint_returns_none(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("quota exceeded, please slow down") is None

    def test_garbage_returns_none(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("@@@!!!") is None

    def test_none_and_empty(self):
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay(None) is None
        assert parse_retry_delay("") is None

    def test_http_date_retry_after_is_none(self):
        """An HTTP-date Retry-After is unparseable here — None is acceptable."""
        from zotpilot.embeddings.base import parse_retry_delay
        assert parse_retry_delay("Wed, 21 Oct 2099 07:28:00 GMT") is None


class TestExceptionHierarchy:
    def test_rate_limit_is_embedding_error(self):
        from zotpilot.embeddings.base import EmbeddingError, RateLimitError
        assert issubclass(RateLimitError, EmbeddingError)

    def test_rate_limit_carries_provider_and_retry_after(self):
        from zotpilot.embeddings.base import RateLimitError
        e = RateLimitError("boom", provider="gemini", retry_after=42.0)
        assert e.provider == "gemini"
        assert e.retry_after == 42.0

    def test_rate_limit_retry_after_defaults_none(self):
        from zotpilot.embeddings.base import RateLimitError
        e = RateLimitError("boom", provider="dashscope")
        assert e.retry_after is None

    def test_backward_compat_imports(self):
        """AC10: EmbeddingError importable from gemini, package root, and base;
        RateLimitError from package root and base."""
        from zotpilot.embeddings import EmbeddingError as IE
        from zotpilot.embeddings import RateLimitError as IR
        from zotpilot.embeddings.base import EmbeddingError as BE
        from zotpilot.embeddings.base import RateLimitError as BR
        from zotpilot.embeddings.gemini import EmbeddingError as GE
        from zotpilot.embeddings.gemini import RateLimitError as GR
        assert IE is BE is GE
        assert IR is BR is GR


class TestGeminiClassifier:
    def _make_embedder(self, client):
        from zotpilot.embeddings.gemini import GeminiEmbedder
        emb = GeminiEmbedder.__new__(GeminiEmbedder)
        emb.model = "gemini-embedding-001"
        emb.dimensions = 768
        emb.timeout = 5.0
        emb.max_retries = 3
        emb.client = client
        return emb

    def _client_error(self, *, status="RESOURCE_EXHAUSTED"):
        from google.genai.errors import ClientError
        rj = {
            "error": {
                "code": 429,
                "status": status,
                "message": "Quota exceeded",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "42s"}
                ],
            }
        }
        return ClientError(429, rj)

    def test_429_raises_rate_limit_with_retry_after(self):
        from zotpilot.embeddings.base import RateLimitError
        client = MagicMock()
        client.models.embed_content.side_effect = self._client_error()
        emb = self._make_embedder(client)
        with pytest.raises(RateLimitError) as ei:
            emb._embed_batch_with_timeout(["t"], "RETRIEVAL_DOCUMENT", 1, 1)
        assert ei.value.provider == "gemini"
        assert ei.value.retry_after == 42.0
        # Must NOT retry the dead quota 3x.
        assert client.models.embed_content.call_count == 1

    def test_status_branch_triggers_without_code_429(self):
        from zotpilot.embeddings.base import RateLimitError
        err = self._client_error()
        err.code = 0  # force only the status check to match
        client = MagicMock()
        client.models.embed_content.side_effect = err
        emb = self._make_embedder(client)
        with pytest.raises(RateLimitError):
            emb._embed_batch_with_timeout(["t"], "RETRIEVAL_DOCUMENT", 1, 1)


class TestDashScopeNoDegrade:
    def _make_embedder(self):
        from zotpilot.embeddings.dashscope import DashScopeEmbedder
        emb = DashScopeEmbedder.__new__(DashScopeEmbedder)
        emb.model = "text-embedding-v4"
        emb.batch_size = 5
        emb.max_input_chars = 6000
        emb.endpoint = "compatible"
        return emb

    def test_document_batch_429_not_degraded_to_single_text(self):
        """AC8: a 429'd RETRIEVAL_DOCUMENT batch must propagate, NOT fan out
        into single-text retries."""
        from zotpilot.embeddings.base import RateLimitError
        emb = self._make_embedder()
        calls = {"n": 0}

        def fake_embed_batch(batch, batch_num, total_batches, task_type="RETRIEVAL_DOCUMENT"):
            calls["n"] += 1
            raise RateLimitError("rl", provider="dashscope", retry_after=10.0)

        emb._embed_batch = fake_embed_batch
        with pytest.raises(RateLimitError):
            emb.embed(["a", "b", "c", "d", "e"], task_type="RETRIEVAL_DOCUMENT")
        assert calls["n"] == 1  # exactly one batch call; no 5 single-text retries

    def test_generic_failure_still_degrades(self):
        """Control: a non-RateLimitError still falls back to single-text retries."""
        from zotpilot.embeddings.base import EmbeddingError
        emb = self._make_embedder()
        calls = {"n": 0}

        def fake_embed_batch(batch, batch_num, total_batches, task_type="RETRIEVAL_DOCUMENT"):
            calls["n"] += 1
            if len(batch) > 1:
                raise EmbeddingError("transient")
            return [[0.0]]  # single-text retries succeed

        emb._embed_batch = fake_embed_batch
        out = emb.embed(["a", "b", "c", "d", "e"], task_type="RETRIEVAL_DOCUMENT")
        assert len(out) == 5
        assert calls["n"] == 1 + 5  # 1 failed batch + 5 single-text retries


class TestOrderingInvariantVectorStore:
    """R1: VectorStore.add_chunks embeds BEFORE collection.add — a 429 during
    embedding must leave zero chunks persisted (collection.add never called)."""

    def test_embed_raises_before_collection_add(self):
        from zotpilot.embeddings.base import RateLimitError
        from zotpilot.models import Chunk
        from zotpilot.vector_store import VectorStore

        vs = VectorStore.__new__(VectorStore)
        vs.embedder = MagicMock()
        vs.embedder.embed.side_effect = RateLimitError("rl", provider="gemini")
        vs.collection = MagicMock()

        chunks = [
            Chunk(text="hello", chunk_index=0, page_num=1, char_start=0, char_end=5,
                  section="intro", section_confidence=1.0),
        ]
        with pytest.raises(RateLimitError):
            vs.add_chunks("DOC1", {"title": "t"}, chunks)
        vs.collection.add.assert_not_called()
