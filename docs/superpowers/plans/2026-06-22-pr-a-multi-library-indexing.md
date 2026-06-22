# PR A — Multi-library indexing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Index every Zotero library (user + groups) by default, with orphan reconciliation lifted to a single global pass so no per-library run can ever delete another library's indexed documents.

**Architecture:** Add a `library_id` to `Indexer` (forwarded to its already-library-aware `ZoteroClient`), add a `reconcile: bool = True` gate around `index_all`'s two reconciliation sites, and introduce an `index_all_libraries` orchestrator that runs each library with `reconcile=False` and performs one global `reconcile_orphaned_index_docs` against the cross-library PDF doc-id union. CLI and MCP entry points call the orchestrator; stats count unindexed papers across all libraries.

**Tech Stack:** Python 3.10+, pytest, existing ZotPilot indexing pipeline (ChromaDB vector store, Zotero SQLite reader).

## Global Constraints

- Branch off **fresh `upstream/main`** (`git fetch upstream && git switch -c feat/multi-library-indexing upstream/main`). Do **not** cherry-pick our fork's commits — `main` diverged pre-v0.5.1.
- **`reconcile: bool = True` default** ⇒ existing single-library `index_all` behavior is byte-for-byte unchanged. Never reintroduce the removed `protected_doc_ids` parameter.
- Reconciliation's mass-delete safety floor (`MASS_DELETE_FRACTION_FLOOR`, empty-read guard, `library_unreachable`) must remain authoritative — the orchestrator passes `library_unreachable=<any library unreachable>` to the global pass.
- Public examples (issue/PR text) use a **neutral** library table — never the real group names (affordable_housing, regenerative_paradigm, ESG Collaboration, NAR_settlement).
- Reuse existing upstream seams verbatim: `ZoteroClient(data_dir, library_id=1)`, `ZoteroClient.get_libraries()`, `ZoteroClient.resolve_group_library_id()`, `index_authority.current_library_pdf_doc_ids()`, `index_authority.reconcile_orphaned_index_docs()`, `Indexer._library_unreachable()`.
- Run the full suite (`pytest -q`) green before opening the PR.

---

## File Structure

- **Modify** `src/zotpilot/indexer.py` — `Indexer.__init__` gains `library_id`; `index_all` gains `reconcile`; add module-level `enumerate_indexable_libraries`, `global_pdf_doc_ids`, `index_all_libraries`.
- **Modify** `src/zotpilot/cli.py` — `cmd_index` calls `index_all_libraries`.
- **Modify** `src/zotpilot/tools/indexing.py` — `index_library` calls `index_all_libraries`; stats helpers count across all libraries.
- **Create** `tests/test_multi_library_indexing.py` — orchestrator, helpers, reconcile-gate, and stats coverage.

Reference implementation already exists on our fork at `git show main:src/zotpilot/indexer.py` (uses the old `protected_doc_ids` approach) and `git show main:tests/test_multi_library_indexing.py`. Port the *ideas*; the reconcile-gate design below supersedes the `protected_doc_ids` threading.

---

### Task 1: `Indexer` accepts a target `library_id`

**Files:**
- Modify: `src/zotpilot/indexer.py` (`Indexer.__init__`, ~line 133)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Produces: `Indexer(config, library_id: int = 1)` — forwards `library_id` to `ZoteroClient`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_multi_library_indexing.py
import types
import zotpilot.indexer as idx


def _patch_zoteroclient(monkeypatch):
    """Replace ZoteroClient with a fake that just records its library_id."""
    captured = {}

    class _FakeZC:
        def __init__(self, data_dir, library_id=1):
            self.data_dir = data_dir
            self.library_id = library_id
            captured["library_id"] = library_id

    monkeypatch.setattr(idx, "ZoteroClient", _FakeZC)
    return captured


