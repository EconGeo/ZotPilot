# Security

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it via GitHub Issues.

## Security Model

- **Zotero database**: Read-only access (`?mode=ro&immutable=1`)
- **ChromaDB**: Local persistent storage, no network access
- **Write operations**: Require explicit Zotero Web API credentials
- **MCP transport**: stdio only (no HTTP endpoints)

## Secrets

Never commit API keys. Required secrets vary by feature:
- `GEMINI_API_KEY` — for Gemini embeddings
- `DASHSCOPE_API_KEY` — for DashScope embeddings (alternative)
- `ANTHROPIC_API_KEY` — for vision table extraction (optional)
- `ZOTERO_API_KEY` + `ZOTERO_USER_ID` — for write operations (optional)

### Where keys are stored

| Location | How it gets there | Exposure risk |
|----------|-------------------|---------------|
| `~/.secrets.env` (`ZOTPILOT_ENV_FILE`) | `zotpilot setup` / `zotpilot config set <secret> <value>`, or edited by hand | Low — one owner-only file, read directly by ZotPilot. **This is the intended location.** |
| Environment variables | User sets manually, for a one-off override | Low — not persisted to disk |
| Shell history | `zotpilot config set zotero_api_key <key>` | Medium — `~/.bash_history` / `~/.zsh_history` |
| `~/.config/zotpilot/config.json` | Legacy installs only | Medium — plaintext on disk. Nothing writes keys here any more; `zotpilot config migrate-secrets` moves what is left and `zotpilot doctor` fails while any remain. |
| OS keychain | Legacy installs only | Low — still read, never written |
| MCP client config (JSON) | Legacy installs only | Medium — plaintext in `~/.claude.json`, `~/.codex/config.toml`, etc. Registration writes no `env` section, and an embedded secret found there forces re-registration without it. |

ZotPilot reads `~/.secrets.env` itself rather than inheriting it from the process
environment. A GUI-launched MCP client starts the server with a minimal environment that
never sourced the user's shell startup files, so a key that exists only as a shell export
would not reach the server.

Resolution order, lowest to highest: `config.json` (legacy) → OS keychain (legacy) →
`~/.secrets.env` → process environment → CLI flag.

### Recommendations

1. **Keep every key in `~/.secrets.env` at mode `0600`.** ZotPilot refuses to read
   credentials from a group- or world-readable file, and hardens the file to `0600`
   whenever it writes one.
2. **Prefer interactive `zotpilot setup`** over `zotpilot config set <secret> <value>` on a
   shared machine: a value passed as an argument lands in shell history. After using the
   flag form, run `history -d <n>` (bash) or edit `~/.zsh_history`.
3. **Migrate legacy installs** with `zotpilot config migrate-secrets`, then confirm with
   `zotpilot doctor` that the `config_secrets` and `secrets_file` checks pass.
4. **Rotate keys** if you suspect exposure. Zotero API keys can be revoked at
   [zotero.org/settings/keys](https://www.zotero.org/settings/keys). Gemini keys at
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
