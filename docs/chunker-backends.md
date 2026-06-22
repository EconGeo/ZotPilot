# Chunker Backends & Indexing Reliability

ZotPilot supports configurable text chunking backends and includes several reliability improvements to the indexing pipeline.

## Chunker Backends

The `chunker_backend` configuration option selects how PDFs are split into chunks for embedding:

### Character-Based Chunker (default)

```yaml
chunker_backend: "char"
```

The default chunker splits text by character count. It is simple, deterministic, and requires no additional dependencies.

**Limitation:** Character boundaries do not always respect token boundaries. A chunk may exceed the embedding model's token window, causing the entire document to fail indexing.

### Token-Aware Chunker (recommended)

```yaml
chunker_backend: "llamaindex"
```

The token-aware chunker uses `llama_index.core.node_parser.SentenceSplitter` with bge-large's own tokenizer. Chunks are guaranteed to fit within the model's token budget.

**Installation:**

```bash
pip install -e '.[llamaindex]'
```

**Token budget:**
- **Hard limit:** 512 tokens (bge-large context window)
- **Target:** 480 tokens (conservative margin)

If a single sentence exceeds 512 tokens, it is truncated to fit.

## Indexing Reliability Improvements

### Preflight Embedder Check

Before indexing begins, ZotPilot runs a 1-token probe against the configured embedder. This catches misconfigurations immediately:

- Wrong provider (e.g., `provider: ollama` but Ollama is not running)
- Invalid API key or authentication failure
- Unreachable server

**Benefit:** Misconfigured embedders now fail in seconds, not after hours of processing.

### `--limit 0` Semantics

The `--limit 0` option now correctly means "index zero documents" (previously incorrectly treated as "no limit").

```bash
zotpilot index --limit 0  # Indexes nothing; useful for testing pipeline setup
```

### Ollama Sub-Batching & Truncation

When using Ollama embedder, oversized inputs are automatically:
1. Truncated to the model's token budget (`truncate_to_token_budget`)
2. Sent in sub-batches to respect embedding concurrency limits

**Benefit:** A single over-long chunk no longer fails the entire document. The chunk is safely truncated, and the document continues indexing.

### Error Message Clarity

Embedding errors now surface their real cause. Previous versions masked errors with `UnboundLocalError`, making debugging difficult.

**Example:**

```
EmbeddingError: Ollama service unreachable at http://localhost:11434
  Caused by: Connection refused

  To fix: Start Ollama with `ollama serve` and verify the endpoint in your config.
```

## Migration Path

When switching from `"char"` to `"llamaindex"`:

1. Install the optional dependency: `pip install -e '.[llamaindex]'`
2. Update config: `chunker_backend: "llamaindex"`
3. Re-index with `--force`:
   ```bash
   zotpilot index --force
   ```

The `--force` flag triggers a full re-embedding because chunk boundaries have changed (character-based → token-aware). Without `--force`, the indexer skips already-indexed documents (re-embedding only occurs with `--force` or for documents not yet in the index).

**Note:** Mixed-backend collections (some docs indexed under `"char"`, others under `"llamaindex"`) are technically valid but not recommended due to inconsistent chunk boundaries and potential search quality variance.

## Configuration Example

```yaml
# ~/.zotpilot/config.yaml

embedder:
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "bge-large"

chunker_backend: "llamaindex"  # Enable token-aware chunking

# ... rest of config ...
```

## Troubleshooting

### `ImportError: No module named 'llama_index'`

Install the optional dependency:
```bash
pip install -e '.[llamaindex]'
```

### `Config has changed. Run with --force to re-index...`

This message appears when the index configuration hash changes (e.g., after switching backends). It is safe to ignore if you don't need a consistent index. To migrate, run:

```bash
zotpilot index --force
```

### Ollama embedding fails silently

Check the preflight diagnostics:
```bash
zotpilot doctor
```

This runs the embedder probe. If it fails, the error message will indicate the issue (service unreachable, wrong model name, etc.).

## References

- [Token-Aware Chunking Migration Guide](./superpowers/specs/2026-06-22-chunking-migration.md)
- ZotPilot configuration: `~/.zotpilot/config.yaml`
- Upstream chunker protocol: `src/zotpilot/pdf/chunker.py`
