# Multi-Library Indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ZotPilot index every Zotero library (personal + all groups) by default, durably, so no index run can silently delete another library's indexed documents.

**Architecture:** Add module-level helpers (`enumerate_indexable_libraries`, `global_pdf_doc_ids`) and an `index_all_libraries` orchestrator in `indexer.py`. The orchestrator computes one authoritative union of all PDF doc_ids across libraries and passes it as `protected_doc_ids` to each per-library `Indexer.index_all` call, so reconciliation only deletes docs absent from *every* library. The CLI and MCP entry points call the orchestrator instead of a single-library `Indexer`. Stats count unindexed across all libraries.

**Tech Stack:** Python 3, pytest, SQLite (Zotero DB), ChromaDB (vector store).

## Global Constraints

- Default index scope = **all libraries** (user + groups), on both CLI and MCP.
- `max_pages` default stays **40**; long books remain skipped (no behavior change).
- Protected set is derived from Zotero SQLite (the global union), never from current store state — guarantees safety under any iteration order or partial/batched run.
- SQLite `libraryID` is what `ZoteroClient`/`Indexer` take; group `groupID` must be resolved via `ZoteroClient.resolve_group_library_id(data_dir, group_id)`.
- Follow existing test style: build multi-library SQLite fixtures like `_create_multi_library_db` in `tests/test_library_filter.py`.
- Run tests from repo root: `cd /Users/andrew.mueller/Projects/ZotPilot`.

---

### Task 1: Library enumeration + global doc-id union helpers

**Files:**
- Modify: `src/zotpilot/indexer.py` (add two module-level functions near the top, after imports, before `class Indexer`)
- Test: `tests/test_multi_library_indexing.py` (create)

**Interfaces:**
- Consumes: `ZoteroClient(data_dir, library_id)`, `ZoteroClient.get_libraries() -> list[dict]` (each `{"library_id": str, "library_type": "user"|"group", "name": str, "item_count": int}`), `ZoteroClient.resolve_group_library_id(data_dir, group_id) -> int`, `index_authority.current_library_pdf_doc_ids(zotero) -> set[str]`.
- Produces:
  - `enumerate_indexable_libraries(config) -> list[tuple[int, str]]` — `(sqlite_library_id, label)`, user library first as `(1, name)`.
  - `global_pdf_doc_ids(config) -> set[str]` — union of PDF item keys across all libraries.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_multi_library_indexing.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -v`
Expected: FAIL with `ImportError: cannot import name 'enumerate_indexable_libraries'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/zotpilot/indexer.py` at module level (after imports, before `class Indexer`):

```python
def enumerate_indexable_libraries(config) -> list[tuple[int, str]]:
    """Return (sqlite_library_id, label) for the user library plus every group.

    User library is always first as (1, name). Group ``library_id`` values from
    ``get_libraries()`` are Zotero groupIDs and are resolved to SQLite libraryIDs.
    """
    from .zotero_client import ZoteroClient

    zc = ZoteroClient(config.zotero_data_dir)
    libs: list[tuple[int, str]] = []
    groups: list[tuple[int, str]] = []
    for lib in zc.get_libraries():
        if lib["library_type"] == "user":
            libs.append((1, lib["name"]))
        else:
            sqlite_id = ZoteroClient.resolve_group_library_id(
                config.zotero_data_dir, int(lib["library_id"])
            )
            groups.append((sqlite_id, lib["name"]))
    return libs + groups


