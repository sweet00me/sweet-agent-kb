# Evidence Extractor（Map）与 Evidence Store

本阶段提供独立 Python 服务接口，复用 `DocumentCatalog`、`ScopeResolver`、`DocumentReader` 和现有文件/SQLite 安全校验。不改动 RAG、LangGraph、Web UI、解析缓存或向量数据库，不包含 Reduce、Entity Resolution 和 LangGraph 子图。

## 文件清单

新增实现：

- `app/document_analysis/evidence_schema.py`：三类模板、严格 Schema、可注入 `EvidenceExtractor` 协议、引用绑定与位置校验。
- `app/document_analysis/evidence_store.py`：独立 SQLite、事务检查点、状态与来源查询、幂等保存、恢复锁。
- `app/document_analysis/evidence_map.py`：授权范围内逐文档 Map、重叠窗口、失败/取消恢复和完成校验。
- `app/document_analysis/deepseek_evidence.py`：默认关闭、延迟初始化、可注入模拟模型的 DeepSeek 适配器。

新增测试：

- `tests/evidence_support.py`：临时目录、实际解析缓存和离线证据夹具。
- `tests/test_evidence_schema.py`：模板、精确引用、Unicode 定位、歧义引用与输入限制。
- `tests/test_evidence_store.py`：专用库、事务回滚、幂等、来源查询、版本/权限与锁。
- `tests/test_evidence_map.py`：跨批次、增量恢复、取消、进程异常退出、最终版本复查。
- `tests/test_deepseek_evidence.py`：模拟模型、默认拒绝、双重授权和截断/异常响应。

新增文档：本文及 `docs/superpowers/plans/2026-10-21-evidence-map-store.md`。
修改配置示例：`.env.example` 增加 `DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK=0`。实际 `.env` 未修改。

## Schema 与注入接口

| 模板 ID | details 字段 |
| --- | --- |
| `people_events` | `people`、`event`、可选 `time`、`location` |
| `research_parameters` | `experiment`、`parameters[{name,value,unit}]`，数值保留原文字串 |
| `general_facts` | `subject`、`predicate`、`object` |

提取器提供 `identity: ExtractorIdentity` 和 `extract(ExtractionRequest) -> ExtractionResponse`。请求包含模板和当前正文窗口；响应必须显式提供 `complete`，每条候选包含 `quote`、`conclusion`、非空 `uncertainty`、符合模板的 `details`，可选成对的窗口内 `start_char/end_char`。

来源路径、文档 ID、文档 SHA256、解析正文 SHA256、缓存 SHA256、解析器/模板版本均由服务端绑定，模型不能覆盖。引用必须逐字匹配；省略位置时，引用必须在窗口内唯一出现。位置以**完整解析正文的 Unicode 字符**为基准，偏移从 0 开始、结束不含；行列从 1 开始。它不是 UTF-8 字节偏移，也不是 DOCX 页码或原始 XML 位置。

默认每批读取 8192 字符，附带最多 1024 字符的上一批尾部。`MapConfig` 可配置 `batch_chars`（1–32768）与 `overlap_chars`（0–32768），窗口最多 65536 字符。每次响应最多 64 条候选，每条引用最多 4096 字符。重叠区的相同出处与结构化字段生成相同 ID，保留首次结论；不同原文位置仍是不同证据。

## 独立存储与完成条件

默认按需创建 `Storage/KnowledgeBase/sqlite/evidence_store.sqlite3`，不复用解析缓存库或 Agent 执行记录库。可通过 `EvidenceStore(catalog, path=...)` 指定 Storage 内的专用数据库；拒绝软链接、硬链接、越界路径、解析库别名和已有其他业务表的数据库。

| 表 | 用途 |
| --- | --- |
| `analysis_tasks` | 固定范围、模板/提取器/配置版本、任务状态、运行租约 |
| `analysis_documents` | 文档 ID/SHA、状态、尝试次数、字符游标、错误码 |
| `analysis_batches` | 连续批次位置和窗口摘要，恢复检查点 |
| `analysis_evidence` | 完整证据 JSON、载荷摘要、`provisional/verified/invalidated` 状态 |

任务 ID 默认由快照、解析后的范围、模板版本、提取器身份/版本和窗口配置生成。同一任务定义重复调用幂等；自定义任务 ID 若对应不同定义则拒绝。修改提取器提示词或行为时，应提升 `identity.version`，避免复用旧检查点。

一次事务同时写入该批证据、检查点和文档游标。失败事务全部回滚；恢复时先验证当前版本和窗口摘要，只跳过已经提交的批次。未提交调用可能在恢复时再次执行，因此不保证模型调用恰好一次。每个证据由确定性 ID 去重。

只有 Reader 真正耗尽、覆盖完整、所有批次连续提交、来源和缓存版本复查通过，文档才可 `completed`，其证据才提升为 `verified`。任务正常收尾还会复查所有已完成文档，防止处理后续文档时，先前文档已变化却仍报告全任务成功。

- 全部文档通过才能将任务标记 `completed`；一部分通过、一部分失败为 `partial`，没有成功文档为 `failed`。
- `complete:false`、虚构引用、模型截断、解析/权限/版本错误都不能构成成功；失败文档已暂存的证据标记 `invalidated`。
- `cancel=callable` 提供批次之间的协作取消。取消或异常退出保留已提交批次，任务为 `cancelled/interrupted`；再次使用同一范围和配置运行可恢复。
- 每个证据库使用独立的非阻塞 Linux `flock`，同库同时只能有一个 Map 运行者。进程退出会释放锁；新运行者将遗留 `running` 标记为 `interrupted` 后恢复。不同库仍可独立执行。
- 清理失败状态只依赖仍持有的运行租约；权限撤销后不会因为无法读取文档而把任务误报为成功。

