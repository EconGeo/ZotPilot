# Indexing Reliability + Token-Aware Chunking (RAG-Library Delegation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ZotPilot's indexing pipeline robust — no document silently fails, no run dies on a single over-long chunk, misconfiguration is caught before long runs — and replace the home-grown char-count chunker with a token-aware splitter delegated to a battle-tested RAG library, behind a swappable abstraction.

**Architecture:** Two phases. **Phase A (reliability)** hardens the existing pipeline with no new runtime dependencies: a token-aware truncation safety net at the embedding boundary, honest error surfacing, preflight config validation, and small correctness/UX fixes. **Phase B (chunking delegation)** introduces a `ChunkerProtocol` seam, keeps the current behavior behind it, then adds a `LlamaIndexChunker` that uses LlamaIndex's token-aware `SentenceSplitter` with bge-large's own tokenizer — so chunks are guaranteed to fit the embedding model's context window. ZotPilot keeps its own Chroma store, provider embedders, extraction pipeline, and `index_all_libraries` orchestration; only the *splitting* and *embedding-boundary safety* change.

**Tech Stack:** Python 3, pytest, ChromaDB, Ollama/Gemini/DashScope embedders, `tokenizers` (HF, for the bge tokenizer), `llama-index-core` (text splitting only — not storage/query).

## Background: why these changes