def global_pdf_doc_ids(config) -> set[str]:
    """Union of Zotero item keys with resolved PDF files across all libraries."""
    from .zotero_client import ZoteroClient
    from .index_authority import current_library_pdf_doc_ids

    ids: set[str] = set()
    for lib_id, _label in enumerate_indexable_libraries(config):
        zc = ZoteroClient(config.zotero_data_dir, library_id=lib_id)
        ids |= current_library_pdf_doc_ids(zc)
    return ids
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): library enumeration + global PDF doc-id union helpers"
```

---

### Task 2: `index_all_libraries` orchestrator (reconciliation safety + batch threading)

**Files:**
- Modify: `src/zotpilot/indexer.py` (add `index_all_libraries` after the Task 1 helpers)
- Test: `tests/test_multi_library_indexing.py` (append)

**Interfaces:**
- Consumes: `Indexer(config, library_id=N)`, `Indexer.index_all(force_reindex, limit, item_key, item_keys, title_pattern, max_pages, batch_size, journal, protected_doc_ids) -> dict` (dict keys include `results: list`, `indexed`, `failed`, `empty`, `skipped`, `already_indexed`, `has_more`, `skipped_long`, `long_documents`, `skipped_no_pdf`); `enumerate_indexable_libraries`, `global_pdf_doc_ids` (Task 1).
- Produces: `index_all_libraries(config, *, force_reindex=False, limit=None, item_key=None, item_keys=None, title_pattern=None, max_pages=0, batch_size=None, journal=None) -> dict` returning aggregated `results` + summed counts + combined `has_more`.

Behavior contract:
- `protected_doc_ids` passed to every per-library `index_all` is the **full** `global_pdf_doc_ids(config)` union → a library never deletes another library's docs, even when not visited this call.
- `batch_size=None` → full sweep of all libraries (CLI default); aggregate `has_more=False` unless a library reports more.
- `batch_size=N` → thread budget across libraries; `has_more=True` if any visited library reported `has_more` or the budget ran out before a library was visited.
- `limit`, `item_key`, `item_keys`, `title_pattern` are passed through unchanged to each per-library call.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_library_indexing.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -k index_all_libraries -v`
Expected: FAIL with `ImportError: cannot import name 'index_all_libraries'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/zotpilot/indexer.py` after the Task 1 helpers:

```python
def index_all_libraries(
    config,
    *,
    force_reindex: bool = False,
    limit: int | None = None,
    item_key: str | None = None,
    item_keys: list[str] | None = None,
    title_pattern: str | None = None,
    max_pages: int = 0,
    batch_size: int | None = None,
    journal=None,
) -> dict:
    """Index every Zotero library (user + groups), protecting all libraries' docs.

    Passes the full cross-library PDF doc-id union as ``protected_doc_ids`` to each
    per-library ``Indexer.index_all`` so reconciliation only removes docs absent
    from every library. Threads ``batch_size`` as a budget across libraries.
    """
    union = global_pdf_doc_ids(config)
    libraries = enumerate_indexable_libraries(config)

    agg_results: list = []
    summed = {"indexed": 0, "failed": 0, "empty": 0, "skipped": 0,
              "already_indexed": 0, "skipped_long": 0}
    long_documents: list = []
    skipped_no_pdf: list = []
    has_more = False
    budget = batch_size  # None => unlimited per library

    for lib_id, _label in libraries:
        if budget is not None and budget <= 0:
            has_more = True  # ran out before visiting this library
            break

        res = Indexer(config, library_id=lib_id).index_all(
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_pattern=title_pattern,
            max_pages=max_pages,
            batch_size=budget,
            journal=journal,
            protected_doc_ids=union,
        )

        agg_results.extend(res.get("results", []))
        for k in summed:
            summed[k] += res.get(k, 0)
        long_documents.extend(res.get("long_documents", []))
        skipped_no_pdf.extend(res.get("skipped_no_pdf", []))

        if res.get("has_more"):
            has_more = True
            break  # this library filled the batch; resume here on next call

        if budget is not None:
            budget -= res.get("indexed", 0) + res.get("failed", 0) + res.get("empty", 0)

    out = {"results": agg_results, "has_more": has_more}
    out.update(summed)
    out["long_documents"] = long_documents
    out["skipped_no_pdf"] = skipped_no_pdf
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): index_all_libraries orchestrator with global protected set"
```

---

### Task 3: Reconciliation regression test (the bug that matters)

**Files:**
- Test: `tests/test_multi_library_indexing.py` (append)

**Interfaces:**
- Consumes: `index_authority.reconcile_orphaned_index_docs(store, current_doc_ids) -> dict`. Proves that reconciling library A with the global union as the protected/current set does not delete library B's docs.

This task is pure test — it locks the safety invariant at the reconciliation layer with a real (fake) store, independent of the orchestrator.

- [ ] **Step 1: Write the failing test (guards against regression even though it should pass once the union is correct)**

Append to `tests/test_multi_library_indexing.py`:

```python
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
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -k reconcile -v`
Expected: PASS (both — the first proves safety with the union, the second documents the bug the union prevents).

