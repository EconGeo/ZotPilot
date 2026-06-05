# Changelog

## [Unreleased]

**嵌入 provider 扩展 + 索引数据安全 / Embedding flexibility + index data-safety** — 通用 OpenAI 兼容 provider 与两层厂商配置、单篇 PDF 导读，以及一组 P0 索引数据安全修复。

### ✨ Highlights
- **通用 OpenAI 兼容嵌入 provider**：一套配置接入 SiliconFlow / Zhipu·GLM / Ollama / vLLM / 自建端点，换厂商只改 `base_url` + `model` + `dimensions`
- **两层「厂商 → 模型」配置**：`zotpilot setup` 先选厂商再选模型，交互式 / 非交互式 / Agent skill 共用同一 `VENDOR_CATALOG`
- **`ztp-tutor` 论文导读**：LLM 通读单篇 PDF，五色高亮 + 逐句中文批注直接写回 Zotero 的 PDF，全程本地
- **索引数据安全（P0）**：索引打不开不再静默清空全库，新增 `doctor --recover-index` 零额度恢复
- **嵌入 429 快速中止**：配额耗尽即停、保留已完成索引，不再烧穿整批

### Added
- **通用 OpenAI 兼容嵌入 provider**（Issue #12，嵌入部分）—— 新增 `openai-compatible` provider，对接任意 OpenAI 兼容 `/embeddings` 端点（SiliconFlow / Zhipu·GLM / Ollama / vLLM / 自建），换厂商只配 `embedding_base_url` + `embedding_model` + `embedding_dimensions`；维度必须显式指定、永不自动探测。取代厂商专用的 Ollama PR #16（感谢 @EconGeo）。固定维度端点（如 SiliconFlow `BAAI/bge-m3`）以 HTTP 400 拒绝 `dimensions` 时自动丢弃重试；vision 的 OpenAI 兼容支持暂缓至后续 issue
- **两层「厂商 → 模型」配置** —— 由单一 `VENDOR_CATALOG` 驱动，交互式向导先选厂商再选模型，非交互式支持 `--provider siliconflow --embedding-model BAAI/bge-m3`（固定 base 厂商自动带 `base_url` + 维度）；新增 `setup --list-vendors [--json]` 与 `setup --non-interactive --verify`。旧 `--provider gemini|dashscope|local|openai-compatible` 仍作别名；运行时 provider 集合与 `_config_hash` 不变（不触发重建索引）
- **自定义 Gemini base URL**（Issue #11）—— 通过 `GEMINI_BASE_URL` 或 `config set gemini_base_url` 指定 Gemini 端点，方便 API 代理 / 受限网络；仅接受 `https://`
- **`ztp-tutor` 论文导读** —— `/ztp-tutor <标题>` 匹配本地文献后由 LLM 通读全文，将五维彩色高亮、逐句中文批注、图表 / 公式标注与论证结构便签写入 Zotero 的 PDF，按"阅读画像"自适应、尊重已有批注；写前自动 `.ztpbak` 备份、原子替换保证原文不损。配套 MCP 工具 `get_paper_for_tutor` / `annotate_pdf` / `save_reading_persona`
- **`zotpilot doctor --recover-index`** —— 从完好 SQLite + HNSW 段重建向量库，复用已有向量、零嵌入 API 调用；先写新目录过校验门再原子换库，失败保留原库。`--source` 指定备份、`--dry-run` 预览，HNSW 不可读时可回退重嵌
- **`zotpilot doctor --reconcile`** —— 预览 / 清理 Zotero 已删除的孤儿索引文档，受删除下限保护，`--force` 越过 25% 下限
- **可选依赖 extra `recover`**（`chroma-hnswlib`）—— 仅索引恢复路径需要；缺失时提示 `uv sync --extra recover`（Python 3.13 暂无预编译 wheel，需 C++ 编译器或改用重嵌回退）

### Changed
- **索引打不开不再自动「搬走 + 重建空库」**（破坏性变更）—— 旧版探针失败会把整库移走并原位重建空库、曾静默清空完好数据；现改为报错保留数据（`IndexUnavailableError`）并引导 `doctor --recover-index`

