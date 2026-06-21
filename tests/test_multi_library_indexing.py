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
