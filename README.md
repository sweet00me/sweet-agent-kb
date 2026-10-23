<!-- 文件说明：Sweet Research Agent 项目总览、现有环境启动、配置、安全边界及维护入口。 -->
# Sweet Research Agent

面向私人知识库的科研助手，结合 LangGraph Agent、RAG 检索与文档集合分析，提供聊天、人工审批、来源核验和文件管理 Web 界面。

项目目前用于 Ubuntu 单机私人部署。模型与 Embedding 通过云端 API 调用；SQLite、ChromaDB、知识库文件和分析结果保存在本地。**本地存储不代表推理全程离线**，使用前应了解资料发送范围与授权条件。

## 已实现功能

| 功能 | 说明 |
| --- | --- |
| 聊天与历史 | React Web UI、SSE 流式响应、会话重命名与软删除；删除历史列表项后保留本地记录及审计日志 |
| 受控工具执行 | 人工审批（HITL）、参数指纹、Execution Guard、SQLite Checkpoint 与请求日志；支持状态查询和断线恢复 |
| 文件管理 | 目录浏览、文件名搜索、分页、上传和下载；PDF、PNG/JPG、Markdown、文本及代码预览，移动端全屏预览 |
| 图片缩略图 | JPG/JPEG/PNG/WebP 等图片懒加载、EXIF 方向处理、受限解码及私有缓存；加载失败回退文件图标 |
| 增量知识库 | MD、TXT、DOCX 解析，结构化分块、稳定 Chunk ID、缓存复用、Embedding 与 ChromaDB 同步 |
| 自动同步 | API 启动后同步一次，此后每轮结束等待 24 小时；不是固定时刻的每日任务 |
| 普通 RAG | 使用 `search_knowledge_tool` 检索相关片段，保留路径、Chunk、版本和引用核验信息 |
| 文档集合分析 | 按明确范围逐篇读取解析全文，提取证据、关联实体、去重统计、比较、整理时间线、核验并报告覆盖情况 |
| 私人部署 | systemd 单 Worker 后端、Nginx 生产静态页面和 API 代理、Tailscale Serve 私有 HTTPS、前端原子发布与回滚 |

文件在线预览与知识库解析是不同能力：支持 PDF 预览，不表示当前增量解析流程支持 PDF。WebP 等缩略图能力也不等于全尺寸预览支持所有图片格式，详见[缩略图说明](docs/file-thumbnails.md)。

## 工作方式

```text
浏览器 / iPhone
  ├─ 开发：Vite :5173 ───────────────┐
  └─ 正式：Tailscale HTTPS → Nginx ──┤
                                    ↓ /api/
                        FastAPI 127.0.0.1:8010
                                    ↓
                        LangGraph Agent + 审批 + 执行守卫
                          ├─ 普通查询 → RAG Top-K 检索
                          └─ 集合任务 → 授权范围内的文档全文分析
                                    ↓
                  本地文件、解析缓存、SQLite、ChromaDB
```

精确片段和单点事实优先检索；跨文档汇总、统计、比较及事件分析使用 `analyze_document_collection`。集合分析沿用已有审批链，范围真正不明确时需要澄清；Top-K 未命中不能证明整个知识库没有相关资料。

集合分析流程：

```text
Catalog → Scope → Reader → Evidence Map/Store
        → Entity Resolution → Reduce → Verification → Coverage
```

支持 `people_events`、`research_parameters`、`general_facts` 三种提取模板。证据保存来源、文档 SHA256、原文引用、位置、结论及不确定性，任务与阶段检查点支持增量保存和恢复。集合分析读取已有解析缓存，不依赖向量检索的 Top-K，也不负责重新建立向量索引。

## 代码目录

```text
app/
  agents/             # Agent 状态、提示词及工具路由
  api/                # FastAPI、聊天/SSE、审批恢复、会话和文件接口
  document_analysis/  # 文档范围、证据、实体关联、归纳、核验及覆盖报告
  memory/             # SQLite Checkpointer 与上下文预算
  observability/      # 审计、Tracing 和模型调用指标
  rag/                # 解析、分块、增量同步、检索及引用核验
  services/           # 存储发现、文件安全访问、上传下载及预览/缩略图
  tools/              # 工具定义及执行守卫
  workflows/          # 人工审批等工作流
frontend/             # Vite + React + TypeScript + Tailwind
deploy/               # systemd、Nginx 及部署辅助程序
scripts/              # 聊天、检索、发布、安装与接管入口
tests/                # 隔离回归测试及部分历史手动演示
docs/                 # 模块说明、修复记录与部署手册
```

## 环境与配置

当前服务器使用 Ubuntu 24.04 LTS、Conda 环境 `agent_kb`（Python 3.11）、Node.js 20.20.2 和 npm 10.8.2。主要 Python 依赖包括 FastAPI、Uvicorn、LangGraph、SQLite Checkpointer、LangChain OpenAI、OpenAI SDK、ChromaDB、python-dotenv、python-docx 和 Pillow。

