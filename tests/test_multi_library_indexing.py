"""Tests for multi-library indexing orchestration."""
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from zotpilot.indexer import enumerate_indexable_libraries, global_pdf_doc_ids


def _make_db(tmp_path):
    """User library (1) + one group (groupID 100 -> libraryID 2), each with one PDF item."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
            dateAdded TEXT DEFAULT '2024-01-01', key TEXT UNIQUE,
            libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title'), (7, 'date'), (8, 'publicationTitle'), (9, 'DOI');
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY, collectionName TEXT,
            parentCollectionID INTEGER, key TEXT UNIQUE, libraryID INTEGER DEFAULT 1
        );
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER, orderIndex INTEGER DEFAULT 0);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER,
            contentType TEXT, linkMode INTEGER, path TEXT
        );
        CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, note TEXT);
        CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT NOT NULL,
                            name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', version INT NOT NULL DEFAULT 0);
        CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT NOT NULL,
                               editable INT NOT NULL DEFAULT 1, filesEditable INT NOT NULL DEFAULT 1,
                               version INT NOT NULL DEFAULT 0, storageVersion INT NOT NULL DEFAULT 0,
                               lastSync INT NOT NULL DEFAULT 0, archived INT NOT NULL DEFAULT 0);
        INSERT INTO libraries VALUES (1, 'user', 1, 1, 0, 0, 0, 0);
        INSERT INTO libraries VALUES (2, 'group', 1, 1, 0, 0, 0, 0);
        INSERT INTO groups VALUES (100, 2, 'Lab Group', '', 0);
    """)
    storage = tmp_path / "storage"
    # Parent item + stored PDF attachment per library. linkMode 0 = imported_file.
    def add_pdf_item(item_id, key, library_id, att_id, att_key):
        conn.execute("INSERT INTO items VALUES (?, 2, '2024-01-01', ?, ?)", (item_id, key, library_id))
        conn.execute("INSERT INTO items VALUES (?, 3, '2024-01-01', ?, ?)", (att_id, att_key, library_id))
        conn.execute(
            "INSERT INTO itemAttachments VALUES (?, ?, 'application/pdf', 0, ?)",
            (att_id, item_id, f"storage:{att_key}.pdf"),
        )
        pdf_dir = storage / att_key
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / f"{att_key}.pdf").write_bytes(b"%PDF-1.4 test")
    add_pdf_item(1, "USERAAAA", 1, 2, "ATTUSER1")
    add_pdf_item(3, "GRPBBBBB", 2, 4, "ATTGRP01")
    conn.commit()
    conn.close()
    return tmp_path


@dataclass
class _Cfg:
    zotero_data_dir: Path


def test_enumerate_indexable_libraries_lists_user_and_group(tmp_path):
    data_dir = _make_db(tmp_path)
    libs = enumerate_indexable_libraries(_Cfg(zotero_data_dir=data_dir))
    lib_ids = {lib_id for lib_id, _name in libs}
    assert 1 in lib_ids          # user library (SQLite libraryID 1)
    assert 2 in lib_ids          # group resolved groupID 100 -> SQLite libraryID 2
    assert libs[0][0] == 1       # user library first


def test_global_pdf_doc_ids_unions_all_libraries(tmp_path):
    data_dir = _make_db(tmp_path)
    ids = global_pdf_doc_ids(_Cfg(zotero_data_dir=data_dir))
    assert ids == {"USERAAAA", "GRPBBBBB"}


from zotpilot.indexer import index_all_libraries


class _FakeIndexer:
    """Stand-in for Indexer that records protected_doc_ids and never touches Chroma."""
    instances = []

    def __init__(self, config, library_id=None):
        self.library_id = library_id if library_id is not None else 1
        self.captured = None
        _FakeIndexer.instances.append(self)

    def index_all(self, **kwargs):
        self.captured = kwargs
        # Library 1 indexes 1 doc with more pending; group library is fully done.
        if self.library_id == 1:
            return {"results": ["r1"], "indexed": 1, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "has_more": True,
                    "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}
        return {"results": ["r2"], "indexed": 1, "failed": 0, "empty": 0,
                "skipped": 0, "already_indexed": 5, "has_more": False,
                "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}


def test_index_all_libraries_protects_global_union(tmp_path, monkeypatch):
    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _FakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _FakeIndexer)

    result = index_all_libraries(cfg, batch_size=None)

    # Every per-library call must receive the FULL union as protected_doc_ids.
    for inst in _FakeIndexer.instances:
        assert inst.captured["protected_doc_ids"] == {"USERAAAA", "GRPBBBBB"}
    # Aggregated counts sum across libraries.
    assert result["indexed"] == 2
    assert result["already_indexed"] == 5
    assert result["results"] == ["r1", "r2"]


def test_index_all_libraries_batch_reports_aggregate_has_more(tmp_path, monkeypatch):
    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _FakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _FakeIndexer)

    # Library 1 (first) reports has_more=True -> aggregate must be True.
    result = index_all_libraries(cfg, batch_size=2)
    assert result["has_more"] is True


