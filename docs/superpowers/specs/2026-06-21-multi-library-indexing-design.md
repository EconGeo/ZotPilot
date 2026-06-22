# Durable multi-library indexing

**Date:** 2026-06-21
**Status:** Approved (design)

## Problem

ZotPilot's indexing pipeline only ever runs against the personal Zotero library
(`library_id = 1`). Group libraries are never indexed.

Worse, the indexer is actively hostile to multi-library state. After each run,
`Indexer.index_all` calls `reconcile_orphaned_index_docs` (`index_authority.py:290`),
which deletes every vector-store document whose `doc_id` is **not** in the library
just indexed:

```python
def orphaned_index_doc_ids(store, current_doc_ids):
    return set(store.get_indexed_doc_ids()) - current_doc_ids
```

`index_all` accepts a `protected_doc_ids` parameter precisely to make this
multi-library-safe, but **no caller ever passes it**:

- CLI: `cli.py` `cmd_index` → `Indexer(config)` (library 1), no `protected_doc_ids`.
- MCP: `tools/indexing.py` `index_library` → `Indexer(config)`, no `protected_doc_ids`.

Net effect: groups are uncovered, and any one-off populate of group docs would be
silently wiped on the next ordinary `index_library` / `zotpilot index` run.

The local Zotero database contains these libraries (SQLite `libraryID` → name):

| SQLite libraryID | groupID  | name                      | PDF attachments |
|------------------|----------|---------------------------|-----------------|
| 1                | —        | My Library (user)         | 3103            |
| 3                | 2350352  | affordable_housing        | 54              |
| 4                | 2588582  | regenerative_paradigm     | 254             |
| 7                | 5292619  | ESG Collaboration (actual)| 123             |
| 8                | 6075488  | NAR_settlement            | 62              |

## Goal

Indexing covers **all** libraries (user + groups) by default, durably — no future
index run can silently delete another library's documents. Decided behavior:

- **Index scope:** all libraries by default, on both CLI and MCP entry points.
- **Page limit:** unchanged. `max_pages = 40` stays; long books remain skipped.

## Core idea

Compute one authoritative set of all valid PDF `doc_id`s across every library,
straight from the Zotero SQLite, and pass that **full union** as
`protected_doc_ids` on every per-library `index_all` call.

Because the protected set is derived from Zotero (not from current store state),
reconciliation can only ever delete documents that exist in **no** library. This
holds regardless of iteration order or partial/batched completion: a library not
yet reached in this run is still protected, so its docs survive.

## Components

All changes live in `~/Projects/ZotPilot` (editable source install).

### 1. `enumerate_indexable_libraries(config) -> list[(int, str)]`

Returns `(sqlite_library_id, label)` for the user library (`1`) plus each group,
resolving `groupID → sqlite libraryID` via the existing
`ZoteroClient.resolve_group_library_id`. Built on the existing
`ZoteroClient.get_libraries()`.

### 2. `global_pdf_doc_ids(config) -> set[str]`

Union of `item_key`s with PDF attachments across all libraries. One
`ZoteroClient` per library, reusing `get_all_items_with_pdfs`. This serves as both
the protected set for reconciliation and the "valid" set for stats.

### 3. `index_all_libraries(config, ...) -> dict` (new orchestrator in `indexer.py`)

- Compute `global_pdf_doc_ids` once.
- Iterate libraries; for each, run
  `Indexer(config, library_id=N).index_all(..., protected_doc_ids=union)`.
- Thread the `batch_size` budget across libraries (decrement by items indexed;
  stop spawning new per-library work once the budget is exhausted).
- Aggregate `results` and counts. `has_more` is true if any library still has
  unindexed items **or** the batch budget ran out before a library was reached.
- Preserve existing journal/lease handling.

### 4. Wire entry points

`cli.py cmd_index` and `tools/indexing.py index_library` call
`index_all_libraries` instead of the single-library `Indexer`. Default = all
libraries. `max_pages` default stays 40.

### 5. Stats accuracy

`_collect_unindexed_papers` / `get_index_stats` count unindexed across all
libraries (compare store `doc_id`s against `global_pdf_doc_ids`) instead of
library 1 only, so `unindexed_count` reflects reality after the fix.

## Testing (TDD)

Existing relevant suites: `test_index_authority.py`, `test_indexer.py`,
`test_reconcile_runtime.py`, `test_library_filter.py`, `test_switch_library.py`,
plus `fixtures/` and `conftest.py`.

New tests:

1. `global_pdf_doc_ids` unions PDF item keys across a multi-library SQLite fixture.
2. **Regression that matters:** indexing library A with the global union as
   `protected_doc_ids` does **not** delete library B's already-indexed docs.
3. Batched `index_all_libraries` reports a correct aggregate `has_more` across
   libraries (budget exhausted mid-sweep ⇒ `has_more = true`).

## Execution after the fix

Run `index_all_libraries` to index the 4 group libraries (~493 PDFs,
article-length only after the 40-page skip) plus the few still-indexable
user-library items.

## Out of scope

- Raising `max_pages` / indexing long books.
- Fixing the `EMN7YZV7` `UnboundLocalError` in `index_all` (note it; separate fix).
- Vision / table-extraction changes.
