# Upstreaming multi-library indexing + token-aware chunking

**Date:** 2026-06-22
**Status:** Approved (design)
**Target:** `xunhe730/ZotPilot` (upstream), via fork `EconGeo/ZotPilot`
**Baseline:** fresh branches off `upstream/main` @ `f1210cf` (v0.5.3+)

## Why this is an upstreaming *plan*, not a feature build

Both features are **already implemented** on our fork's `main`. The work here is
*porting* them to current upstream, not writing them from scratch.

The catch: our `main` forked off an **older** upstream and never merged forward.
Upstream has since shipped v0.5.1–v0.5.3 (formula OCR, vision cache, the
OpenAI-compatible **provider registry**, `doctor`, append-only progress JSONL,
connector download). A raw `main` → `upstream/main` diff is ~5k insertions /
~11k deletions and would *revert* that work. So we do **not** cherry-pick our
commits. We re-express the *ideas* on top of current `upstream/main`.

## Scope decisions

- **Two separate PRs**, one per feature — they are independent, and the maintainer
  prefers focused review.
- **Issue first, then PR** for each — socialize the design (with neutral, non-personal
  examples) before investing in the rebase. Upstream is responsive and has credited
  `@EconGeo` before.
- **In scope:** (A) multi-library indexing, (B) token-aware chunking.
- **Out of scope:** embedding reliability (bge-large default, Ollama
  truncation/sub-batching, preflight, retry-cause surfacing). It collides with
  upstream's `providers.py` + `openai_compat.py` registry — upstream already reaches
  Ollama as an OpenAI-compatible vendor (`nomic-embed-text`, 768-dim), whereas our
  fork kept a dedicated native `embeddings/ollama.py` (`bge-large`, 1024-dim) and
  *deleted* the registry. Upstreaming it would mean a redesign against their seam;
  deferred to a possible later PR.

## Feasibility — every seam we need already exists upstream

Verified against `upstream/main @ f1210cf`:

| Seam | Upstream location | Status |
|------|-------------------|--------|
| `reconcile_orphaned_index_docs(store, current_doc_ids, *, library_unreachable=...)` | `index_authority.py:399` | ✅ present |
| `orphaned_index_doc_ids(store, current_doc_ids)` | `index_authority.py:349` | ✅ present |
| `ZoteroClient.get_libraries()` | `zotero_client.py:898` | ✅ present |
| `ZoteroClient.resolve_group_library_id(data_dir, group_id)` | `zotero_client.py:186` | ✅ present |
| `ZoteroClient.get_all_items_with_pdfs()` | `zotero_client.py:247` | ✅ present |
| `pdf/chunker.py` (char chunker) | present, different content | ✅ wrappable |

Two seams our original fork relied on were **removed** by upstream's refactor and
must be reintroduced as *part of* PR A:

- `Indexer.__init__` is `(self, config)` — **no `library_id`** (hardwired to library 1).
- `index_all` **no longer carries `protected_doc_ids`** (the old unused param was
  cleaned up).

We deliberately do **not** resurrect `protected_doc_ids` (see PR A design).

---

## PR A — Multi-library indexing

### Problem (issue framing)

ZotPilot only indexes the personal library (`library_id = 1`); group libraries are
never reached. Worse, after each run `index_all` reconciles orphans by deleting
every vector-store doc whose `doc_id` is **not** in the library just indexed
(`orphaned = store.get_indexed_doc_ids() - current_doc_ids`). So if group docs were
ever populated, the next ordinary `index_library` / `zotpilot index` run silently
wipes them. Two reconciliation sites in `index_all` exhibit this: startup
(`indexer.py:492`) and end-of-run/batched (`indexer.py:1153`).

> **Genericization:** the issue/PR must use a neutral illustrative library table
> (e.g. "My Library + 2 group libraries"), **not** our real group names
> (affordable_housing, regenerative_paradigm, ESG Collaboration, NAR_settlement).

### Core idea

Index every library (user + groups) by default. Make reconciliation **global and
run-once**: an orphan is a doc present in **no** library, computed from the union of
valid PDF `doc_id`s across all libraries, read straight from Zotero SQLite.

### Reconciliation design — `reconcile` flag, NOT `protected_doc_ids`

Rather than re-add the parameter upstream removed, lift reconciliation out of the
per-library call:

1. Add `reconcile: bool = True` to `index_all`, gating **both** reconciliation sites
   (`:492` and `:1153`). Default `True` ⇒ single-library behavior is byte-for-byte
   unchanged; upstream's cleanup stays intact.
2. The orchestrator calls each per-library `index_all(..., reconcile=False)`.
3. After all libraries are processed, the orchestrator runs **one**
   `reconcile_orphaned_index_docs(store, global_pdf_doc_ids(config),
   library_unreachable=<any library unreachable>)`.

Why this beats re-adding `protected_doc_ids`:

- **Respects upstream intent** — a new, smaller, self-justifying boolean seam vs.
  resurrecting a deliberately-deleted set parameter. Easier review.
- **Correct semantics** — orphanhood is inherently global; computing it once in the
  orchestrator is the right altitude.
- **Fewer scans** — one reconcile pass, not N union-aware ones.
- **Safer vs. the mass-delete floor** — a per-library reconcile compares a tiny
  `current_doc_ids` against the whole index and would trip
  `MASS_DELETE_FRACTION_FLOOR` (or delete other libraries); the single global pass
  compares the full union against the full index, so the floor behaves correctly.