- [ ] **Step 3: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add tests/test_multi_library_indexing.py
git commit -m "test(indexer): lock reconciliation safety invariant for multi-library"
```

---

### Task 4: Wire CLI `cmd_index` to the orchestrator

**Files:**
- Modify: `src/zotpilot/cli.py:355-363` (the `indexer = Indexer(config)` / `indexer.index_all(...)` block in `cmd_index`)

**Interfaces:**
- Consumes: `index_all_libraries(config, ...)` (Task 2). Returns the same summary keys `cmd_index` already prints (`indexed`, `already_indexed`, `skipped`, `failed`, `empty`, `quality_distribution`).

- [ ] **Step 1: Replace the single-library call**

In `src/zotpilot/cli.py`, change the import at the top of `cmd_index` and the call block. Replace:

```python
    indexer = Indexer(config)
    result = indexer.index_all(
        force_reindex=args.force,
        limit=args.limit,
        item_key=args.item_key,
        title_pattern=args.title,
        max_pages=max_pages,
        batch_size=batch_size,
    )
```

with:

```python
    from .indexer import index_all_libraries
    result = index_all_libraries(
        config,
        force_reindex=args.force,
        limit=args.limit,
        item_key=args.item_key,
        title_pattern=args.title,
        max_pages=max_pages,
        batch_size=batch_size,
    )
```

(The `from .indexer import Indexer` line at the top of `cmd_index` may remain; it is harmless. `result.get("quality_distribution")` is already guarded with `.get`, so its absence from the aggregate is fine.)

- [ ] **Step 2: Smoke-test the CLI wiring against the real library (dry, tiny batch)**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && zotpilot index --limit 0 2>&1 | head -20`
Expected: prints "Indexing complete:" with summary lines and no traceback. (`--limit 0` performs enumeration/reconciliation without embedding new docs.)

- [ ] **Step 3: Run the full test suite for regressions**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_cli_config.py tests/test_indexer.py tests/test_multi_library_indexing.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/cli.py
git commit -m "feat(cli): index all libraries by default via index_all_libraries"
```

---

### Task 5: Wire MCP `index_library` to the orchestrator

**Files:**
- Modify: `src/zotpilot/tools/indexing.py:255-266` (the `indexer = Indexer(config)` / `indexer.index_all(...)` block inside `index_library`)

**Interfaces:**
- Consumes: `index_all_libraries(config, ...)` (Task 2). Must preserve the existing `journal` argument and the downstream serialization of `result["results"]` (each item has `.item_key`, `.title`, `.status`, `.reason`, `.n_chunks`, `.n_tables`, `.quality_grade`).

- [ ] **Step 1: Replace the single-library call**

In `src/zotpilot/tools/indexing.py`, replace:

```python
        indexer = Indexer(config)
        result = indexer.index_all(
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_pattern=title_pattern,
            max_pages=effective_max_pages,
            batch_size=batch_size if batch_size > 0 else None,
            journal=journal,
        )
```

with:

```python
        from ..indexer import index_all_libraries
        result = index_all_libraries(
            config,
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_pattern=title_pattern,
            max_pages=effective_max_pages,
            batch_size=batch_size if batch_size > 0 else None,
            journal=journal,
        )
```

(Leave the `from ..indexer import Indexer` import already present in the function; it is harmless.)

- [ ] **Step 2: Verify serialization keys still resolve**

The aggregated `result["results"]` is a list of the same `IndexResult` objects each per-library `index_all` produced, so the existing serialization loop (`r.item_key`, `r.title`, `r.status`, ...) is unchanged. Confirm by reading `src/zotpilot/tools/indexing.py:271-286`.

- [ ] **Step 3: Run indexing + batch-defaults tests**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_indexing_batch_defaults.py tests/test_multi_library_indexing.py -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/tools/indexing.py
git commit -m "feat(mcp): index_library covers all libraries via index_all_libraries"
```

---

### Task 6: Multi-library stats (`_collect_unindexed_papers`)

**Files:**
- Modify: `src/zotpilot/tools/indexing.py:39-62` (`_collect_unindexed_papers`)
- Test: `tests/test_multi_library_indexing.py` (append)