Observed during the multi-library indexing live run (see PR #2, Issue #3):

- **Issue #3 — over-long chunks fail the whole document.** `pdf/chunker.py:19` sizes chunks by characters (`chunk_chars = chunk_size * 4`), a chars/4 token *estimate*. Dense academic text (math, citations, rare subwords) tokenizes to more tokens per char, so a nominally-400-token chunk can exceed bge-large's 512-token limit. `OllamaEmbedder.embed` (`embeddings/ollama.py:33`) sends all texts in one `input` batch with no length guard, so one over-long chunk returns HTTP `400` and the entire document is marked failed. 19 documents failed this way in the live run.
- **Masked errors.** The real failure (HTTP 400 / API-key-invalid) surfaces in some paths as `UnboundLocalError: cannot access local variable 'e'` — an exception-variable scope bug in the embedding retry path. The per-document handler at `indexer.py:635` faithfully records whatever reason it is handed, so a useless reason string reaches the user.
- **Silent misconfiguration.** `embedding_provider=gemini` with an invalid key ran for hours before failing, while a working local Ollama backend was present. There is no preflight check that the configured embedder is reachable/authorized.
- **UX/correctness papercuts.** `--limit 0` means "no limit" (0 is falsy at `cli.py:337`'s guard / `indexer.py:360`), not "index nothing"; the multi-library aggregate over-counts `already_indexed` (sums each library's full store count); the CLI quality-distribution/extraction-stats summary silently disappears after a multi-library run; PDFs whose files are absent on disk are counted as "unindexed" forever.

## Global Constraints

- **No regression to the multi-library safety invariant.** The cross-library protected-union reconciliation (PR #2) must remain intact: a run never deletes a doc present in any library. Do not touch `index_authority.py` reconciliation semantics.
- **Keep ZotPilot's own storage and embedder providers.** Delegation is for *text splitting* (and an embedding-boundary safety net) only. Do NOT adopt LlamaIndex's vector store, query engine, or its embedding wrappers in place of `embeddings/*` or `vector_store.py`.
- **bge-large context limit = 512 tokens.** Target a conservative 480-token chunk ceiling with a hard 512 cap; chunks must never exceed the model's limit after splitting.
- **New heavy deps are optional extras.** `llama-index-core` and `tokenizers`/`transformers` go behind an extra (e.g. `pip install zotpilot[llamaindex]`); core install and the existing char-based chunker must keep working with no new deps.
- **Chunker changes are opt-in and migration-aware.** Switching chunkers changes chunk boundaries; existing indexed collections must not be silently invalidated. Add the chunker identity to `_config_hash` (`indexer.py:32`) so the existing "Config has changed… run with --force" warning fires, and document the reindex path. Never auto-`--force`.
- **Embedder interface stays `EmbedderProtocol`** (`embeddings/base.py`): `embed(texts, task_type) -> list[list[float]]` and `embed_query(query) -> list[float]`. New behavior is additive.
- **Run tests from repo root:** `cd /Users/andrew.mueller/Projects/ZotPilot`. Follow existing test style (real SQLite/Chroma fixtures where used; monkeypatch network calls — never hit a live Ollama/Gemini endpoint in a test).
- **Library decision (confirm before Phase B):** primary recommendation is **LlamaIndex** (`llama_index.core.node_parser.SentenceSplitter`) — most mature token-aware splitter, accepts an arbitrary tokenizer, de-facto RAG standard. The `ChunkerProtocol` seam keeps **txtai**/**Haystack** as drop-in alternatives if dependency weight is a concern.

---

## Phase A — Reliability pass (no new runtime dependencies)

### Task 1: Token-aware truncation safety net at the embedding boundary

Closes Issue #3 defensively: even a chunk that slips past the splitter must never fail the whole document. Each embedder truncates any input that exceeds the model's token budget (with a logged warning) instead of sending it raw.

**Files:**
- Modify: `src/zotpilot/embeddings/base.py` (add `max_input_tokens` to the protocol contract as an optional attribute; add a shared `truncate_to_token_budget` helper)
- Modify: `src/zotpilot/embeddings/ollama.py` (truncate per input before POST; send in sub-batches so one bad input cannot fail the batch)
- Test: `tests/test_embedding_truncation.py` (create)

**Interfaces:**
- Consumes: `OllamaEmbedder(model, base_url, timeout, dimensions)` (`embeddings/ollama.py:21`).
- Produces:
  - `embeddings.base.truncate_to_token_budget(text: str, max_tokens: int, est_chars_per_token: int = 3) -> str` — conservative char-based pre-truncation (no tokenizer dep in core).
  - `OllamaEmbedder.max_input_tokens: int` (default 512) and `OllamaEmbedder.embed_batch_size: int` (default 16); `embed()` truncates each text and POSTs in sub-batches.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embedding_truncation.py
"""Embedding inputs over the model token budget are truncated, not failed."""
import httpx
import pytest

from zotpilot.embeddings.base import truncate_to_token_budget
from zotpilot.embeddings.ollama import OllamaEmbedder


def test_truncate_to_token_budget_caps_length():
    text = "word " * 10_000  # ~50k chars
    out = truncate_to_token_budget(text, max_tokens=512, est_chars_per_token=3)
    assert len(out) <= 512 * 3
    assert out  # non-empty


def test_ollama_embed_truncates_oversized_input(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["inputs"] = json["input"]
        n = len(json["input"])
        return httpx.Response(200, json={"embeddings": [[0.0] * 1024 for _ in range(n)]})

    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.post", fake_post)
    emb = OllamaEmbedder(model="bge-large", dimensions=1024)
    huge = "token " * 20_000
    vecs = emb.embed([huge])
    assert len(vecs) == 1 and len(vecs[0]) == 1024
    # the text actually sent must be within the conservative char budget
    assert all(len(t) <= emb.max_input_tokens * 3 for t in captured["inputs"])


def test_ollama_embed_subbatches(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append(len(json["input"]))
        n = len(json["input"])
        return httpx.Response(200, json={"embeddings": [[0.0] * 1024 for _ in range(n)]})

    monkeypatch.setattr("zotpilot.embeddings.ollama.httpx.post", fake_post)
    emb = OllamaEmbedder(model="bge-large", dimensions=1024)
    emb.embed_batch_size = 4
    vecs = emb.embed(["x"] * 10)
    assert len(vecs) == 10
    assert calls == [4, 4, 2]  # sub-batched, not one giant request
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_truncation.py -v`
Expected: FAIL with `ImportError: cannot import name 'truncate_to_token_budget'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/zotpilot/embeddings/base.py`:

```python
def truncate_to_token_budget(text: str, max_tokens: int, est_chars_per_token: int = 3) -> str:
    """Conservatively cap text to a token budget using a chars-per-token estimate.

    Uses a deliberately low chars/token ratio (3, not 4) so dense/technical text
    stays under the model limit without a tokenizer dependency. Phase B replaces
    this estimate with the model's real tokenizer.
    """
    max_chars = max_tokens * est_chars_per_token
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
```

Modify `src/zotpilot/embeddings/ollama.py` — add the two attributes and rewrite `embed`:

```python
from .base import truncate_to_token_budget

# inside __init__, after self.dimensions = dimensions:
        self.max_input_tokens = 512
        self.embed_batch_size = 16

    def embed(self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if not texts:
            return []
        safe = [truncate_to_token_budget(t, self.max_input_tokens) for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(safe), self.embed_batch_size):
            batch = safe[i : i + self.embed_batch_size]
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": batch},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            out.extend(resp.json()["embeddings"])
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_truncation.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/embeddings/base.py src/zotpilot/embeddings/ollama.py tests/test_embedding_truncation.py
git commit -m "fix(embeddings): truncate oversized inputs and sub-batch Ollama embed calls"
```

---

### Task 2: Surface the real embedding error (kill the `UnboundLocalError: 'e'` mask)

The retry path re-raises a deleted exception variable, so the genuine cause (HTTP 400, auth failure) is lost. Bind the last exception explicitly and chain it, so `IndexResult.reason` and logs carry the real cause.

**Files:**
- Modify: `src/zotpilot/embeddings/gemini.py` (the retry loop that produces the masked error) and `src/zotpilot/embeddings/dashscope.py` if it shares the pattern
- Test: `tests/test_embedding_error_surfacing.py` (create)

**Interfaces:**
- Consumes: `EmbeddingError` (`embeddings/gemini.py`).
- Produces: on exhausted retries, embedders raise `EmbeddingError` whose message contains the original exception's type and text, with `raise ... from last_exc`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embedding_error_surfacing.py
"""Embedding failures report the real cause, never UnboundLocalError on 'e'."""
import httpx
import pytest

from zotpilot.embeddings.gemini import EmbeddingError, GeminiEmbedder


def test_gemini_surfaces_real_http_error(monkeypatch):
    def fake_post(*a, **k):
        return httpx.Response(400, json={"error": {"message": "API key not valid"}})

    monkeypatch.setattr("zotpilot.embeddings.gemini.httpx.post", fake_post, raising=False)
    emb = GeminiEmbedder(model="x", dimensions=1024, api_key="bad", timeout=1.0, max_retries=2)
    with pytest.raises(EmbeddingError) as ei:
        emb.embed(["hello"])
    msg = str(ei.value)
    assert "UnboundLocalError" not in msg
    assert "400" in msg or "API key" in msg
```

(Adapt the monkeypatch target to the actual HTTP client used in `gemini.py`. If the provider SDK is used instead of `httpx`, patch the SDK call the embedder makes. Read `gemini.py` first and patch its real call site.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_error_surfacing.py -v`
Expected: FAIL — either `UnboundLocalError` leaks through, or the raised message does not contain the real cause.

- [ ] **Step 3: Write minimal implementation**

In `src/zotpilot/embeddings/gemini.py`, replace the retry loop's exception handling with an explicitly-bound last exception (representative shape — match the real loop):

```python
last_exc: Exception | None = None
for attempt in range(1, self.max_retries + 1):
    try:
        # ... existing request ...
        return embeddings
    except Exception as exc:            # bind to a name that survives the loop
        last_exc = exc
        logger.warning(
            "Embedding batch failed (attempt %d/%d): %s: %s",
            attempt, self.max_retries, type(exc).__name__, exc,
        )
raise EmbeddingError(
    f"Embedding failed after {self.max_retries} attempts: "
    f"{type(last_exc).__name__}: {last_exc}"
) from last_exc
```

Apply the same pattern in `dashscope.py` if it shares the bug.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_error_surfacing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/embeddings/gemini.py src/zotpilot/embeddings/dashscope.py tests/test_embedding_error_surfacing.py
git commit -m "fix(embeddings): surface real cause on retry exhaustion (no UnboundLocalError mask)"
```

---

### Task 3: Preflight embedder validation before a run

Catch provider/key/reachability problems in seconds, before extracting and embedding hundreds of documents.

**Files:**
- Create: `src/zotpilot/embeddings/preflight.py`
- Modify: `src/zotpilot/indexer.py` (call preflight once at the start of `index_all`, before Phase 1 extraction)
- Test: `tests/test_embedding_preflight.py` (create)

**Interfaces:**
- Consumes: `EmbedderProtocol`.
- Produces: `embeddings.preflight.check_embedder(embedder) -> None` — embeds a one-token probe; on failure raises `EmbeddingError` with a clear, actionable message (provider, model, endpoint). No-op when embedder is `None` (no-RAG mode).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_embedding_preflight.py
import pytest
from zotpilot.embeddings.gemini import EmbeddingError
from zotpilot.embeddings.preflight import check_embedder


class _OK:
    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [[0.0] * 8 for _ in texts]


class _Broken:
    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("connection refused")


def test_preflight_passes_for_working_embedder():
    check_embedder(_OK())  # no raise


def test_preflight_none_is_noop():
    check_embedder(None)  # no-RAG mode


def test_preflight_raises_clear_error_for_broken_embedder():
    with pytest.raises(EmbeddingError) as ei:
        check_embedder(_Broken())
    assert "connection refused" in str(ei.value)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_preflight.py -v`
Expected: FAIL with `ModuleNotFoundError: zotpilot.embeddings.preflight`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/zotpilot/embeddings/preflight.py
"""Fast reachability/auth check for the configured embedder."""
import logging

from .gemini import EmbeddingError

logger = logging.getLogger(__name__)


def check_embedder(embedder) -> None:
    """Embed a tiny probe so misconfiguration fails in seconds, not hours.

    No-op when ``embedder`` is None (no-RAG mode).
    """
    if embedder is None:
        return
    try:
        vec = embedder.embed(["ping"])
    except Exception as exc:
        raise EmbeddingError(
            f"Embedding preflight failed: {type(exc).__name__}: {exc}. "
            f"Check embedding_provider, model, API key, and that the backend is reachable."
        ) from exc
    if not vec or not vec[0]:
        raise EmbeddingError("Embedding preflight returned no vector; check provider/model config.")
```

Modify `src/zotpilot/indexer.py` `index_all` — add immediately before Phase 1 extraction begins (after `self.embedder` exists and items are resolved):

```python
        from .embeddings.preflight import check_embedder
        check_embedder(self.embedder)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_embedding_preflight.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run indexer regression subset**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_indexer.py tests/test_multi_library_indexing.py -q`
Expected: PASS (preflight must not break existing indexer tests; if a test constructs an Indexer with a stub store/embedder, ensure the stub's `embed` returns a vector).

- [ ] **Step 6: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/embeddings/preflight.py src/zotpilot/indexer.py tests/test_embedding_preflight.py
git commit -m "feat(indexer): preflight embedder check fails misconfiguration fast"
```

---

### Task 4: Fix `--limit 0` footgun and the multi-library aggregate summary

`--limit 0` should mean "index nothing" (a true dry/enumerate-reconcile pass), and the multi-library aggregate should report `already_indexed` and the quality/extraction summary correctly.

**Files:**
- Modify: `src/zotpilot/cli.py:337` (limit handling) and `src/zotpilot/indexer.py:360` (apply limit when `limit is not None`, not when truthy)
- Modify: `src/zotpilot/indexer.py` `index_all_libraries` aggregation (don't sum each library's full-store `already_indexed`; aggregate `quality_distribution`/`extraction_stats`)
- Test: `tests/test_multi_library_indexing.py` (append) and `tests/test_cli_setup.py` (the existing `test_index_cli_defaults_to_batch_size_two` neighborhood)

**Interfaces:**
- Consumes: `index_all_libraries` (PR #2).
- Produces: `index_all_libraries(...)` returns `already_indexed` = count of distinct indexed docs across the protected union (not the per-library sum), plus aggregated `quality_distribution` (summed per grade) and `extraction_stats` (summed counters).

- [ ] **Step 1: Write the failing test (limit semantics)**

```python
# append to tests/test_multi_library_indexing.py
def test_limit_zero_indexes_nothing(tmp_path, monkeypatch):
    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _FakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _FakeIndexer)
    # A fake whose index_all asserts it received limit == 0 (not None)
    result = index_all_libraries(cfg, limit=0)
    for inst in _FakeIndexer.instances:
        assert inst.captured["limit"] == 0
```

In the single-library `Indexer.index_all`, the fix is the slice guard. Add a focused unit test in `tests/test_indexer.py` that builds an Indexer over a 3-item fixture and asserts `index_all(limit=0)` indexes 0 and `index_all(limit=None)` processes all (use the existing indexer test fixtures/fakes).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py -k limit_zero tests/test_indexer.py -q`
Expected: FAIL — current `if limit:` treats 0 as "no limit".

- [ ] **Step 3: Fix limit handling**

In `src/zotpilot/indexer.py:360`, change:

```python
        if limit:
            items = items[:limit]
```

to:

```python
        if limit is not None:
            items = items[:limit]
```

In `src/zotpilot/cli.py:337`, change the `args.batch_size`-style guard so `--limit 0` passes `0` through (only treat a *missing* limit as `None`):

```python
    limit = args.limit  # argparse default None; 0 means "index nothing"
```

(Confirm `args.limit` default is `None`. If a sentinel/`-1` is used, normalize here. Do not couple limit to batch_size.)

- [ ] **Step 4: Write the failing test (aggregate summary)**

```python
# append to tests/test_multi_library_indexing.py
def test_aggregate_already_indexed_is_distinct_not_summed(tmp_path, monkeypatch):
    data_dir = _make_db(tmp_path)
    cfg = _Cfg(zotero_data_dir=data_dir)
    _FakeIndexer.instances = []
    monkeypatch.setattr("zotpilot.indexer.Indexer", _FakeIndexer)
    # _FakeIndexer reports already_indexed=5 for every library; with 2 libraries
    # the aggregate must NOT be 10. Expect the distinct store-union count instead.
    result = index_all_libraries(cfg, batch_size=None)
    assert result["already_indexed"] <= 5
```

- [ ] **Step 5: Fix aggregation**

In `index_all_libraries`, stop summing `already_indexed`; instead report `len(global_pdf_doc_ids(config) & <store indexed ids>)` once, and aggregate `quality_distribution` (sum per grade) and `extraction_stats` (sum counters) across libraries so the CLI summary prints. Remove `already_indexed` from the blind `for k in summed` sum and compute it once after the loop.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_multi_library_indexing.py tests/test_indexer.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/cli.py src/zotpilot/indexer.py tests/test_multi_library_indexing.py tests/test_indexer.py
git commit -m "fix(index): --limit 0 means index-nothing; aggregate already_indexed/quality across libraries"
```

---

## Phase B — Token-aware chunking via a RAG library

### Task 5: Introduce `ChunkerProtocol` and put the current chunker behind it

A swappable seam with zero behavior change, so the LlamaIndex backend (and any future txtai/Haystack backend) drops in without touching the indexer.

**Files:**
- Create: `src/zotpilot/pdf/chunker_base.py`
- Modify: `src/zotpilot/pdf/chunker.py` (the existing `Chunker` already matches the protocol; no logic change — just confirm signature)
- Test: `tests/test_chunker_protocol.py` (create)

**Interfaces:**
- Produces: `pdf.chunker_base.ChunkerProtocol` with `chunk(full_text: str, pages: list[PageExtraction], sections: list[SectionSpan]) -> list[Chunk]`. The existing `Chunker` (`pdf/chunker.py:6`) satisfies it unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chunker_protocol.py
from zotpilot.pdf.chunker_base import ChunkerProtocol
from zotpilot.pdf.chunker import Chunker


def test_chunker_satisfies_protocol():
    c = Chunker(chunk_size=400, overlap=100)
    assert isinstance(c, ChunkerProtocol)


def test_chunker_chunks_simple_text():
    c = Chunker(chunk_size=50, overlap=10)
    chunks = c.chunk("Sentence one. Sentence two. " * 20, pages=[], sections=[])
    assert chunks and all(ch.text for ch in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_chunker_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: zotpilot.pdf.chunker_base`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/zotpilot/pdf/chunker_base.py
"""Chunker interface shared by the char-based and token-aware backends."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Chunk, PageExtraction, SectionSpan


@runtime_checkable
class ChunkerProtocol(Protocol):
    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_chunker_protocol.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/pdf/chunker_base.py tests/test_chunker_protocol.py
git commit -m "refactor(chunker): add ChunkerProtocol seam (no behavior change)"
```

---

### Task 6: Add `LlamaIndexChunker` (token-aware splitting with bge-large's tokenizer)

**Files:**
- Create: `src/zotpilot/pdf/llamaindex_chunker.py`
- Modify: `pyproject.toml` (add a `[project.optional-dependencies]` `llamaindex` extra: `llama-index-core`, `tokenizers`)
- Test: `tests/test_llamaindex_chunker.py` (create; skip if the extra is not installed)

**Interfaces:**
- Consumes: `Chunk`, `PageExtraction`, `SectionSpan` (`..models`); `assign_section_with_confidence`, `is_reference_like_text` (`pdf/section_classifier.py`).
- Produces: `LlamaIndexChunker(chunk_size=480, overlap=100, model_tokenizer="BAAI/bge-large-en-v1.5", hard_cap_tokens=512)` implementing `ChunkerProtocol`. Every emitted `Chunk.text` tokenizes to `<= hard_cap_tokens` under the model tokenizer; page/section metadata is assigned exactly as the char chunker does (reuse the section-classifier helpers).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llamaindex_chunker.py
import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("tokenizers")

from tokenizers import Tokenizer  # noqa: E402
from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker  # noqa: E402


def test_no_chunk_exceeds_hard_cap():
    # Dense text with long tokens to stress the tokenizer vs. chars/4 estimate.
    text = ("supercalifragilistic " * 2000) + ("∑∫∂√≈≠≤≥ " * 500)
    c = LlamaIndexChunker(chunk_size=480, overlap=60, hard_cap_tokens=512)
    chunks = c.chunk(text, pages=[], sections=[])
    tok = c._tokenizer  # the HF tokenizer instance
    for ch in chunks:
        n = len(tok.encode(ch.text).ids)
        assert n <= 512, f"chunk has {n} tokens"
    assert len(chunks) > 1


def test_chunks_carry_section_and_page_metadata():
    c = LlamaIndexChunker(chunk_size=120, overlap=20)
    chunks = c.chunk("Intro text. " * 100, pages=[], sections=[])
    assert chunks and all(hasattr(ch, "section") for ch in chunks)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && pip install -e '.[llamaindex]' && python -m pytest tests/test_llamaindex_chunker.py -v`
Expected: FAIL with `ModuleNotFoundError: zotpilot.pdf.llamaindex_chunker`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/zotpilot/pdf/llamaindex_chunker.py
"""Token-aware chunker backed by LlamaIndex's SentenceSplitter + the model tokenizer."""
from __future__ import annotations

from ..models import Chunk, PageExtraction, SectionSpan
from .section_classifier import assign_section_with_confidence, is_reference_like_text


class LlamaIndexChunker:
    """Split text into chunks guaranteed to fit the embedding model's token window.

    Uses the model's own tokenizer (not a chars/4 estimate), so dense academic
    text cannot produce an over-budget chunk. A hard post-split cap truncates any
    residual outlier as a final safety net.
    """

    def __init__(
        self,
        chunk_size: int = 480,
        overlap: int = 100,
        model_tokenizer: str = "BAAI/bge-large-en-v1.5",
        hard_cap_tokens: int = 512,
    ):
        from llama_index.core.node_parser import SentenceSplitter
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_pretrained(model_tokenizer)
        self.hard_cap_tokens = hard_cap_tokens

        def _token_len(text: str) -> int:
            return len(self._tokenizer.encode(text).ids)

        self._splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            tokenizer=lambda t: self._tokenizer.encode(t).ids,
        )
        self._token_len = _token_len

    def _truncate(self, text: str) -> str:
        ids = self._tokenizer.encode(text).ids
        if len(ids) <= self.hard_cap_tokens:
            return text
        return self._tokenizer.decode(ids[: self.hard_cap_tokens])

    def chunk(
        self,
        full_text: str,
        pages: list[PageExtraction],
        sections: list[SectionSpan],
    ) -> list[Chunk]:
        if not full_text:
            return []

        page_boundaries = [(p.char_start, p.page_num) for p in pages]
        chunks: list[Chunk] = []
        cursor = 0
        for idx, piece in enumerate(self._splitter.split_text(full_text)):
            piece = self._truncate(piece.strip())
            if not piece:
                continue
            # locate char offset for page mapping (best-effort, like the char chunker)
            start = full_text.find(piece[:64], cursor)
            if start < 0:
                start = cursor
            end = start + len(piece)
            cursor = end

            page_num = 1
            for offset, pnum in page_boundaries:
                if offset <= start:
                    page_num = pnum
                else:
                    break

            section, confidence = assign_section_with_confidence(start, sections)
            if section != "references" and is_reference_like_text(piece):
                section, confidence = "references", 1.0

            chunks.append(Chunk(
                text=piece, chunk_index=idx, page_num=page_num,
                char_start=start, char_end=end,
                section=section, section_confidence=confidence,
            ))
        return chunks
```

(If `Tokenizer.from_pretrained` requires network at runtime, document caching the tokenizer locally or vendoring `tokenizer.json`; add an offline fallback to `truncate_to_token_budget` from Task 1.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_llamaindex_chunker.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/pdf/llamaindex_chunker.py pyproject.toml tests/test_llamaindex_chunker.py
git commit -m "feat(chunker): token-aware LlamaIndex chunker fitting the embedding window"
```

---

### Task 7: Wire chunker selection + add chunker identity to the config hash

**Files:**
- Modify: `src/zotpilot/indexer.py` (`__init__` chunker construction at `:177`; `_config_hash` at `:32`)
- Modify: config schema/loader (add `chunker_backend: str = "char"` with values `char` | `llamaindex`)
- Test: `tests/test_multi_library_indexing.py` (append) or `tests/test_indexer.py`

**Interfaces:**
- Consumes: `ChunkerProtocol`, `Chunker`, `LlamaIndexChunker`.
- Produces: `Indexer` selects the chunker from `config.chunker_backend`; `_config_hash` includes `chunker_backend` so switching backends triggers the existing reindex-needed warning.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_indexer.py (or a focused new test module)
from zotpilot.indexer import _config_hash


def test_config_hash_changes_with_chunker_backend(make_config):
    cfg_char = make_config(chunker_backend="char")
    cfg_li = make_config(chunker_backend="llamaindex")
    assert _config_hash(cfg_char) != _config_hash(cfg_li)
```

(Use the existing config factory/fixture in the indexer tests; if none exists, build a minimal config object with the attributes `_config_hash` reads.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_indexer.py -k chunker_backend -v`
Expected: FAIL — `_config_hash` does not yet include `chunker_backend`.

- [ ] **Step 3: Write minimal implementation**

In `src/zotpilot/indexer.py` `_config_hash` (`:32`), append `chunker_backend` to the hashed `data` string:

```python
        f"{getattr(config, 'chunker_backend', 'char')}:"
```

In `Indexer.__init__` (`:177`), select the chunker:

```python
        backend = getattr(config, "chunker_backend", "char")
        if backend == "llamaindex":
            from .pdf.llamaindex_chunker import LlamaIndexChunker
            self.chunker = LlamaIndexChunker(
                chunk_size=config.chunk_size, overlap=config.chunk_overlap,
            )
        else:
            self.chunker = Chunker(chunk_size=config.chunk_size, overlap=config.chunk_overlap)
```

Add `chunker_backend` to the config schema/loader with default `"char"` and validation against `{"char", "llamaindex"}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/andrew.mueller/Projects/ZotPilot && python -m pytest tests/test_indexer.py -k chunker_backend tests/test_multi_library_indexing.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add src/zotpilot/indexer.py tests/test_indexer.py
git commit -m "feat(indexer): select chunker_backend; include it in config hash"
```

---

### Task 8: Migration guidance, dependency extra, and docs

**Files:**
- Modify: `README.md` / `docs/` (chunker-backend docs, reindex guidance)
- Create: `docs/superpowers/specs/2026-06-22-chunking-migration.md` (short migration note)
- Test: none (docs); verification is a manual checklist

**Interfaces:** none.

- [ ] **Step 1: Document the backend + migration**

Write: how to enable token-aware chunking (`pip install zotpilot[llamaindex]`, set `chunker_backend=llamaindex`); that switching backends changes chunk boundaries; that the index will warn "config changed — run with `--force` to re-index"; that a one-time `zotpilot index --force` re-embeds under the new chunker for a consistent collection; that mixed-backend collections are valid but not recommended. Note the bge-large 512-token window and the conservative 480-token target.

- [ ] **Step 2: Document the reliability fixes**

Note the preflight check, the `--limit 0` semantics change (now "index nothing"), Ollama sub-batching/truncation, and improved error messages.

- [ ] **Step 3: Commit**

```bash
cd /Users/andrew.mueller/Projects/ZotPilot
git add README.md docs/
git commit -m "docs: chunker backends, token-aware chunking migration, reliability notes"
```

---

## Delivery: upstream proposal

This work targets the upstream repo **`xunhe730/ZotPilot`** (the fork's parent). Suggested delivery:

- Branch from upstream `main` (or rebase the EconGeo fork branch onto it): `feat/indexing-reliability-and-token-aware-chunking`.
- Open the PR against `xunhe730/ZotPilot` with a constructive framing: the current char-based chunker can emit chunks that exceed the embedding model's token window (causing whole-document failures under Ollama bge-large; see live-run evidence), and the retry path masks the real error. This PR (1) adds a token-aware splitter behind a swappable `ChunkerProtocol`, keeping the char chunker as default, and (2) hardens the embedding boundary, error surfacing, and preflight validation — all additive and opt-in, with the multi-library safety invariant untouched.
- Reference Issue #3 and the multi-library PR (#2) for context.
- Keep the heavy deps behind the `llamaindex` extra so the default install is unchanged.

---

## Self-Review

**Spec coverage:**
- Issue #3 (over-long chunk fails doc) → Task 1 (embedder truncation/sub-batch safety net) + Task 6 (token-aware splitter). ✓
- Masked errors (`UnboundLocalError: 'e'`) → Task 2. ✓
- Silent misconfiguration → Task 3 (preflight). ✓
- `--limit 0` footgun → Task 4. ✓
- Aggregate `already_indexed` over-count + dropped CLI quality/extraction summary → Task 4. ✓
- RAG-library delegation behind a swappable seam → Tasks 5–7, library choice in Global Constraints. ✓
- Migration safety (config hash, opt-in reindex, optional dep) → Global Constraints + Task 7 + Task 8. ✓
- Multi-library safety invariant preserved → Global Constraints (no `index_authority` changes). ✓
- Missing-on-disk PDFs counted as unindexed → **NOT yet a task.** Deferred: it is a stats-accuracy issue, not a reliability/chunking concern, and lives in the path-resolution layer (`ZoteroClient.get_all_items_with_pdfs` vs `current_library_pdf_doc_ids`). Track as a separate follow-up issue rather than expanding this plan's scope.

**Placeholder scan:** Code steps contain concrete code. Two steps explicitly flag "match the real call site" (A2's monkeypatch target, A4's config factory) because the exact retry-loop/fixture shapes must be read from the current source at implementation time — the implementer is instructed to read those files first. No TODO/TBD left as deliverables.

**Type consistency:** `ChunkerProtocol.chunk(full_text, pages, sections) -> list[Chunk]` matches `Chunker.chunk` (`pdf/chunker.py:22`) and `LlamaIndexChunker.chunk` (B2). `truncate_to_token_budget(text, max_tokens, est_chars_per_token)` defined in A1 and reused in B2's offline fallback note. `check_embedder(embedder) -> None` consistent A3. `chunker_backend` string used identically in B3's `_config_hash`, `Indexer.__init__`, and config schema.
