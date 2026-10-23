# Storage 自动定时同步

状态：已实现。API 启动后自动执行一次，之后每轮结束等待 24 小时（86400 秒），再执行下一轮。依次复用现有的文档解析、增量 Chunk 缓存、百炼 Embedding 和 Chroma 索引同步；支持 MD、TXT、DOCX，沿用现有目录和私密文件排除规则。

## 配置与运行

在项目 `.env` 中配置后重启 API：

```dotenv
RAG_AUTO_SYNC_ENABLED=1
RAG_AUTO_SYNC_INTERVAL_SECONDS=86400
```

默认开启、间隔 24 小时，每轮结束后计时，并非每天固定时刻。API 重启仍会立即执行一次。`RAG_AUTO_SYNC_ENABLED=0` 可关闭。调度随 API 生命周期运行，API 停止期间不会同步；当前部署继续使用 `--workers 1`。

后台任务与 API 检索位于同一个进程。已用临时真实 Chroma 验证：独立进程修改本地索引后，已有读取客户端可能继续返回旧数据；同进程同步能让已有读取客户端看到新版本。Chroma 的[嵌入式部署约束](https://cookbook.chromadb.dev/core/system_constraints/)也要求避免多个进程并行写入同一个存储目录，因此本功能不使用独立 cron 写入进程。

## 并发与失败处理

- Agent 正在执行时，本轮记录 `skipped_busy`，下个周期再尝试；同步期间由同一执行锁保护检索及引用核验。
- 自动任务在生命周期内持有 `KnowledgeBase/sqlite/rag_sync.lock`，写日志失败时也不会释放所有权。API 就绪前必须获得锁；若启动时已有写入进程占用，API 会启动失败，须等其结束后重新启动。旧的解析、分块、向量写入命令均遵守此锁；只读预览仍可使用。
- Storage 未挂载时不创建知识库目录、不扫描、不调用云端，后续周期继续重试。
- 解析、分块或向量预检查失败时停止后续阶段；需要人工复核的文档不会绕过检查上传。Embedding 失败沿用现有的旧版本保护，下个周期重试。
- 未变化的文档和 Chunk 会复用缓存，不重复请求文档 Embedding。原件在同步过程中变化时，本轮失败并留待下次处理。
- 沿用原有保留策略：删除原件不会自动清理历史缓存和向量；涉及缺失原件的预检查异常会写入日志，需人工处理。

状态 SQLite（`sqlite/`）和 Chroma（`chroma/`）使用不同存储文件，不会互相争用同一个 SQLite 文件锁；同一 SQLite 数据库仍只能有一个并发写事务，见 [SQLite 事务说明](https://www.sqlite.org/lang_transaction.html)。当前同步顺序执行解析、分块、Chroma 写入及验证，最后提交该文档的向量状态 SQLite；进程内执行锁与进程间文件锁保护已接入的同步入口。文件锁是建议锁，绕过这些入口直接写同一存储仍可能产生竞争，尤其是 [Chroma 不支持本地多进程并发写入](https://cookbook.chromadb.dev/core/system_constraints/)。

两个数据库之间没有共同事务，因此锁只能防止受保护入口互相并发，不能保证两库在崩溃时一起提交或回滚。例如 Chroma 已写入而 SQLite 状态未提交时，两边会暂时不一致；后续同步会重新校验 Chroma 内容并补记状态，复用已写入的向量。

## 日志与状态

均位于 `/home/sweet/Storage/KnowledgeBase/`：

| 文件 | 内容 |
| --- | --- |
| `logs/auto_sync.log` | 每轮统计、失败原因；单文件 2 MiB，保留 3 份轮转备份 |
| `logs/auto_sync_status.json` | 最近一轮状态、各阶段统计、上次成功时间及下次计划时间，原子写入 |
| `sqlite/rag_sync.lock` | 写入所有权锁；文件保留，进程退出后锁自动释放 |

Storage 未挂载、无法取得所有权或无法写入日志时，原因会记录到 API 进程日志。状态文件的 `last_success_at` 在失败或忙时跳过后继续保留。

手动完整同步会写入本地并使用既有云端 Embedding 配置。先停止 API，执行后再启动 API，保证 Chroma 客户端读取最新索引：

```bash
cd /home/sweet/Workspace/sweet-agent-kb
/home/sweet/miniconda3/envs/agent_kb/bin/python -m app.rag.auto_sync --once
```

## 验证

```bash
/home/sweet/miniconda3/envs/agent_kb/bin/python -m unittest tests.test_auto_sync
```

覆盖新增、修改、未变化不重复 Embedding、排除指定私密来源、解析和云端失败保护、人工复核预检查、挂载恢复、跨进程及旧命令互斥、Agent 忙时跳过、异常后继续调度、优雅退出和真实 Chroma 读取更新。所有测试使用临时目录，云端调用使用离线替身。

2026-10-21 已在现有 API 上启用自动同步，当前周期已改为 86400 秒。首次启用时实际扫描 55 份文档，自动更新 `PC_Files/Files/笔记.txt` 的解析、Chunk 和 16 个向量；其余 54 份文档复用解析缓存。正式向量库为 54 份文档、126 个 Chunk，保留 1 份指定来源的排除规则；随后只读预检查为 54 份未变化、0 份待解析、0 失败。首次启用前已备份 RAG SQLite、解析/分块缓存及 Chroma 至 `KnowledgeBase/backups/auto_sync_before_20261021T094704Z/`，3 条待审批记录保留。

验证结果：自动同步专用 21 项测试及其余相关单元测试共 80 项通过；17 个既有离线测试模块通过；启用后正式 HTTP/SSE 与跨协议重放、来源 SHA 核对均通过（分别 2.90 秒、2.47 秒）。回归日志：`/tmp/sweet-agent-auto-sync-regression/`；正式 API/SSE 验收报告：`/tmp/sweet-agent-auto-sync-api-sse-live.json`。

## 修改的文件

- `app/rag/auto_sync.py`：完整管线、周期调度、日志、状态和单次运行入口。
- `app/rag/sync_lock.py`：跨进程所有权及手动写入互斥。
- `app/rag/document_sync.py`、`app/rag/chunk_sync.py`、`app/rag/vector_sync.py`：原有写入命令接入同一互斥锁。
- `app/api/main.py`：生命周期接入、就绪前取得所有权、退出时等待同步结束。
- `.env.example`：开关与同步间隔。
- `tests/test_auto_sync.py`、`tests/test_citation_api.py`：自动同步回归及既有 API 测试隔离。
- `docs/auto-sync.md`、`docs/citation-verification.md`：配置、验证、日志说明及状态更新。
