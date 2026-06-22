# [Issue draft B] Char-based chunker can exceed the embedding model's token window (silent truncation)

**Target:** `xunhe730/ZotPilot` · **Labels:** enhancement · **Author:** @EconGeo

## Summary

The current chunker (`pdf/chunker.py`) splits on **character** counts. Dense
academic text tokenizes to far more tokens per character than prose, so a
char-sized chunk can exceed the embedding model's token window. When that happens
the embedding backend silently truncates the chunk, so the tail of the passage is
never represented in the vector — quietly degrading retrieval for exactly the
information-dense passages that matter most.

I'd like to add an **opt-in** token-aware chunker that splits using the embedding
model's own tokenizer, guaranteeing every chunk fits. Default behavior stays
exactly as today. Opening this first to agree on the shape (and the dependency
question) before sending the PR.

## Proposal

A `ChunkerProtocol` seam + a second backend, selected by config. The char chunker
remains the default.

1. `pdf/chunker_base.py` — a `runtime_checkable` `ChunkerProtocol` documenting the
   existing `chunk(full_text, pages, sections) -> list[Chunk]` interface. The
   current `Chunker` already satisfies it structurally (no behavior change).
2. `pdf/llamaindex_chunker.py` — `LlamaIndexChunker`: LlamaIndex `SentenceSplitter`
   driven by a HuggingFace `tokenizers` tokenizer, with a `hard_cap_tokens`
   post-split truncation as a final safety net. It is **self-contained** — it does
   not import the embedding provider; the tokenizer name + token cap are
   constructor parameters.
3. `config.chunker_backend` (default `"char"`). `Indexer.__init__` picks the
   backend.
4. Config hash: extend `_config_hash` (`config.py:412`) to fold in the backend
   **only when it is not `"char"`**. This keeps every existing index's hash
   byte-identical, so nobody gets a spurious "config changed, reindex needed" on
   upgrade. Switching to `"llamaindex"` does change the hash (correct — it's a
   different embedding space).

## Dependencies

LlamaIndex + tokenizers would ship as an **optional extra**
(`pip install zotpilot[chunker]`), since the backend is opt-in — core install and
the char backend keep working without them. Tests for the new backend use
`pytest.importorskip`. If you'd rather these be core deps, say so and I'll adjust.

## Open question — default tokenizer

My implementation defaults to `BAAI/bge-large-en-v1.5` / `hard_cap_tokens=512`.
ZotPilot's default embedding vendor differs (e.g. `bge-m3` / `nomic-embed-text`),
so ideally the tokenizer + cap should be **configurable and aligned to the active
embedding model** rather than hardcoded. I've kept them as constructor params so
this is just config wiring — happy to wire them to the embedding config in whatever
way fits your provider registry. Guidance welcome on the right default.

## Tests

- `Chunker` satisfies `ChunkerProtocol`; char remains the default backend.
- No chunk exceeds `hard_cap_tokens` (including the decode-truncate fallback).
- char-backend config hash is unchanged vs. pre-field; `llamaindex` changes it.
- `Indexer` selects the token-aware chunker when `chunker_backend="llamaindex"`.

Happy to send the PR once the dependency-as-extra and default-tokenizer questions
are settled.