def test_index_all_libraries_batch_exhaustion_skips_unvisited_library(tmp_path, monkeypatch):
    """Test that budget depletion at loop top skips unvisited libraries and sets has_more=True."""

    class _BudgetFakeIndexer:
        """Fake indexer that reports has_more=False with indexed count == batch_size."""
        instances = []

        def __init__(self, config, library_id=None):
            self.library_id = library_id if library_id is not None else 1
            self.captured = None
            _BudgetFakeIndexer.instances.append(self)

        def index_all(self, **kwargs):
            self.captured = kwargs
            # Library 1 indexes exactly batch_size (2) docs with no more pending.
            # This depletes budget to 0, so library 2 is never visited.
            if self.library_id == 1:
                return {"results": ["r1", "r2"], "indexed": 2, "failed": 0, "empty": 0,
                        "skipped": 0, "already_indexed": 0, "has_more": False,
                        "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}
            # Library 2 should never be reached, but return a default just in case.
            return {"results": ["r3"], "indexed": 1, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "has_more": False,
                    "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}

    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _BudgetFakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _BudgetFakeIndexer)

    # batch_size=2; library 1 indexes exactly 2 docs with has_more=False.
    # This exhausts budget to 0, triggering the `budget <= 0` check at loop top
    # before library 2 is visited.
    result = index_all_libraries(cfg, batch_size=2)

    # Only library 1 should have been instantiated.
    assert len(_BudgetFakeIndexer.instances) == 1
    # Budget exhaustion must set has_more=True (aggregated).
    assert result["has_more"] is True
    # Verify the aggregated count from library 1 only.
    assert result["indexed"] == 2


def test_index_all_libraries_does_not_stall_on_fully_indexed_first_library(tmp_path, monkeypatch):
    """A fully-indexed lib1 (has_more=True, indexed=0) must NOT starve lib2."""

    class _StallFakeIndexer:
        instances = []

        def __init__(self, config, library_id=None):
            self.library_id = library_id if library_id is not None else 1
            self.captured = None
            _StallFakeIndexer.instances.append(self)

        def index_all(self, **kwargs):
            self.captured = kwargs
            if self.library_id == 1:
                # Already fully indexed: has_more=True but zero new work done.
                return {"results": [], "indexed": 0, "failed": 0, "empty": 0,
                        "skipped": 0, "already_indexed": 50, "has_more": True,
                        "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}
            # Group library: has real work.
            return {"results": ["r2"], "indexed": 1, "failed": 0, "empty": 0,
                    "skipped": 0, "already_indexed": 0, "has_more": False,
                    "skipped_long": 0, "long_documents": [], "skipped_no_pdf": []}

    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _StallFakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _StallFakeIndexer)

    result = index_all_libraries(cfg, batch_size=5)

    # Both libraries must have been visited — lib1 must NOT starve lib2.
    assert len(_StallFakeIndexer.instances) == 2
    # Only lib2's work counts (lib1 made no progress).
    assert result["indexed"] == 1


from zotpilot.index_authority import reconcile_orphaned_index_docs


class _FakeStore:
    def __init__(self, doc_ids):
        self._ids = set(doc_ids)
        self.deleted = []

    def get_indexed_doc_ids(self):
        return set(self._ids)

    def delete_document(self, doc_id):
        self.deleted.append(doc_id)
        self._ids.discard(doc_id)


def test_reconcile_with_union_keeps_other_library_docs():
    # Store holds docs from library A (USERAAAA) and library B (GRPBBBBB).
    store = _FakeStore({"USERAAAA", "GRPBBBBB"})
    union = {"USERAAAA", "GRPBBBBB"}  # global union -> nothing is orphaned

    result = reconcile_orphaned_index_docs(store, union)

    assert result["deleted_count"] == 0
    assert store.deleted == []
    assert store.get_indexed_doc_ids() == {"USERAAAA", "GRPBBBBB"}


def test_reconcile_without_union_would_delete_other_library_docs():
    # Demonstrates the ORIGINAL bug: reconciling against only library A's docs
    # deletes library B's doc. This documents why the union is required.
    store = _FakeStore({"USERAAAA", "GRPBBBBB"})
    only_library_a = {"USERAAAA"}

    result = reconcile_orphaned_index_docs(store, only_library_a)

    assert "GRPBBBBB" in result["orphaned_doc_ids"]
    assert store.deleted == ["GRPBBBBB"]


import zotpilot.tools.indexing as indexing_mod


def test_collect_unindexed_papers_spans_all_libraries(tmp_path, monkeypatch):
    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)

    # Store already has the user-library doc indexed; group doc is not.
    store = _FakeStore({"USERAAAA"})
    monkeypatch.setattr(indexing_mod, "_get_config", lambda: cfg)
    monkeypatch.setattr(indexing_mod, "_get_store", lambda: store)

    papers, total = indexing_mod._collect_unindexed_papers()

    doc_ids = {p["doc_id"] for p in papers}
    assert "GRPBBBBB" in doc_ids       # group-library unindexed item is surfaced
    assert "USERAAAA" not in doc_ids   # already-indexed user item excluded
    assert total == 1