def test_indexer_forwards_library_id_to_client(monkeypatch, tmp_path):
    captured = _patch_zoteroclient(monkeypatch)
    # Neutralize the rest of __init__ so the test stays a unit test.
    monkeypatch.setattr(idx, "Chunker", lambda **k: object())
    monkeypatch.setattr(idx, "create_embedder", lambda c: object())
    monkeypatch.setattr(idx, "VectorStore", lambda *a, **k: object())
    monkeypatch.setattr(idx, "JournalRanker", lambda: object())

    cfg = types.SimpleNamespace(
        zotero_data_dir=tmp_path, chunk_size=400, chunk_overlap=100,
        chroma_db_path=tmp_path, vision_enabled=False, vision_provider="anthropic",
    )
    idx.Indexer(cfg, library_id=7)
    assert captured["library_id"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_library_indexing.py::test_indexer_forwards_library_id_to_client -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'library_id'`.

- [ ] **Step 3: Edit `Indexer.__init__`**

Change the signature and the `ZoteroClient` construction:

```python
    def __init__(self, config: Config, library_id: int = 1):
        self.config = config
        self.zotero = ZoteroClient(config.zotero_data_dir, library_id=library_id)
```

Leave the rest of `__init__` unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_multi_library_indexing.py::test_indexer_forwards_library_id_to_client -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): Indexer accepts target library_id, forwarded to ZoteroClient"
```

---

### Task 2: `index_all` gains a `reconcile` gate

**Files:**
- Modify: `src/zotpilot/indexer.py` (`index_all` signature ~line 410; reconcile sites ~492 and ~1153)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Produces: `Indexer.index_all(..., reconcile: bool = True)` — when `False`, neither the startup nor the end-of-run reconciliation runs.

- [ ] **Step 1: Write the failing test**

```python
def test_index_all_skips_reconcile_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        idx, "reconcile_orphaned_index_docs",
        lambda *a, **k: calls.append(a) or {"deleted_count": 0},
    )
    # Build an Indexer shell without running heavy __init__.
    inst = idx.Indexer.__new__(idx.Indexer)
    inst.config = types.SimpleNamespace(zotero_data_dir=None)

    class _Z:
        def get_all_items_with_pdfs(self):
            return []  # empty library -> startup reconcile is the only candidate

    class _Store:
        def get_indexed_doc_ids(self):
            return []

    inst.zotero = _Z()
    inst.store = _Store()
    inst.journal = None
    inst._formula_provider = None
    monkeypatch.setattr(idx.Indexer, "_ensure_formula_provider_available", lambda self: None)
    monkeypatch.setattr(idx.Indexer, "_library_unreachable", lambda self: False)

    inst.index_all(reconcile=False)
    assert calls == []  # reconciliation suppressed

    inst.index_all(reconcile=True)
    assert len(calls) == 1  # startup reconcile ran (empty-library no-op call)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_library_indexing.py::test_index_all_skips_reconcile_when_disabled -v`
Expected: FAIL — `TypeError: index_all() got an unexpected keyword argument 'reconcile'`.

- [ ] **Step 3: Add the parameter and gate both reconcile sites**

In the `index_all` signature, add `reconcile: bool = True` (place it after `progress_sink`):

```python
    def index_all(
        self,
        force_reindex: bool = False,
        limit: int | None = None,
        item_key: str | None = None,
        item_keys: list[str] | None = None,
        title_pattern: str | None = None,
        max_pages: int = 0,
        batch_size: int | None = None,
        journal: IndexJournal | None = None,
        progress_sink: ProgressSink | None = None,
        reconcile: bool = True,
    ) -> dict:
```

At the **startup** reconcile site (currently `reconciliation = reconcile_orphaned_index_docs(self.store, current_doc_ids, library_unreachable=self._library_unreachable())` near line 492), wrap the whole reconcile-and-log block:

```python
        current_doc_ids = {item.item_key for item in items}
        if reconcile:
            reconciliation = reconcile_orphaned_index_docs(
                self.store,
                current_doc_ids,
                library_unreachable=self._library_unreachable(),
            )
            if reconciliation.get("refused_mass_delete"):
                logger.warning(
                    "Indexer: refused to delete orphaned indexed document(s) — %s",
                    reconciliation.get("skipped_reason", "mass-deletion safety floor triggered"),
                )
            elif reconciliation["deleted_count"] > 0:
                logger.info(
                    "Indexer: removed %d orphaned indexed document(s) not present in the current Zotero PDF library",
                    reconciliation["deleted_count"],
                )
```

At the **end-of-run** reconcile site (currently `if counts["indexed"] > 0:` near line 1147), add the flag to the condition:

```python
        if reconcile and counts["indexed"] > 0:
            final_current_doc_ids = {
                item.item_key
                for item in self.zotero.get_all_items_with_pdfs()
                if item.pdf_path and item.pdf_path.exists()
            }
            final_reconciliation = reconcile_orphaned_index_docs(
                self.store,
                final_current_doc_ids,
                library_unreachable=self._library_unreachable(),
            )
            # ... existing logging unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_multi_library_indexing.py::test_index_all_skips_reconcile_when_disabled -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): reconcile flag gates index_all's per-library orphan reconciliation"
```

---

### Task 3: Library enumeration + global PDF doc-id union helpers

**Files:**
- Modify: `src/zotpilot/indexer.py` (add two module-level functions after `IndexResult`)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Produces: `enumerate_indexable_libraries(config) -> list[tuple[int, str]]` — `(sqlite_library_id, label)` for user (always `(1, name)` first) + each group, resolving groupID→sqlite libraryID.
- Produces: `global_pdf_doc_ids(config) -> set[str]` — union of PDF-bearing item keys across all libraries.

- [ ] **Step 1: Write the failing test**

```python
def test_enumerate_and_union_span_all_libraries(monkeypatch, tmp_path):
    libs = [
        {"library_id": "1", "library_type": "user", "name": "My Library", "item_count": 2},
        {"library_id": "2350352", "library_type": "group", "name": "Group A", "item_count": 1},
    ]

    class _FakeZC:
        def __init__(self, data_dir, library_id=1):
            self.library_id = library_id
        def get_libraries(self):
            return libs
        def get_all_items_with_pdfs(self):
            return []  # union content covered via current_library_pdf_doc_ids patch

    monkeypatch.setattr(idx, "ZoteroClient", _FakeZC)
    monkeypatch.setattr(idx.ZoteroClient, "resolve_group_library_id",
                        staticmethod(lambda data_dir, gid: {2350352: 3}[gid]))
    cfg = types.SimpleNamespace(zotero_data_dir=tmp_path)

    assert idx.enumerate_indexable_libraries(cfg) == [(1, "My Library"), (3, "Group A")]

    # Each library contributes a distinct doc id to the union.
    seen = {1: {"AAA"}, 3: {"BBB"}}
    monkeypatch.setattr(
        "zotpilot.index_authority.current_library_pdf_doc_ids",
        lambda zc: seen[zc.library_id],
    )
    assert idx.global_pdf_doc_ids(cfg) == {"AAA", "BBB"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_library_indexing.py::test_enumerate_and_union_span_all_libraries -v`
Expected: FAIL — `AttributeError: module 'zotpilot.indexer' has no attribute 'enumerate_indexable_libraries'`.

- [ ] **Step 3: Add the helpers**

Insert after the `IndexResult` dataclass in `indexer.py`:

```python
def enumerate_indexable_libraries(config) -> list[tuple[int, str]]:
    """Return (sqlite_library_id, label) for the user library plus every group.

    User library is always first as (1, name). Group ``library_id`` values from
    ``get_libraries()`` are Zotero groupIDs and are resolved to SQLite libraryIDs.
    """
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
    from .index_authority import current_library_pdf_doc_ids

    ids: set[str] = set()
    for lib_id, _label in enumerate_indexable_libraries(config):
        zc = ZoteroClient(config.zotero_data_dir, library_id=lib_id)
        ids |= current_library_pdf_doc_ids(zc)
    return ids
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_multi_library_indexing.py::test_enumerate_and_union_span_all_libraries -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): library enumeration + cross-library PDF doc-id union helpers"
```

---

### Task 4: `index_all_libraries` orchestrator with one global reconciliation

**Files:**
- Modify: `src/zotpilot/indexer.py` (add `index_all_libraries` after the helpers)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Consumes: `enumerate_indexable_libraries`, `global_pdf_doc_ids`, `Indexer(config, library_id=...)`, `Indexer.index_all(..., reconcile=False)`, `reconcile_orphaned_index_docs`.
- Produces: `index_all_libraries(config, *, force_reindex=False, limit=None, item_key=None, item_keys=None, title_pattern=None, max_pages=0, batch_size=None, journal=None, progress_sink=None) -> dict` with aggregated `results`/counts/`has_more`/`already_indexed`/`quality_distribution`/`extraction_stats`.

- [ ] **Step 1: Write the failing tests**

```python
class _FakeIndexerStore:
    def __init__(self, indexed_ids):
        self._ids = set(indexed_ids)
    def get_indexed_doc_ids(self):
        return set(self._ids)


class _FakeIndexer:
    """Per-library fake recording the reconcile kwarg and returning a canned result."""
    instances = []

    def __init__(self, config, library_id=1):
        self.library_id = library_id
        self.store = _FakeIndexerStore({"AAA"} if library_id == 1 else {"BBB"})
        self.calls = []
        _FakeIndexer.instances.append(self)

    def _library_unreachable(self):
        return False

    def index_all(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "results": [], "indexed": 1, "failed": 0, "empty": 0, "skipped": 0,
            "skipped_long": 0, "has_more": False, "long_documents": [],
            "skipped_no_pdf": [], "quality_distribution": {"A": 1},
            "extraction_stats": {"native": 1},
        }


def _wire_orchestrator(monkeypatch, union):
    _FakeIndexer.instances = []
    monkeypatch.setattr(idx, "Indexer", _FakeIndexer)
    monkeypatch.setattr(idx, "enumerate_indexable_libraries",
                        lambda c: [(1, "My Library"), (3, "Group A")])
    monkeypatch.setattr(idx, "global_pdf_doc_ids", lambda c: set(union))
    recon = []
    monkeypatch.setattr(idx, "reconcile_orphaned_index_docs",
                        lambda store, ids, **k: recon.append((set(ids), k)) or {"deleted_count": 0})
    return recon


def test_orchestrator_runs_each_library_with_reconcile_false(monkeypatch):
    _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    out = idx.index_all_libraries(types.SimpleNamespace())
    assert [i.library_id for i in _FakeIndexer.instances] == [1, 3]
    assert all(c["reconcile"] is False for i in _FakeIndexer.instances for c in i.calls)
    assert out["indexed"] == 2
    assert out["quality_distribution"] == {"A": 2}
    assert out["extraction_stats"] == {"native": 2}


def test_orchestrator_reconciles_once_against_global_union(monkeypatch):
    recon = _wire_orchestrator(monkeypatch, {"AAA", "BBB"})
    idx.index_all_libraries(types.SimpleNamespace())
    assert len(recon) == 1                       # exactly one global pass
    assert recon[0][0] == {"AAA", "BBB"}         # against the union
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_multi_library_indexing.py -k orchestrator -v`
Expected: FAIL — `AttributeError: module 'zotpilot.indexer' has no attribute 'index_all_libraries'`.

- [ ] **Step 3: Add the orchestrator**

Insert after the helpers in `indexer.py`:

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
    progress_sink=None,
) -> dict:
    """Index every Zotero library (user + groups), reconciling orphans once globally.

    Each per-library ``index_all`` runs with ``reconcile=False``; after all
    libraries are processed a single ``reconcile_orphaned_index_docs`` runs against
    the cross-library PDF doc-id union, so an orphan is a doc present in NO library.
    Threads ``batch_size`` as a budget across libraries.
    """
    union = global_pdf_doc_ids(config)
    libraries = enumerate_indexable_libraries(config)

    agg_results: list = []
    summed = {"indexed": 0, "failed": 0, "empty": 0, "skipped": 0, "skipped_long": 0}
    agg_quality_distribution: dict[str, int] = {}
    agg_extraction_stats: dict[str, int] = {}
    long_documents: list = []
    skipped_no_pdf: list = []
    has_more = False
    budget = batch_size  # None => unlimited per library
    last_idxr = None
    any_unreachable = False

    for lib_id, _label in libraries:
        if budget is not None and budget <= 0:
            has_more = True  # ran out before visiting this library
            break

        idxr = Indexer(config, library_id=lib_id)
        any_unreachable = any_unreachable or idxr._library_unreachable()
        res = idxr.index_all(
            force_reindex=force_reindex,
            limit=limit,
            item_key=item_key,
            item_keys=item_keys,
            title_pattern=title_pattern,
            max_pages=max_pages,
            batch_size=budget,
            journal=journal,
            progress_sink=progress_sink,
            reconcile=False,  # defer to the single global pass below
        )
        last_idxr = idxr

        agg_results.extend(res.get("results", []))
        for k in summed:
            summed[k] += res.get(k, 0)
        long_documents.extend(res.get("long_documents", []))
        skipped_no_pdf.extend(res.get("skipped_no_pdf", []))
        for grade, count in res.get("quality_distribution", {}).items():
            agg_quality_distribution[grade] = agg_quality_distribution.get(grade, 0) + count
        for stat, count in res.get("extraction_stats", {}).items():
            agg_extraction_stats[stat] = agg_extraction_stats.get(stat, 0) + count

        progress = res.get("indexed", 0) + res.get("failed", 0) + res.get("empty", 0)
        if budget is not None:
            budget -= progress

        if batch_size is not None and res.get("has_more") and progress > 0:
            has_more = True
            break  # real work done and more remains -> resume here next call
        elif batch_size is None and res.get("has_more"):
            has_more = True  # full sweep: aggregate but keep going

    # Single global reconciliation: an orphan is a doc present in NO library. The
    # union is read from Zotero up front, so this is safe even on partial/batched
    # runs. The mass-delete floor + any_unreachable still guard against bad reads.
    if last_idxr is not None:
        reconcile_orphaned_index_docs(
            last_idxr.store, union, library_unreachable=any_unreachable
        )

    # already_indexed = distinct docs present in BOTH the union and the store
    # (NOT the sum of per-library counts).
    if last_idxr is not None and getattr(last_idxr, "store", None) is not None:
        already_indexed = len(last_idxr.store.get_indexed_doc_ids() & union)
    else:
        already_indexed = 0

    out = {"results": agg_results, "has_more": has_more}
    out.update(summed)
    out["already_indexed"] = already_indexed
    out["long_documents"] = long_documents
    out["skipped_no_pdf"] = skipped_no_pdf
    out["quality_distribution"] = agg_quality_distribution
    out["extraction_stats"] = agg_extraction_stats
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_multi_library_indexing.py -k orchestrator -v`
Expected: PASS.

- [ ] **Step 5: Port the remaining orchestrator tests**

From `git show main:tests/test_multi_library_indexing.py`, port these cases (adapt the fake to the `_FakeIndexer` above; drop any `protected_doc_ids` assertions — the orchestrator now passes `reconcile=False` and reconciles once globally):
- `test_index_all_libraries_batch_reports_aggregate_has_more`
- `test_index_all_libraries_batch_exhaustion_skips_unvisited_library`
- `test_index_all_libraries_does_not_stall_on_fully_indexed_first_library`
- `test_limit_zero_indexes_nothing`
- `test_aggregate_already_indexed_is_distinct_not_summed`

Also port the direct-reconcile invariants (they exercise `reconcile_orphaned_index_docs` and need no change):
- `test_reconcile_with_union_keeps_other_library_docs`
- `test_reconcile_without_union_would_delete_other_library_docs`

Run: `pytest tests/test_multi_library_indexing.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/zotpilot/indexer.py tests/test_multi_library_indexing.py
git commit -m "feat(indexer): index_all_libraries orchestrator with single global reconciliation"
```

---

### Task 5: Wire CLI and MCP entry points to the orchestrator

**Files:**
- Modify: `src/zotpilot/cli.py` (`cmd_index`, ~line 797)
- Modify: `src/zotpilot/tools/indexing.py` (`index_library`, ~line 278)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Consumes: `index_all_libraries`.

- [ ] **Step 1: Write the failing test**

```python
def test_cli_and_mcp_call_index_all_libraries(monkeypatch):
    import zotpilot.cli as cli
    seen = {}
    monkeypatch.setattr("zotpilot.indexer.index_all_libraries",
                        lambda config, **k: seen.setdefault("called", k) or
                        {"results": [], "indexed": 0, "failed": 0, "empty": 0,
                         "skipped": 0, "already_indexed": 0, "skipped_no_pdf": [],
                         "has_more": False})
    # The CLI/MCP wiring imports index_all_libraries from .indexer; assert the
    # symbol is referenced (smoke check that the call site was switched over).
    import inspect
    assert "index_all_libraries" in inspect.getsource(cli.cmd_index)
    import zotpilot.tools.indexing as ti
    assert "index_all_libraries" in inspect.getsource(ti.index_library)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_library_indexing.py::test_cli_and_mcp_call_index_all_libraries -v`
Expected: FAIL — `index_all_libraries` not present in either call site's source.

- [ ] **Step 3: Switch the CLI call site**

In `cli.py cmd_index`, replace:

```python
    indexer = Indexer(config)
    try:
        result = indexer.index_all(
            force_reindex=args.force,
            limit=args.limit,
            item_key=args.item_key,
            title_pattern=args.title,
            max_pages=max_pages,
            batch_size=batch_size,
            journal=journal,
            progress_sink=progress_sink,
        )