**Interfaces:**
- Consumes: `global_pdf_doc_ids(config)`, `enumerate_indexable_libraries(config)` (Task 1); `index_authority.authoritative_indexed_doc_ids(store, current_doc_ids) -> set[str]`; `_get_config()`, `_get_store()`, `ZoteroClient`.
- Produces: `_collect_unindexed_papers(limit, offset) -> (list[dict], int)` counting unindexed PDF items across **all** libraries.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_multi_library_indexing.py`:

```python
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
```

Note: `_FakeStore` lacks a `db_path` attribute, so `authoritative_indexed_doc_ids` returns the raw intersection (`union & stored`) — exactly the behavior this test asserts.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -k collect_unindexed -v`
Expected: FAIL — current implementation uses `_get_zotero()` (single library) so `GRPBBBBB` is absent / `total` wrong.

- [ ] **Step 3: Rewrite `_collect_unindexed_papers`**

Replace the body of `_collect_unindexed_papers` in `src/zotpilot/tools/indexing.py` with:

```python
def _collect_unindexed_papers(limit: int | None = None, offset: int = 0) -> tuple[list[dict], int]:
    """Return unindexed Zotero papers across all libraries and their total count."""
    from ..indexer import enumerate_indexable_libraries, global_pdf_doc_ids
    from ..zotero_client import ZoteroClient

    config = _get_config()
    union = global_pdf_doc_ids(config)
    indexed_set = authoritative_indexed_doc_ids(_get_store(), union)

    papers: list[dict] = []
    total = 0
    for lib_id, _label in enumerate_indexable_libraries(config):
        zotero = ZoteroClient(config.zotero_data_dir, library_id=lib_id)
        for item in zotero.get_all_items_with_pdfs():
            if item.item_key in indexed_set:
                continue
            total += 1
            if total <= offset:
                continue
            if limit is not None and len(papers) >= limit:
                continue
            papers.append(
                {
                    "doc_id": item.item_key,
                    "title": item.title or "(no title)",
                    "year": item.year,
                    "authors": item.authors,
                }
            )
    return papers, total
```

Confirm the existing imports at the top of `tools/indexing.py` already include `authoritative_indexed_doc_ids` (it is used by the current implementation). If not, add `from ..index_authority import authoritative_indexed_doc_ids`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/tools/indexing.py tests/test_multi_library_indexing.py
git commit -m "feat(stats): count unindexed papers across all libraries"
```

---

### Task 7: Full-suite regression check + execution of the real index

**Files:** none (verification + operational run)

- [ ] **Step 1: Run the full test suite**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest -q`
Expected: PASS (no regressions). If pre-existing unrelated failures appear, note them but do not fix in this plan.

- [ ] **Step 2: Re-check stats now span all libraries**

Use the MCP `get_index_stats` tool (or `zotpilot` equivalent). Expected: `unindexed_count` now includes group-library PDFs (≈ prior 305 + group items), confirming multi-library visibility.

- [ ] **Step 3: Run the real multi-library index**

Use the MCP `index_library` tool with `batch_size` ~10, repeating until `has_more=false` (or `zotpilot index` for a single full sweep). This indexes the 4 group libraries (~493 article-length PDFs after the 40-page skip) and any remaining indexable user-library items.

- [ ] **Step 4: Confirm durability**

Run `get_index_stats` once more, then run `index_library` again (one batch). Expected: the second run reports the group docs as `already_indexed` and deletes nothing — proving the protected-union fix holds.

---

## Self-Review

**Spec coverage:**
- Global union protected set → Tasks 1, 2, 3. ✓
- `enumerate_indexable_libraries` / `global_pdf_doc_ids` → Task 1. ✓
- `index_all_libraries` orchestrator with batch threading + has_more → Task 2. ✓
- CLI wired, default-all → Task 4. ✓
- MCP wired, default-all → Task 5. ✓
- Stats across all libraries → Task 6. ✓
- Reconciliation safety regression → Task 3. ✓
- Execution of real index, durability check → Task 7. ✓
- Out of scope (max_pages, EMN7YZV7 bug, vision) → untouched. ✓

**Placeholder scan:** No TBD/TODO; all code steps contain full code. ✓

**Type consistency:** `enumerate_indexable_libraries -> list[tuple[int, str]]`, `global_pdf_doc_ids -> set[str]`, `index_all_libraries(config, *, ...) -> dict` used identically across Tasks 1, 2, 4, 5, 6. `reconcile_orphaned_index_docs` returns `{"orphaned_doc_ids", "deleted_count"}` per source. ✓
