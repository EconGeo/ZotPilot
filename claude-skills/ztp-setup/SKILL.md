---
name: ztp-setup
description: >
  Use for setting up, updating, or repairing ZotPilot.
  Trigger on: "安装ZotPilot", "配置嵌入模型", "注册MCP", "ZotPilot无法启动", "升级ZotPilot",
  "install zotpilot", "setup zotpilot", "configure embedding provider",
  "register MCP", "zotpilot not found", "zotpilot doctor", "update zotpilot", "zotpilot upgrade",
  or when the user is setting up for the first time, after an upgrade, or when commands are broken.
  Covers: setup → provider selection → API key config → MCP/skill registration → initial index → health check, plus upgrade.
---
# Setup Workflow

## Steps
1. Check installation: `python scripts/run.py status --json` or `uv run zotpilot status`
2. If not installed, install **this fork from GitHub**, ideally into a dedicated
   environment (`micromamba create -n zotpilot python=3.12 -c conda-forge`):
   `pip install git+https://github.com/EconGeo/ZotPilot.git`

   **Never `pip install zotpilot` or `uv tool install zotpilot`.** That name on PyPI is
   upstream `xunhe730/ZotPilot` — a different lineage, not a newer release of this fork. It
   lacks the Ollama embedding provider, multi-library indexing, token-aware chunking,
   ChromaDB batching, `delete_note` and BBT 7 support, and its `setup` takes flags this fork
   does not implement, so these skills break against it.
3. To update, re-run the fork install:
   `pip install --upgrade --force-reinstall git+https://github.com/EconGeo/ZotPilot.git`
   (dev checkout: `git pull` — an editable install needs nothing else.)

   `zotpilot upgrade` is also safe on a current build: it reinstalls from the URL pip
   recorded for this install and does not consult PyPI. **On a build older than the
   fork-aware upgrade fix it did resolve "latest" from PyPI and could replace this fork
   with upstream.** Tell the two apart with `zotpilot upgrade --check`: if it prints a
   `Latest:` version number, the build predates the fix — use the explicit command above.
   A version gap against PyPI is expected here, not a defect.
4. **Provider Selection**: Determine the user's preferred embedding platform.
   - **gemini**: Requires Google API key. Paid, but provides high-quality embeddings.
   - **dashscope**: Aliyun service. Preferred for Chinese users.
   - **local**: No API key required, completely private, but indexing runs slowly.
   - **none**: Not accepted by `zotpilot setup --provider`; use `zotpilot config set embedding_provider none` only when intentionally disabling vector indexing.
5. **API Key Setup**: Prefer interactive `zotpilot setup` on shared machines. API keys are stored in `~/.config/zotpilot/config.json`; do not paste or commit that file.
6. Configure: `zotpilot setup --non-interactive --provider [gemini|dashscope|local]`
7. MCP registration and skill deployment are included in `zotpilot setup`. Advanced repair only: `zotpilot install` (alias: `zotpilot register`).
8. Initial Index: `zotpilot index --limit 20` (first-time quick index)
9. Verify health: `zotpilot doctor`

## Troubleshooting
- If Zotero is not natively detected at standard paths during setup, instruct the user to explicitly define it via the flag: `--zotero-dir /path/to/zotero/data`