The global union is read from Zotero up front, independent of how many libraries a
batched run actually reached, so the final reconcile is safe even on partial /
`has_more` runs. Mirror upstream's "skip reconcile when nothing was indexed"
optimization at the orchestrator level (skip the global pass when aggregate
`indexed == 0`).

### Change set (replayed onto `upstream/main`)

1. **`Indexer.__init__`** — add `library_id: int = 1` (or equivalent), so an Indexer
   can target a specific library; default preserves current behavior.
2. **`index_all`** — add `reconcile: bool = True`; gate both reconciliation sites on it.
3. **`indexer.py` new helpers:**
   - `enumerate_indexable_libraries(config) -> list[(sqlite_library_id, label)]` —
     user library `1` plus each group, resolving `groupID → sqlite libraryID` via
     `resolve_group_library_id`, built on `get_libraries()`.
   - `global_pdf_doc_ids(config) -> set[str]` — union of PDF-bearing `item_key`s
     across all libraries (one `ZoteroClient` per library, reusing
     `get_all_items_with_pdfs`). Serves as both the reconcile floor and the stats
     "valid" set.
4. **`index_all_libraries(config, ...) -> dict`** — orchestrator:
   - compute `global_pdf_doc_ids` once;
   - iterate libraries, each `Indexer(config, library_id=N).index_all(..., reconcile=False)`;
   - thread the `batch_size` budget across libraries (decrement by items indexed;
     stop spawning new per-library work when exhausted);
   - aggregate `results` + counts; `has_more` true if any library still has
     unindexed items **or** the budget ran out before a library was reached;
   - run the single global reconciliation (unless aggregate `indexed == 0`);
   - preserve journal/lease handling.
5. **Wire entry points** — `cli.py cmd_index` and `tools/indexing.py index_library`
   call `index_all_libraries` instead of constructing a single-library `Indexer`.
6. **Stats** — `get_index_stats` / unindexed counts use `global_pdf_doc_ids` so the
   "unindexed papers" figure spans all libraries (port from our fork).

### Behavior change to flag in the issue

`index_library` / `zotpilot index` default scope goes single → **all** libraries.
Offer the maintainer an opt-in flag (e.g. `--library <id>` / `all_libraries=True`
default) if they prefer to preserve single-library default.

### Tests

Port `tests/test_multi_library_indexing.py` (~350 lines), adapted to upstream's
`index_all` shape, covering at minimum:
- reconciliation never deletes another library's docs across a multi-library run;
- a fully-indexed library does not starve later libraries in a batched run;
- batch budget-exhaustion sets `has_more` and the global reconcile still runs safely;
- `--limit 0` means index-nothing; aggregate `already_indexed` across libraries.

---

## PR B — Token-aware (LlamaIndex) chunking

### Problem (issue framing)

The char-based chunker can emit chunks longer than the embedding model's token
window; dense academic text is then silently truncated at embed time, degrading
retrieval. A token-aware backend that splits using the model's own tokenizer
guarantees every chunk fits.

### Design

Opt-in chunker backend behind a protocol seam. Default stays `char` ⇒ fully
backward compatible.

1. **`pdf/chunker_base.py`** — `ChunkerProtocol` (`chunk(full_text, pages, sections)
   -> list[Chunk]`). No behavior change; the existing char chunker already satisfies it.
2. **`pdf/llamaindex_chunker.py`** — `LlamaIndexChunker`: LlamaIndex
   `SentenceSplitter` driven by a HuggingFace `tokenizers` tokenizer, with a
   `hard_cap_tokens` guarantee (decode-truncate any residual over-cap chunk).
   **Self-contained** — it does *not* reach into the embedding provider.
3. **`config.chunker_backend`** (default `"char"`); `indexer.py` selects the backend
   and folds it into the config hash **only for non-default backends**, so existing
   "char" indexes see an unchanged hash and are **not** force-reindexed.
4. **Dependencies** — `llama-index-core` + `tokenizers` as an **optional extra**
   (e.g. `pip install zotpilot[llamaindex]`), since the backend is opt-in.
5. **Tokenizer default** — our fork hardcodes `BAAI/bge-large-en-v1.5` /
   `hard_cap_tokens=512`. Upstream's default vendor model differs (`bge-m3` /
   `nomic-embed-text`). Make the tokenizer + cap **configurable** and document
   aligning them to the active embedding model; pick a neutral default for upstream.

### Tests

Port `tests/test_llamaindex_chunker.py` + `tests/test_chunker_protocol.py`:
- token cap is never exceeded (incl. the decode-truncate fallback);
- char backend remains the default and its config hash is unchanged;
- protocol conformance for both backends.

---

## Sequencing & workflow

For each PR (A then B, independently):

1. Open a **tracking issue** on `xunhe730/ZotPilot` with the problem framing above,
   neutral examples, the proposed design, and the behavior-change note. Await
   maintainer signal on shape.
2. Branch off fresh `upstream/main`:
   `git fetch upstream && git switch -c feat/<name> upstream/main`.
3. Replay the change set; keep the diff minimal and aligned to upstream's style.
4. Port the tests; run the full suite green locally.
5. Open the PR referencing the issue; respond to review.

PR A and PR B touch largely disjoint files (`indexer.py`/`tools` vs.
`pdf/`), so they can proceed in parallel without stacking.

## Open considerations (raise in the issues)

- **A:** default-scope behavior change (single → all libraries); offer an opt-in flag.
- **B:** tokenizer/cap defaults should track the active embedding model, not be
  hardcoded to `bge-large`.
- **Both:** confirm the maintainer wants the LlamaIndex deps as an optional extra
  vs. a core dependency.