### Fixed
- **P0 索引「打不开即静默清空」**（RC1/RC2）—— 探针改为只读、不加载 HNSW、子进程带超时；段错误判为「不可用」而不再搬移 / 清空完好库
- **批量误删保护**（RC6）—— 孤儿对账常开删除下限：空读 / 数据目录不可达 / 删除超 25% 即拒删告警，`--force` 仅放行比例下限
- **嵌入维度不匹配**（RC7）—— CLI 与 `doctor` 捕获 `EmbeddingDimensionMismatchError` / `IndexUnavailableError`，给出可操作提示而非崩溃
- **配置漂移**（RC8）—— 影响索引内容的配置变化且未 `--force` 时硬阻断（`ConfigDriftError`），避免混合嵌入空间
- **嵌入 429 配额级联**（Issue #15）—— 429 分类为带 `provider` / `retry_after` 的 `RateLimitError`：索引立即中止、未处理论文记为 `failed`、已完成索引保留、lease 正常释放；另加连续 3 篇同特征失败的兜底中止（`systemic_abort`）。以 `counts["rate_limited_abort"] / ["systemic_abort"] / ["not_indexed_due_to_abort"]` 透出

## 如何更新 / How to Update

```bash
zotpilot update              # 自动探测安装方式，更新 CLI + skill 目录
zotpilot update --check      # 只查版本，不安装
zotpilot update --dry-run    # 预览操作，不执行
```

手动更新：`uv tool upgrade zotpilot` 或 `pip install --upgrade zotpilot`

---

## [0.5.0] - 2026-04-28

**架构重构 / Architectural Refactor** — 重新设计入库流程、精简工具层、新增浏览器扩展。

### ✨ Highlights
- **Connector 浏览器扩展**：AI agent 可通过你的浏览器保存论文到 Zotero，自动带上机构订阅的 PDF
- **一步入库**：给 agent 一组 DOI / arXiv ID / URL，它帮你全部存进 Zotero 并验证 PDF
- **18 个精简工具**（原 33 个）：合并冗余，每个工具做一件事
- **Research 工作流**：4 个声明式 Skill 引导 agent 完成"搜索 → 入库 → 整理 → 报告"全流程
- **索引可靠性大修**（Issue #7）：增量索引、中断恢复、不再丢失已完成的索引数据