```

with:

```python
    from .indexer import index_all_libraries
    try:
        result = index_all_libraries(
            config,
            force_reindex=args.force,
            limit=args.limit,
            item_key=args.item_key,
            title_pattern=args.title,
            max_pages=max_pages,
            batch_size=batch_size,
            journal=journal,
            progress_sink=progress_sink,
        )
```

(The `Indexer` import on the function's first line can stay; it's still used for the exception types.)

- [ ] **Step 4: Switch the MCP call site**

In `tools/indexing.py index_library`, replace:

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

- [ ] **Step 5: Run test + full suite**

Run: `pytest tests/test_multi_library_indexing.py::test_cli_and_mcp_call_index_all_libraries -v && pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zotpilot/cli.py src/zotpilot/tools/indexing.py tests/test_multi_library_indexing.py
git commit -m "feat(cli,mcp): index all libraries by default via index_all_libraries"
```

---

### Task 6: Count unindexed papers across all libraries (stats)

**Files:**
- Modify: `src/zotpilot/tools/indexing.py` (`_collect_unindexed_papers` ~line 39-42; `get_index_stats` ~line 472)
- Test: `tests/test_multi_library_indexing.py`

**Interfaces:**
- Consumes: `global_pdf_doc_ids`.

- [ ] **Step 1: Write the failing test**

```python
def test_collect_unindexed_papers_spans_all_libraries(monkeypatch):
    import zotpilot.tools.indexing as ti
    # Union spans two libraries; only one doc is indexed -> one unindexed remains.
    monkeypatch.setattr("zotpilot.indexer.global_pdf_doc_ids",
                        lambda config: {"AAA", "BBB"})
    monkeypatch.setattr(ti, "_get_config", lambda: types.SimpleNamespace())

    class _Store:
        def get_indexed_doc_ids(self):
            return {"AAA"}
    monkeypatch.setattr(ti, "_get_store", lambda: _Store())

    src = __import__("inspect").getsource(ti._collect_unindexed_papers)
    assert "global_pdf_doc_ids" in src  # stats use the cross-library union
