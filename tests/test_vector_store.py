"""Tests for ChromaDB vector store."""

from unittest.mock import patch

import chromadb
import pytest
from chromadb.config import Settings

from zotpilot.vector_store import (
    ChromaStoreUnopenableError,
    VectorStore,
    _probe_chroma_db_access,
)


@pytest.fixture
def store(tmp_path, mock_embedder):
    """Create a VectorStore with in-memory ChromaDB."""
    return VectorStore(tmp_path / "chroma", mock_embedder)


@pytest.fixture
def populated_store(store, sample_chunks):
    """Store with some chunks added."""
    doc_meta = {
        "title": "Test Paper",
        "authors": "Test Author",
        "year": 2020,
        "citation_key": "test2020",
        "publication": "Test Journal",
        "doi": "10.1234/test",
        "tags": "ml; ai",
        "collections": "Test Collection",
        "journal_quartile": "Q1",
        "pdf_hash": "abc123",
        "quality_grade": "A",
    }
    store.add_chunks("TEST001", doc_meta, sample_chunks)
    return store


class TestVectorStore:
    def test_add_and_count(self, populated_store):
        assert populated_store.count() == 3

    def test_get_indexed_doc_ids(self, populated_store):
        ids = populated_store.get_indexed_doc_ids()
        assert "TEST001" in ids

    def test_delete_document(self, populated_store):
        populated_store.delete_document("TEST001")
        assert populated_store.count() == 0

    def test_get_document_meta(self, populated_store):
        meta = populated_store.get_document_meta("TEST001")
        assert meta is not None
        assert meta["doc_title"] == "Test Paper"
        assert meta["year"] == 2020

    def test_get_document_meta_not_found(self, store):
        meta = store.get_document_meta("NONEXISTENT")
        assert meta is None

    def test_get_adjacent_chunks(self, populated_store):
        chunks = populated_store.get_adjacent_chunks("TEST001", 1, window=1)
        assert len(chunks) >= 1
        # Should include the center and at least one neighbor
        indices = {c.metadata["chunk_index"] for c in chunks}
        assert 1 in indices

    def test_search(self, populated_store):
        results = populated_store.search("neural networks", top_k=3)
        assert len(results) <= 3
        for r in results:
            assert r.score >= 0

    def test_empty_store_search(self, store):
        results = store.search("anything", top_k=5)
        assert results == []

    def test_unopenable_db_fails_loudly_and_is_untouched(self, tmp_path, mock_embedder):
        db_path = tmp_path / "chroma"
        db_path.mkdir()
        (db_path / "chroma.sqlite3").write_text("broken")

        with (
            patch("zotpilot.vector_store._probe_chroma_db_access", return_value=False),
            pytest.raises(ChromaStoreUnopenableError),
        ):
            VectorStore(db_path, mock_embedder)

        assert (db_path / "chroma.sqlite3").read_text() == "broken"
        assert list(tmp_path.glob("chroma.corrupt-*")) == []


def _collection_names(db_path) -> set[str]:
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    return {c.name for c in client.list_collections()}


class TestProbeConfiguredCollection:
    def test_probe_does_not_create_default_collection(self, tmp_path, mock_embedder):
        VectorStore(tmp_path / "chroma", mock_embedder, collection_name="other")

        assert _probe_chroma_db_access(tmp_path / "chroma", "other") is True

        names = _collection_names(tmp_path / "chroma")
        assert names == {"other"}

    def test_probe_accepts_store_without_the_collection_yet(self, tmp_path, mock_embedder):
        VectorStore(tmp_path / "chroma", mock_embedder, collection_name="other")

        assert _probe_chroma_db_access(tmp_path / "chroma", "not_yet_created") is True

        names = _collection_names(tmp_path / "chroma")
        assert names == {"other"}
