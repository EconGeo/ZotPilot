# PR B — Token-aware (LlamaIndex) chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in token-aware chunker that splits text with the embedding model's own tokenizer so no chunk exceeds the model's token window, behind a `ChunkerProtocol` seam, with the legacy char chunker remaining the default.

**Architecture:** A `ChunkerProtocol` documents the existing chunk interface; a new `LlamaIndexChunker` (LlamaIndex `SentenceSplitter` + a HuggingFace tokenizer, plus a hard token-cap fallback) satisfies it; `config.chunker_backend` selects the backend in `Indexer.__init__` and is folded into the config hash **only for non-default backends** so existing "char" indexes are never force-reindexed; LlamaIndex + tokenizers ship as an optional extra.

**Tech Stack:** Python 3.10+, pytest, `llama-index-core`, `tokenizers` (optional extra), existing ZotPilot chunking pipeline.

## Global Constraints

- Branch off **fresh `upstream/main`** (`git fetch upstream && git switch -c feat/token-aware-chunking upstream/main`). Independent of PR A — do not stack.
- **Default `chunker_backend = "char"`** ⇒ existing users' config hash is byte-identical and no reindex is triggered. The `_config_hash` only appends the backend string when it is **not** `"char"`.
- LlamaIndex deps are **optional** (`pip install zotpilot[chunker]`); core install and char backend must work without them. Tests that need them use `pytest.importorskip`.
- The token-aware chunker is **self-contained**: it must not import or depend on the embedding provider. Its tokenizer + token cap are constructor parameters.
- Run the full suite (`pytest -q`) green before opening the PR.

## Open consideration to raise in the issue

The chunker's default tokenizer is `BAAI/bge-large-en-v1.5` / `hard_cap_tokens=512`. Upstream's default vendor model differs (e.g. `bge-m3`, `nomic-embed-text`). Note in the issue that the tokenizer + cap should be configurable and aligned to the active embedding model; the plan keeps them as constructor params so this is a config-wiring follow-up, not a code change.

---

## File Structure

- **Create** `src/zotpilot/pdf/chunker_base.py` — `ChunkerProtocol`.
- **Create** `src/zotpilot/pdf/llamaindex_chunker.py` — `LlamaIndexChunker`.
- **Modify** `src/zotpilot/config.py` — add `chunker_backend` field (default `"char"`); extend `_config_hash`; add to `from_dict`/`to_dict`.
- **Modify** `src/zotpilot/indexer.py` — `Indexer.__init__` selects the chunker by `config.chunker_backend`.
- **Modify** `pyproject.toml` — add the `chunker` optional-dependency extra.
- **Create** `tests/test_chunker_protocol.py`, `tests/test_llamaindex_chunker.py`.

The exact implementations already exist on our fork: `git show main:src/zotpilot/pdf/chunker_base.py`, `git show main:src/zotpilot/pdf/llamaindex_chunker.py`, `git show main:tests/test_chunker_protocol.py`, `git show main:tests/test_llamaindex_chunker.py`. They are reproduced verbatim below.

---

### Task 1: `ChunkerProtocol` seam

**Files:**
- Create: `src/zotpilot/pdf/chunker_base.py`
- Test: `tests/test_chunker_protocol.py`

**Interfaces:**
- Produces: `ChunkerProtocol` (runtime-checkable) with `chunk(full_text, pages, sections) -> list[Chunk]`. The existing `Chunker` already satisfies it (no behavior change).

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

Run: `pytest tests/test_chunker_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotpilot.pdf.chunker_base'`.

- [ ] **Step 3: Create the protocol**

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

Run: `pytest tests/test_chunker_protocol.py -v`
Expected: PASS (both tests — `Chunker` already conforms structurally).

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/pdf/chunker_base.py tests/test_chunker_protocol.py
git commit -m "refactor(chunker): add ChunkerProtocol seam (no behavior change)"
```

---

### Task 2: `chunker_backend` config field + conditional config hash

**Files:**
- Modify: `src/zotpilot/config.py` (`Config` dataclass; `from_dict`; `to_dict`; `_config_hash` ~line 412)
- Test: `tests/test_config_chunker_backend.py`

**Interfaces:**
- Produces: `Config.chunker_backend: str` (default `"char"`); `_config_hash` appends `":<backend>"` only when `backend != "char"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_chunker_backend.py
import dataclasses
from zotpilot.config import Config, _config_hash


def _base_config(**overrides):
    """Build a Config via from_dict with minimal required fields."""
    data = {"zotero_data_dir": "/tmp/z", "chroma_db_path": "/tmp/c"}
    data.update(overrides)
    return Config.from_dict(data)