**仓库尚未提供完整 Python 环境锁定文件。** `requirements-thumbnail.txt` 仅包含缩略图附加依赖，不能用于从零安装全部后端。以下命令面向已配置好的 `agent_kb` 环境；换机器时需先核对依赖、挂载路径和部署配置，不能直接视为通用安装器。

```bash
cd /home/sweet/Workspace/sweet-agent-kb
conda activate agent_kb

# 仅首次创建；保留已有 .env，不覆盖现有密钥和授权配置。
test -e .env || (umask 077; cp .env.example .env)
```

通过本地编辑器配置 `.env`，不要把真实值提交到 Git。除[配置样例](.env.example)中的 Agent、预算和同步参数外，普通 RAG 还需要以下 Embedding 配置：

| 配置 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` | Agent 与已授权分析所用模型 |
| `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL` | 云端 Embedding 凭据和接口 |
| `EMBEDDING_MODEL`、`EMBEDDING_DIMENSIONS` | 必须与已有 Chroma 集合的模型及维度一致 |
| `RAG_AUTO_SYNC_ENABLED` | 默认开启；设为 `0` 可关闭启动同步和周期同步 |
| `RAG_AUTO_SYNC_INTERVAL_SECONDS` | 默认 `86400`，从上一轮结束后计时 |
| `AGENT_CONTEXT_TRIGGER_TOKENS` | 默认 `900000`，是输入压缩触发阈值；需与实际模型容量匹配 |
| `DOCUMENT_ANALYSIS_MAX_*`、`DOCUMENT_ANALYSIS_REQUEST_TIMEOUT_SECONDS` | 集合分析调用、请求大小、核验目标和时间预算 |

上下文计数依赖本地 tokenizer，首次准备方式见[上下文预算](docs/context-budget.md)：

```bash
python -m app.memory.context_budget --install-tokenizer
```

此安装步骤需要下载 tokenizer 资源。已有缓存或自定义路径按上述文档配置。

### 数据与日志

当前路径以 `/home/sweet/Storage` 独立挂载为基础，部分模块及部署文件使用本机绝对路径。代码克隆不包含正式数据库、解析缓存或私人文档，也不会自动恢复现有知识库。

| 位置 | 内容 |
| --- | --- |
| `/home/sweet/Storage/KnowledgeBase/documents/` | 知识库文档；实际索引范围及排除规则由同步配置控制 |
| `/home/sweet/Storage/KnowledgeBase/sqlite/` | 会话 Checkpoint、请求状态、解析/分块状态及分析存储 |
| `/home/sweet/Storage/KnowledgeBase/chroma/` | 本地持久化向量集合 |
| `/home/sweet/Storage/KnowledgeBase/logs/` | 业务日志；自动同步日志为 `auto_sync.log`，状态为 `auto_sync_status.json` |
| `~/.cache/sweet-agent-kb/thumbnails-v1/` | 受限的图片缩略图缓存，不参与知识库扫描 |
| `/srv/sweet-research-agent/` | 正式前端版本、共享哈希资源和回滚链接 |

后端进程日志通过 `journalctl -u sweet-research-agent.service` 查看。日志和检查点可能包含私人路径及对话信息，应与文档一样限制访问。

## 开发启动

**已有正式服务时直接复用 8010，不要再启动第二个后端。** 开发与正式 UI 当前共用后端、数据库、审批状态和文件，开发操作也会影响正式数据。启动前先检查 `systemctl status sweet-research-agent.service` 及端口占用。

仅在后端尚未运行、Storage 正确挂载且环境已配置时，使用单 Worker 启动：

```bash
conda activate agent_kb
cd /home/sweet/Workspace/sweet-agent-kb
RAG_AUTO_SYNC_ENABLED=0 python -m uvicorn app.api.main:app \
  --host 127.0.0.1 --port 8010 --workers 1
```

这里临时关闭自动同步，避免开发启动立即触发文档 Embedding；**普通聊天和 RAG 仍可能调用云端**。不要对正式数据启动多个 Worker 或额外的索引写入进程。

前端初次安装依赖应在独立开发副本或维护窗口进行，已有 Vite 运行时不要用 `npm ci` 替换其依赖目录：

```bash
cd frontend
npm ci                           # 仅首次安装或经审阅的依赖更新
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

默认本机访问 `http://127.0.0.1:5173`，前端 `/api` 代理到 `127.0.0.1:8010`。远程开发可使用已有 SSH 转发或绑定经授权的 Tailnet 地址；已有 5173 服务时复用它。不要为了手机访问直接开放公网监听。

只读健康检查：

```bash
curl --noproxy '*' --fail http://127.0.0.1:8010/api/v1/health
```

