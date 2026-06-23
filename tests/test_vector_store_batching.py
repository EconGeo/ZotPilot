"""Tests for ChromaDB batch-size limiting in VectorStore insert methods."""

import pytest

from zotpilot.models import Chunk
from zotpilot.vector_store import VectorStore


@pytest.fixture
def store(tmp_path, mock_embedder):
    """Create a VectorStore with a real (but temporary) ChromaDB."""
    return VectorStore(tmp_path / "chroma", mock_embedder)


def _make_chunks(n: int) -> list[Chunk]:
    """Synthesize N minimal Chunk objects."""
    return [
        Chunk(
            text=f"chunk text {i}",
            chunk_index=i,
            page_num=1,
            char_start=i * 20,
            char_end=(i + 1) * 20,
            section="body",
            section_confidence=1.0,
        )
        for i in range(n)
    ]


class _RecordingCollection:
    """Drop-in replacement for store.collection that logs every add() call."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def add(self, *, ids, documents, embeddings, metadatas):
        self.calls.append(list(ids))


class TestChromaBatchSize:
    """Verify that _add_batched slices inserts under _max_add_batch."""

    # ------------------------------------------------------------------
    # Test 1 — 12 000 chunks: every call ≤ max_batch; ids preserved in order
    # ------------------------------------------------------------------
    def test_large_batch_sliced_correctly(self, store, mock_embedder):
        n = 12_000
        chunks = _make_chunks(n)
        doc_meta = {
            "title": "Big Book",
            "authors": "A. Author",
            "year": 2024,
            "citation_key": "book2024",
            "publication": "",
            "doi": "",
            "tags": "",
            "collections": "",
            "journal_quartile": "",
            "pdf_hash": "",
            "quality_grade": "",
        }

        # Swap collection for recording fake
        recorder = _RecordingCollection()
        store.collection = recorder

        store.add_chunks("BIG001", doc_meta, chunks)

        # Every individual call must be within the cap
        cap = store._max_add_batch
        for call_ids in recorder.calls:
            assert len(call_ids) <= cap, (
                f"add() called with {len(call_ids)} ids, exceeds cap {cap}"
            )

        # Concatenation of all recorded ids must equal the expected ids in order
        all_recorded = [chunk_id for call in recorder.calls for chunk_id in call]
        expected_ids = [f"BIG001_chunk_{i:04d}" for i in range(n)]
        assert all_recorded == expected_ids, "ids were dropped or reordered"

    # ------------------------------------------------------------------
    # Test 2 — edge cases on the slicing boundary (drive _add_batched directly)
    # ------------------------------------------------------------------
    def _run_direct(self, store, n: int) -> list[list[str]]:
        """Call _add_batched with n synthetic records; return the call log."""
        recorder = _RecordingCollection()
        store.collection = recorder

        ids = [f"id_{i}" for i in range(n)]
        documents = [f"doc {i}" for i in range(n)]
        embeddings = [[0.1] * 768 for _ in range(n)]
        metadatas = [{"chunk_type": "text"} for _ in range(n)]

        store._add_batched(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        return recorder.calls

    def test_edge_zero_records(self, store):
        calls = self._run_direct(store, 0)
        assert calls == [], "zero records must produce zero add() calls"

    def test_edge_exactly_max_batch(self, store):
        n = store._max_add_batch
        calls = self._run_direct(store, n)
        assert len(calls) == 1, (
            f"exactly _max_add_batch ({n}) records should produce exactly 1 call, "
            f"got {len(calls)}"
        )
        all_ids = [i for call in calls for i in call]
        assert all_ids == [f"id_{i}" for i in range(n)]

    def test_edge_max_batch_plus_one(self, store):
        n = store._max_add_batch + 1
        calls = self._run_direct(store, n)
        assert len(calls) == 2, (
            f"_max_add_batch+1 ({n}) records should produce exactly 2 calls, "
            f"got {len(calls)}"
        )
        all_ids = [i for call in calls for i in call]
        assert all_ids == [f"id_{i}" for i in range(n)]
