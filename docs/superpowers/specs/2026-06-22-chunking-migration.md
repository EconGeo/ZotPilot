# Token-Aware Chunking Migration Guide

## Overview

ZotPilot now supports swappable chunker backends, including a token-aware splitter that respects embedding model constraints. This guide covers enabling the new chunker and understanding the migration path.

## Enabling Token-Aware Chunking

### Installation

Install the optional `llamaindex` dependency:

```bash
pip install -e '.[llamaindex]'
```

This adds the `SentenceSplitter` from `llama_index` (token-aware, using bge-large's tokenizer).

### Configuration

Edit your ZotPilot config file and set:

```yaml
chunker_backend: "llamaindex"
```

Options are:
- `"char"` (default): Character-based chunking (existing behavior)
- `"llamaindex"`: Token-aware `SentenceSplitter` (new)

## Migration & Config Hash

When you switch backends, the index configuration hash changes. On the next `zotpilot index` run, the indexer will warn:

```
Config has changed. Run with --force to re-index and regenerate embeddings under the new configuration.
```

This is expected and safe. The hash change triggers the warning to ensure consistency.

## Re-Indexing Strategy

To migrate your entire collection to the new backend:

```bash
zotpilot index --force
```

This is a **one-time, full re-embedding** under the new chunker. Chunks will have different boundaries than before (token-aware rather than character-based), and new embeddings will be generated.

**Why `--force`?**
- Backend switch changes chunk boundaries → old chunks and new chunks are incomparable.
- Running without `--force` after a config change will skip re-embedding (to avoid unnecessary work if config accidentally reverted).
- `--force` guarantees a fresh, consistent index.

## Mixed-Backend Collections

If you have a collection indexed partially under `"char"` and partially under `"llamaindex"`, the indexer will handle it gracefully. **However, this is not recommended** because:
- Chunks from different backends may not be directly comparable (character vs. token boundaries).
- Search quality may be inconsistent across documents indexed under different chunkers.

**Best practice:** Do a clean `--force` reindex after switching backends.

## Token Limits & Safety

The bge-large embedding model has a **512-token context window**. The token-aware chunker targets a **conservative 480-token limit** with a hard 512-token cap.

This means:
- Any single chunk will never exceed 512 tokens (the model's absolute maximum).
- Most chunks will be ≤480 tokens to leave room for safety margin and query expansion.
- If a sentence is longer than the 512-token hard cap, it is truncated to fit.

This replaces the previous char-based chunking, which could produce chunks exceeding the embedding model's token budget, causing document-level indexing failures.

## Reliability Improvements

This release hardens the indexing pipeline with several fixes:

1. **Preflight embedder check**: A 1-token probe runs before indexing. Misconfigured embedders (wrong provider, bad API key, unreachable server) now fail in seconds, not hours.
2. **`--limit 0` semantics**: Now correctly means "index nothing" (previously incorrectly treated as "no limit").
3. **Ollama sub-batching & truncation**: Oversized inputs are transparently truncated to the embedder's token budget and sent in sub-batches. One over-long chunk no longer fails an entire document.
4. **Better error messages**: Embedding errors now surface their real cause (fixed an `UnboundLocalError` mask in the retry path).

## Summary

- Enable with `pip install -e '.[llamaindex]'` and `chunker_backend: "llamaindex"` in config.
- Switch backends once, then run `zotpilot index --force` for a clean reindex.
- Token-aware chunks respect the bge-large 512-token window (480-token target).
- Existing reliability improvements reduce silent failures and improve error diagnostics.