Health 返回正常仅表示 HTTP 服务可用，不代表 Agent 空闲、分析已完成或任务可安全重试。

## 审批与隐私边界

- 集合分析默认关闭云端授权。正式云端全文分析需要管理员分别授权 Workflow、Map、Verification，并由用户批准当次工具调用；普通聊天的 API Key 或一次 UI 审批不能替代独立开关。
- `DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM`、`DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK`、`DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM` 默认均为 `0`。Entity/Reduce 云端辅助另有独立开关，默认使用规则和 Python。配置和可信调用要求见[工作流说明](docs/analysis-verification-workflow.md)。
- 集合分析持续排除 `KnowledgeBase/documents/Diary/24.7.16.md`，范围解析、全文读取和历史证据访问均受权限约束。
- 引用位置正确、结论获得原文支持、现实事实真实是三件不同的事。`not_checked`、`unsupported` 和仅通过结构检查的结果不作为已核验事实发布。
- 文档处理完成与结论语义确定性分别报告。覆盖率 100% 不代表事实完全无遗漏；人物身份不明、证据冲突或部分失败必须保留不确定性。
- 当前没有独立多用户认证。应通过 Tailscale grants/ACL 限制可信用户及设备；文件 `root_id` 不是身份凭证。禁止把服务直接暴露到公网，或使用 Funnel 代替私人访问控制。
- 不要提交 `.env`、API Key、私人文档、SQLite、ChromaDB、缓存或运行日志；不要把它们放入 `frontend/public` 或 `VITE_*` 变量。

## 正式部署与更新

使用 **systemd + Nginx + Tailscale Serve**，完整安装、挂载保护、安全接管及手机入口见[部署手册](docs/deployment.md)。配置针对当前主机，不能未经审计直接覆盖其他机器的服务。已经部署的主机无需重复运行首次安装或接管脚本。

前端更新以 `sweet` 用户在项目根目录执行：

```bash
./scripts/deploy.sh --check       # 检查当前正式页面及 API
./scripts/deploy.sh               # lint → 离线前端测试 → build → 原子发布
./scripts/deploy.sh --list        # 查看保留版本
./scripts/deploy.sh --rollback    # 回滚至上一版本
```

发布脚本保留旧版本，失败时保留或恢复旧链接，不重启 FastAPI。后端更新应单独暂停新提交、核对运行任务与审批、等待任务结束，再安排优雅重启。**Checkpoint 不保证外部工具恰好执行一次，未确认请求不能直接重发。**

## 测试

后端离线测试使用临时目录和模拟模型。以下是基础模块的显式回归入口，在项目根目录运行：

```bash
conda activate agent_kb
DEEPSEEK_API_KEY=offline-test RAG_AUTO_SYNC_ENABLED=0 python -m unittest \
  tests.test_document_catalog tests.test_scope_resolver tests.test_document_reader
```

更多分析、审批、SSE、文件和部署测试见对应模块文档。**不要对 `tests/` 直接做全目录发现**：其中保留了可能调用真实云端或访问正式存储的历史手动演示。测试模型替身验证的是程序流程，不是实际模型的语义准确率。

前端检查在 `frontend/` 运行；构建会生成本地 PDF.js 资源和 `dist`，不会自动发布：

```bash
npm run lint
npm test
npm run build
```

2026-10-23 在前一代码提交 `22c3015` 的暂存区导出副本上完成：**422 项后端 unittest、17 组离线脚本检查、38 项前端测试，以及 lint/build 全部通过**；后端测试隔离正式存储并限制网络。该记录不等于真实云端模型、iPhone 真机、重启或掉盘场景全部验收通过。

## 文档索引与当前限制

| 主题 | 文档 |
| --- | --- |
| 私人部署、日常更新与回滚 | [部署手册](docs/deployment.md) |
| 自动同步与并发边界 | [自动同步](docs/auto-sync.md) |
| 检索引用与上下文 | [引用核验](docs/citation-verification.md)、[上下文预算](docs/context-budget.md) |
| 集合分析基础 | [Catalog / Scope / Reader](docs/document-analysis-foundation.md) |
| 证据提取与持久化 | [Evidence Map / Store](docs/evidence-map-store.md) |
| 关联与归纳 | [Entity Resolution / Reduce](docs/entity-resolution-reduce.md) |
| 核验、覆盖及 Agent 集成 | [分析工作流](docs/analysis-verification-workflow.md) |
| 核验效率、预算及回答展示 | [工作流优化](docs/analysis-workflow-optimization.md) |
| 文件管理缩略图 | [接口、缓存和浏览器测试](docs/file-thumbnails.md) |

当前仍以单机、单进程和受控自然语言范围为边界，没有分布式任务队列或独立的分析管理页面。部分文档保留阶段实施时的历史状态，当前实现以代码及后续修复记录为准。完整 Python 依赖锁定、不同主机路径适配及真实设备验收仍需分别处理。