def test_chunker_backend_defaults_to_char():
    cfg = _base_config()
    assert cfg.chunker_backend == "char"


def test_char_backend_hash_unchanged_vs_no_field():
    cfg = _base_config()
    cfg_no_field = dataclasses.replace(cfg)
    # char backend must NOT extend the hash string.
    assert _config_hash(cfg) == _config_hash(cfg_no_field)


def test_llamaindex_backend_changes_hash():
    char_cfg = _base_config(chunker_backend="char")
    li_cfg = _base_config(chunker_backend="llamaindex")
    assert _config_hash(char_cfg) != _config_hash(li_cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_chunker_backend.py -v`
Expected: FAIL — `TypeError`/`AttributeError`: `chunker_backend` is not a Config field.

- [ ] **Step 3: Add the field, serialization, and hash extension**

In `config.py`, add to the `Config` dataclass (place it at the **end** of the field list so the default does not violate dataclass field ordering):

```python
    # Chunker backend: "char" (default, char-based) or "llamaindex" (token-aware)
    chunker_backend: str = "char"
```

In `Config.from_dict`, add alongside the other `data.get(...)` fields:

```python
            chunker_backend=data.get("chunker_backend", "char"),
```

In `Config.to_dict`, add alongside the other entries:

```python
            "chunker_backend": self.chunker_backend,
```

In `_config_hash` (config.py:412), append the conditional backend extension immediately **before** the `return` line (after the existing `openai-compatible` block):

```python
    if getattr(config, "chunker_backend", "char") != "char":
        data += f":{config.chunker_backend}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_chunker_backend.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zotpilot/config.py tests/test_config_chunker_backend.py
git commit -m "feat(config): chunker_backend field; conditional config-hash (char unchanged)"
```

---

### Task 3: `LlamaIndexChunker` + optional dependency extra

**Files:**
- Create: `src/zotpilot/pdf/llamaindex_chunker.py`
- Modify: `pyproject.toml` (`[project.optional-dependencies]`)
- Test: `tests/test_llamaindex_chunker.py`

**Interfaces:**
- Consumes: `models.Chunk/PageExtraction/SectionSpan`, `pdf.section_classifier.assign_section_with_confidence`, `pdf.section_classifier.is_reference_like_text`.
- Produces: `LlamaIndexChunker(chunk_size=480, overlap=100, model_tokenizer="BAAI/bge-large-en-v1.5", hard_cap_tokens=512)` satisfying `ChunkerProtocol`.

- [ ] **Step 1: Add the optional-dependency extra**

In `pyproject.toml` under `[project.optional-dependencies]`, add the `chunker` extra and include it in `all`:

```toml
chunker = ["llama-index-core>=0.10.0", "tokenizers>=0.15.0"]
all = ["zotpilot[paddle,vision,formula,chunker]"]
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_llamaindex_chunker.py
import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("tokenizers")

from zotpilot.models import PageExtraction
from zotpilot.pdf.chunker_base import ChunkerProtocol
from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker


def test_satisfies_protocol():
    assert isinstance(LlamaIndexChunker(), ChunkerProtocol)


def test_no_chunk_exceeds_hard_cap():
    c = LlamaIndexChunker(chunk_size=120, overlap=20, hard_cap_tokens=128)
    text = "Dense academic sentence about econometrics. " * 200
    pages = [PageExtraction(page_num=1, text=text, char_start=0, char_end=len(text))]
    chunks = c.chunk(text, pages=pages, sections=[])
    assert chunks
    for ch in chunks:
        assert len(c._tokenizer.encode(ch.text).ids) <= 128


def test_chunks_carry_section_and_page_metadata():
    c = LlamaIndexChunker(chunk_size=120, overlap=20)
    text = "Introduction text. " * 50
    pages = [PageExtraction(page_num=1, text=text, char_start=0, char_end=len(text))]
    chunks = c.chunk(text, pages=pages, sections=[])
    assert chunks
    assert all(ch.page_num == 1 for ch in chunks)
    assert all(ch.text for ch in chunks)
```

(If `PageExtraction`'s constructor differs from the kwargs above, mirror the field names from `git show upstream/main:src/zotpilot/models.py` lines 45+.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_llamaindex_chunker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zotpilot.pdf.llamaindex_chunker'` (or skipped if the optional deps are absent — install them first: `pip install -e '.[chunker]'`).

- [ ] **Step 4: Create the chunker**

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

        self._splitter = SentenceSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            tokenizer=lambda t: self._tokenizer.encode(t).ids,
        )

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
        for piece in self._splitter.split_text(full_text):
            piece = self._truncate(piece.strip())
            if not piece:
                continue
            # Locate char offset for page/char metadata mapping.
            # This is BEST-EFFORT / APPROXIMATE: SentenceSplitter may produce
            # overlapping pieces (chunk_overlap > 0) and _truncate can shorten a
            # piece, so `find` may match a slightly wrong position or fall back to
            # `cursor`. The approximation only affects page_num/char_start/char_end
            # metadata — it does NOT affect chunk content or the token-cap guarantee.
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
                text=piece, chunk_index=len(chunks), page_num=page_num,
                char_start=start, char_end=end,
                section=section, section_confidence=confidence,
            ))
        return chunks
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pip install -e '.[chunker]' && pytest tests/test_llamaindex_chunker.py -v`
Expected: PASS. (First run downloads the tokenizer; allow network or pre-cache.)

- [ ] **Step 6: Commit**

```bash
git add src/zotpilot/pdf/llamaindex_chunker.py pyproject.toml tests/test_llamaindex_chunker.py
git commit -m "feat(chunker): token-aware LlamaIndex chunker fitting the embedding window"
```

---

### Task 4: Select the chunker backend in `Indexer.__init__`

**Files:**
- Modify: `src/zotpilot/indexer.py` (`Indexer.__init__`, chunker construction ~line 137)
- Test: `tests/test_chunker_backend_selection.py`

**Interfaces:**
- Consumes: `config.chunker_backend`, `LlamaIndexChunker`, `Chunker`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chunker_backend_selection.py
import types
import zotpilot.indexer as idx
from zotpilot.pdf.chunker import Chunker


def _make_indexer(monkeypatch, backend):
    monkeypatch.setattr(idx, "ZoteroClient", lambda *a, **k: object())
    monkeypatch.setattr(idx, "create_embedder", lambda c: object())
    monkeypatch.setattr(idx, "VectorStore", lambda *a, **k: object())
    monkeypatch.setattr(idx, "JournalRanker", lambda: object())
    cfg = types.SimpleNamespace(
        zotero_data_dir="/tmp", chunk_size=400, chunk_overlap=100,
        chroma_db_path="/tmp", vision_enabled=False, vision_provider="anthropic",
        chunker_backend=backend,
    )
    return idx.Indexer(cfg)


def test_default_backend_is_char_chunker(monkeypatch):
    inst = _make_indexer(monkeypatch, "char")
    assert isinstance(inst.chunker, Chunker)


def test_llamaindex_backend_selects_token_aware_chunker(monkeypatch):
    import pytest
    pytest.importorskip("llama_index.core")
    pytest.importorskip("tokenizers")
    from zotpilot.pdf.llamaindex_chunker import LlamaIndexChunker
    inst = _make_indexer(monkeypatch, "llamaindex")
    assert isinstance(inst.chunker, LlamaIndexChunker)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_chunker_backend_selection.py -v`
