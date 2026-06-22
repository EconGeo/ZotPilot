# [Issue draft A] Indexing only covers the personal library, and reconciliation can wipe group-library docs

**Target:** `xunhe730/ZotPilot` · **Labels:** enhancement · **Author:** @EconGeo

## Summary

`zotpilot index` / the `index_library` MCP tool only ever index the personal
library (`library_id = 1`). Group libraries are never indexed. Worse, the
orphan-reconciliation step is multi-library-hostile: after each run it deletes
every vector-store document whose `doc_id` is **not** in the single library that
was just indexed — so if group documents are ever present in the index, the next
ordinary index run silently removes them.

I have this working on a fork and would like to upstream it. Opening this first
to agree on the approach before sending the PR.

## Where it happens (against `main`)

Reconciliation computes orphans as "indexed docs minus *this library's* current
docs":

- `index_authority.py:349` — `orphaned_index_doc_ids(store, current_doc_ids) = store.get_indexed_doc_ids() - current_doc_ids`
- `index_authority.py:399` — `reconcile_orphaned_index_docs(...)` deletes them.

`Indexer.index_all` calls it twice, each time with `current_doc_ids` built from a
single library:

- startup pass (`indexer.py:~492`)
- end-of-run/batched pass (`indexer.py:~1153`)

Since `Indexer.__init__` constructs `ZoteroClient(config.zotero_data_dir)` (always
`library_id = 1`), group libraries are never reached, and any group docs in the
store are reconciled away on the next personal-library index.

## Reproduction (neutral example)

A Zotero install with the personal library plus two group libraries:

| SQLite libraryID | type  | name           |
|------------------|-------|----------------|
| 1                | user  | My Library     |
| 3                | group | Team Library A |
| 4                | group | Team Library B |

1. Index normally → only `My Library` is indexed.
2. Manually index a doc from `Team Library A`.
3. Run `zotpilot index` again → the `Team Library A` doc is deleted from the index
   (it is not in `My Library`'s `current_doc_ids`).

## Proposed approach

Index every library by default, and make orphan reconciliation **global and
run-once** — an orphan should be a doc present in **no** library, not "absent from
the one library I just indexed."

Concretely (all on top of seams that already exist in `main`):

1. `Indexer.__init__(config, library_id=1)` — forward `library_id` to the
   already-library-aware `ZoteroClient(data_dir, library_id=...)`.
2. Add `reconcile: bool = True` to `index_all`, gating both reconciliation sites.
   Default `True` ⇒ existing single-library behavior is byte-for-byte unchanged.
3. New `index_all_libraries(config, ...)` orchestrator: enumerate libraries via
   `ZoteroClient.get_libraries()` (+ `resolve_group_library_id` for groups), run
   each with `reconcile=False`, then perform **one** `reconcile_orphaned_index_docs`
   against the cross-library PDF doc-id union (built from
   `current_library_pdf_doc_ids` per library). Thread `batch_size` as a budget
   across libraries.
4. Point `cli.py cmd_index` and `tools/indexing.py index_library` at the
   orchestrator; count unindexed papers across all libraries in `get_index_stats`.

I chose a single global reconciliation (rather than re-introducing the old
`protected_doc_ids` parameter that was removed) because:
- orphanhood is inherently global, so it belongs in one pass at the orchestrator;
- it does one reconcile instead of N, and
- it's safer against the existing mass-delete floor — a per-library reconcile
  compares a tiny `current_doc_ids` against the whole index and could trip
  `MASS_DELETE_FRACTION_FLOOR`, whereas the global pass compares the full union
  against the full index. The empty-read / `library_unreachable` guards are
  preserved (the orchestrator passes `library_unreachable=<any library unreachable>`).

## Behavior change to confirm

This makes **all libraries** the default index scope for both `zotpilot index` and
`index_library` (today: personal only). I think that's the right default, but if
you'd prefer to keep single-library as the default I can gate it behind an opt-in
(e.g. a `--all-libraries` flag / `all_libraries=False` parameter). Your call on
which you'd merge.

## Tests

I have ~12 focused tests (library enumeration, the union, the reconcile gate, batch
budget threading + `has_more`, "fully-indexed first library doesn't starve later
ones", aggregate-vs-summed `already_indexed`, and the "global union keeps other
libraries' docs / single-library reconcile would delete them" invariants).

Happy to send the PR once you're good with the shape — especially the default-scope
question and the global-reconcile design.