```

(The source-level assertion keeps the test independent of the unindexed-paper row formatting, which is unchanged.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_library_indexing.py::test_collect_unindexed_papers_spans_all_libraries -v`
Expected: FAIL — `global_pdf_doc_ids` not referenced in `_collect_unindexed_papers`.

- [ ] **Step 3: Switch both stats call sites to the union**

In `_collect_unindexed_papers`, replace the single-library `current_doc_ids = current_library_pdf_doc_ids(zotero)` (line 42) with:

```python
    from ..indexer import global_pdf_doc_ids

    current_doc_ids = global_pdf_doc_ids(_get_config())
```

In `get_index_stats`, replace the analogous `current_doc_ids = current_library_pdf_doc_ids(zotero)` (line 472) with the same `global_pdf_doc_ids(_get_config())` call. Remove now-unused single-library `zotero`/`current_library_pdf_doc_ids` references only if they become dead.

- [ ] **Step 4: Run test + full suite**

Run: `pytest tests/test_multi_library_indexing.py::test_collect_unindexed_papers_spans_all_libraries -v && pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/tools/indexing.py tests/test_multi_library_indexing.py
git commit -m "feat(stats): count unindexed papers across all libraries"
```

---

## Self-Review

- **Spec coverage:** Indexer `library_id` (T1), `reconcile` gate replacing `protected_doc_ids` (T2), `enumerate_indexable_libraries`/`global_pdf_doc_ids` (T3), `index_all_libraries` with one global reconcile + budget threading + aggregation (T4), CLI+MCP wiring (T5), cross-library stats (T6). Behavior-change note (single→all default) is carried in the design spec and the issue text.
- **Placeholders:** none — every code step shows the exact edit; bulk test cases are ported from a named, existing file with explicit adaptations.
- **Type consistency:** `reconcile: bool` (T2) is consumed as `reconcile=False` (T4); `global_pdf_doc_ids(config) -> set[str]` (T3) consumed in T4 and T6; `index_all_libraries(config, ...)` (T4) consumed in T5.