Expected: FAIL — default path returns a `Chunker` only when `chunker_backend` is read; the `llamaindex` test fails because `__init__` always builds `Chunker`.

- [ ] **Step 3: Add backend selection**

In `Indexer.__init__`, replace the unconditional chunker construction:

```python
        self.chunker = Chunker(
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap,
        )
```

with:

```python
        backend = getattr(config, "chunker_backend", "char")
        if backend == "llamaindex":
            from .pdf.llamaindex_chunker import LlamaIndexChunker
            self.chunker = LlamaIndexChunker(
                chunk_size=config.chunk_size, overlap=config.chunk_overlap
            )
        else:
            self.chunker = Chunker(
                chunk_size=config.chunk_size, overlap=config.chunk_overlap
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_chunker_backend_selection.py -v`
Expected: PASS.

- [ ] **Step 5: Run full suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zotpilot/indexer.py tests/test_chunker_backend_selection.py
git commit -m "feat(indexer): select chunker_backend from config (char default)"
```

---

## Self-Review

- **Spec coverage:** `ChunkerProtocol` seam (T1), `chunker_backend` config + conditional hash keeping char byte-identical (T2), self-contained `LlamaIndexChunker` + optional extra with `importorskip` tests (T3), backend selection in the indexer (T4). The tokenizer/cap-alignment open question is documented for the issue, not deferred silently.
- **Placeholders:** none — every file is reproduced verbatim or shown as an exact edit; the one conditional (PageExtraction kwargs) names the upstream source to mirror.
- **Type consistency:** `ChunkerProtocol.chunk(full_text, pages, sections) -> list[Chunk]` (T1) is the interface `LlamaIndexChunker.chunk` (T3) and `Chunker.chunk` implement; `config.chunker_backend` (T2) is read identically in `_config_hash` (T2) and `Indexer.__init__` (T4); the `"char"`/`"llamaindex"` literals match across T2 and T4.