## 离线调用示例

下例使用模拟提取器，仅演示接口。请在项目根目录运行；调用会按需创建目录快照库与证据库，不会调用云端或修改向量数据库。

```python
from pathlib import Path
from app.document_analysis.catalog import DocumentCatalog, CatalogFilter
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.evidence_schema import (
    CandidateEvidence, ExtractionResponse, ExtractorIdentity, MapConfig,
)
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.evidence_map import EvidenceMap

class MockExtractor:
    identity = ExtractorIdentity(name="example-mock", version="1", provider="offline")

    def extract(self, request):
        quote = "地球绕太阳运行。"
        start = request.window.text.find(quote)
        candidates = [] if start < 0 else [CandidateEvidence(
            quote=quote, conclusion=quote, uncertainty="模拟示例，仅匹配原文。",
            details={"subject": "地球", "predicate": "绕", "object": "太阳"},
            start_char=start, end_char=start + len(quote),
        )]
        return ExtractionResponse(candidates=candidates, complete=True)

catalog = DocumentCatalog(Path("Storage").resolve())
snapshot = catalog.create_snapshot(CatalogFilter(
    directories=("KnowledgeBase/documents",),
))
resolution = ScopeResolver(catalog).resolve_filter(CatalogFilter(), snapshot.snapshot_id)
if resolution.status != "resolved":
    raise ValueError(resolution.status)
store = EvidenceStore(catalog)
result = EvidenceMap(catalog, store, MockExtractor()).run(
    resolution.scope, "general_facts", MapConfig(),
)
task = store.get_task(result["task_id"])
records = store.list_evidence(result["task_id"])
```

## 查询出处与版本变化

- `get_task(task_id)`：任务定义、状态和各类文档数量。
- `get_document(task_id, source_or_document_id)`：文档状态、版本、游标和错误码。
- `list_evidence(task_id, source=..., document_id=...)`：按任务/文档查询，默认只返回已完成文档的已验证证据。`partial` 任务可能包含成功文档的证据，调用者须同时检查任务状态。
- `get_evidence(evidence_id)`：按 ID 查询；默认同样隐藏未验证证据。
- `get_origin(evidence_id)`：重新授权并校验当前来源，返回证据、状态、快照 ID 和 `version_status`。此接口可返回历史/未完成证据，务必同时检查 `state`；`version_status="verified"` 只表示当前引用与来源版本匹配。
- `include_unverified=True` 可用于受授权的恢复诊断，返回明确状态，不应作为已完成分析结果发布。

所有查询复用当前 Catalog 权限和固定范围校验；强制排除 `KnowledgeBase/documents/Diary/24.7.16.md`，无法通过自造范围、窗口、证据来源或历史查询绕过。

源文件或缓存变化后，旧任务拒绝继续使用不同版本。先通过现有文档同步更新解析缓存，再创建新快照和新任务。旧证据保留原引用、位置和 SHA，`get_origin` 会报告 `source_changed/cache_changed` 等稳定错误码；历史完整正文未另行归档。

## DeepSeek 授权

真实分析必须同时满足：

1. 独立环境开关 `DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK=1`；缺省为 `0`。
2. 显式构造 `DeepSeekEvidenceExtractor(authorized=True)`。

拥有 `DEEPSEEK_API_KEY` 本身不构成授权。默认构造适配器不会初始化客户端或联网，禁用状态下 Map 在创建任务之前就拒绝执行。已授权调用仍逐批检查开关。只有满足双重授权后才延迟使用现有 `DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL/DEEPSEEK_MODEL` 配置；不会调用 Agent 工具或 Graph。适配器设置单次请求超时 60 秒、无隐式重试，要求 JSON 和正常 `stop` 结束。

本轮仅用模拟模型测试授权路径，**未进行真实 DeepSeek 或 Embedding 调用**，真实服务可用性和提取质量尚未在线验证。

## 验证与限制

离线测试使用临时 Storage、真实本地解析器/SQLite、模拟提取器和模拟模型，并禁止外部网络访问。覆盖精确引用与位置、跨批次重叠、幂等与原子回滚、版本变化、失败恢复、取消、`KeyboardInterrupt`、子进程 `os._exit`、权限撤销及 Diary 排除。

```bash
/home/sweet/miniconda3/envs/agent_kb/bin/python -m unittest \
  tests.test_evidence_schema tests.test_evidence_store \
  tests.test_evidence_map tests.test_deepseek_evidence
```

当前新增测试 41 项通过；与原有 Catalog、Scope、Reader、上下文预算、定时同步、引用校验、API/SSE、文件预览及会话管理联合运行，共 195 项测试通过（11.498 秒）。另外 17 组既有离线回归全部通过，涵盖 Agent 行为、原子提交、解析/Chunk/向量同步、文件管理与请求诊断。网络守卫禁止外部连接，HTTP 联动测试仅使用本地回环地址。

限制：

- `complete` 表示所有窗口处理与结构化校验完成，不保证模型没有漏提，也不自动证明结论被引用语义蕴含；不确定性来自提取器，未做独立语义审判。
- 跨 Batch 支持受重叠窗口限制；跨度超过窗口的事实可能漏提。不做跨文档实体消歧、语义去重或全局归纳。
- Reader 仍沿用现有完整解析正文读取方式，分批控制模型输入，未实现从磁盘流式解析；反复 SHA/缓存验证增加长文档处理开销。
- 完成时进行逐文档版本复查，不锁定所有源文件；任务完成之后发生的外部修改需由后续运行或 `get_origin` 发现。
- 同步接口的取消在批次边界检查，不能强制中断正在进行的模型请求；进程中断恢复仅复用已提交批次。
- 不创建 HTTP 路由或界面，本阶段仅完成两个独立基础模块及可选模型适配器。