### Added
- **`zotpilot install` 命令别名** — 与 `zotpilot register` 等价，用作统一的多平台安装/注册入口
- **Connector 浏览器扩展** — 基于 Zotero Connector fork，加入 AI agent 调用路径。Agent 通过本地 bridge 触发浏览器保存，带机构权限下载 PDF。从 [GitHub Release](https://github.com/xunhe730/ZotPilot/releases) 下载 zip，加载到 Chrome 即可
- **`ingest_by_identifiers` 工具** — 给 DOI / arXiv ID / URL 即可入库，自动去重、验证 PDF、失败时走 API fallback。返回每篇论文的最终状态（`saved_with_pdf` / `saved_metadata_only` / `duplicate` / `failed`）
- **`profile_library` 工具** — 分析文献库的主题分布、期刊结构、时间跨度，帮助 agent 理解你的研究方向
- **`search_academic_databases` 全参数搜索** — OpenAlex 检索支持 `min_citations`、`concepts`、`institutions`、`source` 等 filter，cursor-based 分页
- **`zotpilot update` 命令** — 一键升级 CLI + skill 目录
- **版本漂移检测** — MCP server 启动时检查已部署 skill 版本，不匹配时提示更新
- **增量索引** — 基于 PDF hash 跳过已索引文档，中断后从断点恢复，不重复处理
- **索引并发保护** — 防止多个 agent 同时索引导致重复数据
- **入库即时验证** — Connector 保存后通过本地 Zotero API 验证 itemType + title，自动识别并清理出版商 translator 产生的网页快照垃圾 item，失败时走 DOI API fallback

### Changed
- **安装/注册用户入口收敛** — 推荐入口统一为 `zotpilot setup`（首次配置）和 `zotpilot install` / `zotpilot register`（重注册 / 修复 drift），不再向终端用户暴露 `register --dev`
- **多平台注册失败传播** — `update` / `sync` 遇到部分平台注册失败时会显式失败并列出平台，不再假成功
- **Claude Code 注册语法修正** — stdio 注册改为 `claude mcp add ... -- <command>`，兼容 `uv run --directory ...`
- **AGENTS.md / CLAUDE.md** — 同步到 v0.5.0 三 Agent 协作模型（Claude / OpenCode / Codex），更新架构描述和文档维护规则
- **MCP 工具从 33 个精简到 18 个**：
  - `search_papers` 新增 `section_type` 参数，可搜表格和图表（替代 `search_tables` / `search_figures`）
  - `ingest_by_identifiers` 支持 URL 输入（替代 `save_urls`）
  - `manage_collections` 支持 `action="create"`（替代 `create_collection`）
  - `index_library` 支持 `item_keys` 参数局部重索引（替代 `reindex_degraded`）
- **入库流程同步化** — 不再需要轮询状态或多步确认，一次调用返回完整结果
- **Skill 系统** — 4 个声明式 skill（`ztp-research` / `ztp-review` / `ztp-profile` / `ztp-setup`）替代旧的路由器模式，由平台原生机制自动选择
- **平台支持收敛到 3 个** — Claude Code / Codex CLI / OpenCode 为官方支持平台（Gemini CLI / Cursor / Windsurf 不再维护适配，MCP 工具仍可用但不保证）

### Removed
- **状态机工具** — `confirm_candidates` / `approve_ingest` / `get_batch_status` 等 7 个多步确认工具，被 `ingest_by_identifiers` 一步替代
- **`switch_library`** — 多文献库切换推迟到未来版本
- **旧工具别名** — `search_tables`、`search_figures`、`save_urls`、`create_collection`、`reindex_degraded` 等已合并到对应工具

### Fixed
- **Bridge 认证改为 Origin 白名单** — 原 `X-ZotPilot-Token` 方案存在根本缺陷：`/status`
  公开下发 token + `Access-Control-Allow-Origin: *` 导致任意网页都能两步拿到 token
  并调用 `/enqueue`。同时扩展与 bridge 的 token 契约跨仓库未同步（`f0d8c96` 只改了
  主仓库，发布用的 fork 仓库扩展从未跟进），造成 v0.5.0 内测期所有 Connector 保存
  全部 401。改为 Origin 白名单：浏览器强制附加不可伪造的 `Origin` header，bridge 只
  放行 `chrome-extension://` / `moz-extension://` / `safari-web-extension://` 前缀
  和无 Origin（CLI/MCP）的请求，其他一律 403。安全上真正防住了"恶意网页调用 bridge
  写入 Zotero"的攻击面；架构上无共享 secret，扩展与 bridge 可独立升级
- **Preflight 真正阻塞 + 分级 blocking** — 检测到反爬页面时阻塞整个批次要求用户介入，不再悄然降级为 API fallback；分级策略：`anti_bot_detected` / `subscription_required` 封 publisher 域，`preflight_timeout` / `preflight_failed` 只封单 URL（不误伤 IEEE / Springer SPA 慢 hydration 的无关条目）
- **DOI suffix 接受 `.` 字符** — `identifier_resolver._DOI_RE` 从 `[^\s\)\"\',;\.\?]+` 改为 `\S+`，不再误拒 Elsevier / IEEE 风格 DOI（如 `10.1016/j.jcp.2022.111902`、`10.1109/jas.2023.123537`）。与上游 `search.is_doi_query` 对齐
- **OpenAlex SSL 首连重试** — `_request` 现在捕获 `httpx.RequestError` 并按现有 backoff 重试（原代码仅 429 走重试路径，TLS 首连抖动会直接挂）
- **`state._init_lock` 自死锁** — `_get_library_override()` 去掉无意义的 lock acquire（持有者二次 acquire 非 `RLock` 导致 MCP `tools/call` 永不返回）
- **active_candidates 对象一致性** — `run_preflight_check` 接收 `active_candidates` 引用，保证 preflight 操作的对象与后续处理的对象为同一实例
- **ArXiv API 改用 HTTPS** — `identifier_resolver` 中 ArXiv API 端点从 `http://` 改为 `https://`
- **代码质量（P0–P2）** — 修复 `section_type` 验证、`chunk_index` 边界保护、`year_min=0` 过滤异常、消除死代码赋值
- **Issue #7：索引中断丢数据** — 增量索引基于 PDF hash，中断后自动从断点恢复；清理 ChromaDB 中的 stale 孤儿记录
- **arXiv DOI 路由** — `10.48550/arXiv.xxx` 格式的 DOI 正确路由到 arXiv API（CrossRef 不索引这类 DOI）
- **PDF 提取冷启动** — 硬化 PDF fallback 链，修复首次索引时的提取失败
- **API 密钥不再写入配置文件** — `config save()` 跳过所有 API key 字段
- **MCP 配置文件权限** — Unix 上自动设为 0600，防止其他用户读取
- **OpenAlex 请求限流** — 添加 rate limiter 和 429 重试，避免触发 API 封禁

### 从 v0.4 升级 / Upgrading from v0.4

```bash
pip install --upgrade zotpilot     # 或 uv tool upgrade zotpilot
zotpilot install                   # 必须：工具签名变了，需重新注册
```

Connector 浏览器扩展是 Research 工作流的核心组件，从 [GitHub Release](https://github.com/xunhe730/ZotPilot/releases) 下载安装到 Chrome。没有 Connector，入库功能降级为 metadata-only（无 PDF），纯 URL 入库会失败。搜索、引用、整理功能不受影响。

如果你之前通过 `register --gemini-key` 传入 API 密钥，升级后改用 `zotpilot config set gemini_api_key <key>` 保存（更安全，不进 shell history）。

---

## [0.4.0] - 2026-03-24

### Added
- `bridge` CLI 子命令：`zotpilot bridge [--port N]` 手动启动 HTTP bridge 服务（为后续浏览器扩展集成做基础设施准备）

### Fixed
- pyzotero `url_params` 泄漏
- Zotero API `qmode` 参数修复

---

## [0.3.1] - 2026-03-23

### Added
- `status --json` 新增 version 字段
- `--version` flag
- Cursor / Windsurf 升级为 Tier 1

### Fixed
- Windows `zotpilot update` 文件锁定时输出友好提示
- 收窄异常类型、路径比较安全性、文件编码显式指定
- ruff lint / mypy 全部通过

---

## [0.3.0] - 2026-03-23

### Added
- `zotpilot update` 一键更新命令，自动探测安装方式（uv / pip / editable），同时更新 CLI 和所有平台 skill 目录
- `--check` / `--dry-run` / `--cli-only` / `--skill-only` 标志
- Skill 目录升级安全检查：跳过符号链接、脏工作树、非 ZotPilot 仓库

---

## [0.2.1] - 2026-03-23

### Added
- 论文摄取：`search_academic_databases`、`add_paper_by_identifier`、`ingest_papers`（Semantic Scholar 搜索 + Zotero 导入）
- `config` CLI 子命令：`set` / `get` / `list` / `unset` / `path`
- Semantic Scholar API key 支持（`S2_API_KEY`）
- `switch_library` 工具：切换用户/群组文献库
- `get_annotations` 工具：读取高亮和评论

### Fixed
- API key 优先级：环境变量现在优先于配置文件

---

## [0.2.0] - 2026-03-19

### Added
- No-RAG 模式：`embedding_provider: "none"` 可在不配置 embedding 的情况下使用元数据搜索、笔记、标签等基础功能

---

## [0.1.5] - 2026-03-19

### Added
- `get_feeds` 工具：列出 RSS 订阅或获取订阅条目

---

## [0.1.4] - 2026-03-19

### Added
- `get_notes` / `create_note` 笔记工具
- `advanced_search` 高级元数据搜索（年份/作者/标签/集合等，无需索引）

---

## [0.1.3] - 2026-03-19

### Changed
- 批量工具合并：`batch_tags(action="add|set|remove")`、`batch_collections(action="add|remove")`，工具数 29 → 26
- 所有工具 docstring 精简

---

## [0.1.2] - 2026-03-19

### Added
- 查询缓存：相同查询不再重复调用 embedding API
- 批量写操作工具（最多 100 条）

### Removed
- 内置中英翻译（改由 Agent 负责）

---

## [0.1.1] - 2026-03-19

### Fixed
- 线程安全：所有单例初始化使用双重检查锁
- ReDoS 漏洞修复
- API key 不再打印到终端
- Collection 缓存在写操作后正确失效

---

## [0.1.0] - 2026-03-16

### Added
- 初始版本：26 个 MCP 工具
- Gemini / Local 嵌入提供方
- 章节感知重排序 + 期刊质量加权
- PDF 提取（文本 + 表格 + 图表 + OCR）
