# 文档集合分析：验证、覆盖率与 Agent 集成

本阶段完成 Catalog → Scope → Reader/Map → Entity Resolution → Reduce → Verification → Coverage 闭环，通过现有 LangGraph 的受控工具接入。没有增加多 Agent、知识图谱、新框架或前端页面。普通 RAG、审批、执行守卫、Checkpoint、Tracing 和原 HTTP/SSE 接口继续使用。

## 验证结果如何理解

| 字段 | 含义 | 不代表什么 |
| --- | --- | --- |
| `position_status` | Evidence ID 对应的原文、SHA256、解析缓存版本、Unicode 字符范围及行列是否匹配 | 结论得到原文支持 |
| `citation_status` | 位置正确，且引用 ID、授权范围、上游处理状态和实体内部引用有效 | 现实事实真实 |
| `semantic_status` | `supported` / `partially_supported` / `contradicted` / `unsupported`；未配置语义验证器时为 `not_checked` | 外部独立事实核查或模型判断绝对正确 |
| `real_world_truth` | 始终为 `not_checked` | 已证明现实真实性 |

Map 的原始位置会通过既有 `bind_evidence` 重新绑定；实体提及会与既有规则派生结果核对，关联和分组必须引用真实提及，自动合并仍须有规则依据。语义验证器接收提及 ID 对应的名称与证据，不仅收到不透明 ID。所有语义返回的 Evidence ID 和原文短引文再次逐字校验；限制条件会持久化并进入覆盖率报告。

结构性引用失败标记为 `unsupported`、`support_basis=structural_failure`，不会调用语义模型；这表示声明的引用无法提供有效支持，不能据此判断现实事实为假。已知 Evidence ID 也必须属于生成该结论的任务范围，不能借用其他任务的来源。

Reduce 统计由 Python 对当前证据集合重新计算，不能用模型口述的计数替代。Python 核验通过仅说明这个证据集合的计算一致，不保证人数、事件总数或知识库事实没有遗漏。原先 Entity/Reduce 对象中的 `semantic_verified=False` 保留；新核验结果单独保存，避免混淆阶段含义。

## 覆盖率口径

- `documents`：固定 Scope 中按来源路径去重的 `expected/analyzed/failed/missing/pending`，同一文件在多个 Map 任务中不会重复增加文档分母。一个来源被多个任务处理时，所有请求任务都完成才计入已分析。
- `body.processing_ratio`：通过版本检查的连续已提交批次字符数 / 固定正文长度；重叠 Batch 不重复计数。`completion_ratio` 仅计入完整处理的文档。未知分母返回 `null`，缺失文件与失败分开报告。
- `exclusions`：快照中已知的权限/规则排除和范围过滤数量；不扫描整盘，不能冒充磁盘全量盘点。多个快照无法恢复排除文件的全部身份时，计数标记为下界。
- `verification` / `verification_by_kind`：分别提供总体及 Map 证据、实体、结论、比较、时间线、统计的核验情况。`verification_complete` 表示所有目标已得到核验结论，`all_targets_supported` 另行表示是否全部获支持；发现矛盾也属于完成核验，不能算全部支持。
- `pipeline_complete`：必须存在覆盖整个任务集合、依赖匹配的 Entity → Reduce 链；只分析任务子集，或分别分析各任务，不能冒充已做跨任务关联。
- `analysis_complete`：文档处理、全范围后续阶段及所有目标支持检查均完成，且没有阻断的不确定性。身份不明、冲突、未定时间、上游失败、验证缺失或校验器限制都会保留。即使为 `true`，`facts_exhaustive` 仍为 `false`。

## 调用方式

### 1. 现有 Agent / HTTP / SSE

普通单点事实查询继续使用 `search_knowledge_tool`。全量逐篇分析、跨文档统计、实验比较或时间线问题由提示词引导调用 `analyze_document_collection`，参数如下：

```json
{
  "question": "按时间线整理这些文档记录的事件，并统计证据去重情况",
  "scope_query": "分析 `KnowledgeBase/documents/notes` 目录下全部文档",
  "template_id": "people_events"
}
```

`scope_query` 只描述目录、文件、类型和日期范围；内容问题放在 `question`。模板另支持 `research_parameters`、`general_facts`。可选 `snapshot_id` 固定清单、`analysis_id` 恢复原分析；恢复时原问题、范围、模板和后端绑定必须一致。

继续调用现有 `POST /api/v1/chat` 或 `/api/v1/chat/stream`。需要审批时，从响应的 `pending_approvals` 取得实际工具调用 ID 和指纹，提交到 `/api/v1/threads/{thread_id}/review` 或 `/review/stream`，沿用 `decisions`、`expected_fingerprints` 和 `request_id` 协议。不要自行构造执行许可。

工具策略为 `approve`。执行器校验原审批参数后，才通过隐藏的 `RunnableConfig` 传递绑定工具调用 ID、参数指纹的许可。模型参数中没有 `cloud_authorized` 或权限覆盖项；直接调用工具而不经过守卫会返回 `analysis_approval_required`。

分析答案由服务器选择已通过核验的 Reduce 结果生成，按问题呈现事实、统计、比较或时间线，带 `【Evidence:<ID>】` 和来源位置。聊天最多展示 20 项结果，不限制全文读取；完整阶段结果保存在 SQLite。同一轮同时调用 RAG 与集合分析时，RAG 答案仍走原核验链，再附加集合分析报告，不由模型改写分析统计。

纯分析的 `citation_report.mode=document_analysis`；其中 `status=not_applicable` 仅表示不使用 RAG Chunk 引用格式，分析是否完整看 `analysis_status/analysis_complete`。混合调用为 `mode=rag_and_document_analysis`，集合分析情况在 `document_analysis` 子字段中。SSE 继续发送原有审批、节点、已核验答案及完成事件，不向前端泄漏模型草稿。

### 2. 离线 Python 接口

已有解析缓存必须可用；本工作流不负责重新解析、同步或写入向量库。可信调用方可注入纯离线提取器和语义验证器：

```python
from app.document_analysis.catalog import DocumentCatalog
from app.document_analysis.workflow import AnalysisWorkflow

# extractor：实现现有 identity / extract(ExtractionRequest) 接口。
# validator：实现 identity / verify(VerificationRequest) 接口。
# 离线实现的 identity.provider 必须为 "offline"。
workflow = AnalysisWorkflow(DocumentCatalog(), extractor, validator=validator)
response = workflow.run(
    question="比较学习率并统计当前证据集合",
    scope_query="分析 `KnowledgeBase/documents/notes` 目录下全部文档",
    template_id="research_parameters",
)
analysis_id = response["run_id"]
current = workflow.status(analysis_id)
resumed = workflow.resume(analysis_id)
```

`validator=None` 可以运行机械核验及 Python 统计，但语义检查会明确为 `not_checked`，不会发布未经支持验证的事实结论。测试模拟器仅用于验证程序流程，不能当作实际语义验证器。

独立调用接口：

```python
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.coverage import CoverageReporter

# store 是复用既有 EvidenceStore 的 SynthesisStore。
verified = EvidenceVerifier(store, validator).run(task_ids, derived_run_ids=derived_ids)
coverage = CoverageReporter(store).run(
    task_ids, verification_run_id=verified["run_id"], derived_run_ids=derived_ids,
)
origin = store.evidence.get_origin(evidence_id)
check_origins = store.get_origin(verified["run_id"], verification_id)
```

`on_stage(stage, status)` 可接收阶段状态；`cancel` 回调在阶段/批次/核验目标之间检查。已提交检查点增量保存，普通失败或取消可重试；进程中断保留 `interrupted`，恢复跳过已提交单元。部分 Map 失败仍生成覆盖率，公开状态为 `partial`，不标记完整成功。

文件版本变化后，原报告的新鲜度检查会拒绝沿用旧结果。应通过现有同步流程更新解析缓存，创建新快照再分析；恢复不会擅自把旧快照替换为新文件版本。

### 3. 真实云端的独立授权

本次没有运行真实云端模型。默认工具使用延迟创建客户端的 DeepSeek Map 和语义核验适配器，所有开关默认关闭：

```dotenv
DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM=0
DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK=0
DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM=0
DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM=0
DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM=0
```

正式启用云端集合分析须由管理员单独授权 Workflow、Map 和 Verification 三个开关，再由用户批准该次工具调用；API Key、普通 Agent/RAG 授权或模型文本不能代替这些条件。Entity/Reduce 默认仍使用本地规则和 Python；只有另行注入云端辅助器时才需要其对应授权。适配器还检查可信端的 `authorized=True`，工作流检查 `cloud_authorized=True`，每次发送请求前重新检查阶段开关。工具不会修改环境开关。

## 存储与权限

复查并复用了现有 SQLite 和缓存接口：`document_sync.sqlite3` 的 `parsed_documents` 提供解析目录，Catalog SQLite 保存固定清单，Evidence Store 保存任务/文档/批次/证据。新增 Verification、Coverage、Workflow 记录复用 `evidence_synthesis.sqlite3` 既有的 `synthesis_runs/synthesis_items` 两张表，不新增业务表或修改解析库结构。默认目录仍为 Storage 根下的 `KnowledgeBase/sqlite/`；当前配置的 Storage 根为 `/home/sweet/Storage`。

任务定义、输入版本、结果和检查点均保存 SHA256 指纹。工作流外层与内部阶段使用不同文件锁分区，避免嵌套死锁或错误回收父任务；SQLite 写入只发生在短事务中，模型请求不占用数据库写事务。此处沿用本地单机存储与锁，不提供分布式任务调度。

`KnowledgeBase/documents/Diary/24.7.16.md` 始终强制排除，且在 Scope、全文读取、证据查询、历史结果核验和出处查询中再次检查。权限收紧后不能通过旧证据恢复访问。权限沿用可信端 `CatalogPolicy`、路径安全检查和现有审批，没有新增多租户 ACL 体系。`read_historical` 仅供显式诊断，仍检查当前权限，不能视为当前版本有效报告。

## 本阶段文件清单

新增实现：

- `app/document_analysis/validation_input.py`：当前版本、任务、批次及固定范围输入。
- `app/document_analysis/verification_schema.py`：可注入语义核验协议。
- `app/document_analysis/evidence_verification.py`：位置、版本、引用、语义和 Python 统计核验。
- `app/document_analysis/deepseek_verification.py`：默认关闭的 DeepSeek 原文支持适配器。
- `app/document_analysis/coverage.py`：文档、正文、分类型验证和完整性报告。
- `app/document_analysis/workflow.py`：全流程、恢复、公开状态和已核验结果选择。
- `app/tools/document_analysis.py`：受控工具、确定性报告和混合工具答案处理。

修改既有实现：

- `app/document_analysis/evidence_store.py`：批次查询及历史证据的来源权限/范围复查。
- `app/document_analysis/synthesis_store.py`：新增阶段类型、依赖、历史读取、出处查询及嵌套锁分区。
- `app/document_analysis/synthesis_runtime.py`：语义核验独立授权。
- `app/document_analysis/deepseek_evidence.py`：提取与核验复用原 JSON 客户端配置。
- `app/tools/executor.py`：注册工具、传递与审批绑定的执行许可。
- `app/agents/graph.py`：分析结果发布及混合调用继续保留 RAG 核验。
- `app/agents/prompts.py`、`app/agents/tool_policy.py`：路由说明及审批策略。
- `.env.example`：两个新增默认关闭开关；未修改真实 `.env`。

新增测试：`tests/test_validation_input.py`、`tests/test_evidence_verification.py`、`tests/test_analysis_coverage.py`、`tests/test_analysis_workflow.py`、`tests/test_analysis_agent.py`、`tests/test_deepseek_verification.py`。新增文档为本文件及 `docs/superpowers/plans/2026-10-22-verification-coverage-workflow.md`。

## 验证记录与限制

新增 57 项离线测试已通过：使用临时目录和真实 SQLite/Graph/HTTP/SSE，只在提取器、语义模型和向量网络边界使用模拟器。覆盖错误 ID/位置/引用、原件与缓存变化、部分失败、批次重叠、幂等恢复、取消与进程中断、权限及 Diary 排除、跨任务统计、语义限制、审批拒绝与参数篡改、混合工具结果、SSE 重放和持久化。

2026-10-22 联合兼容性回归：原有 246 项 + 新增 57 项，合计 **303 项 unittest 全部通过，38.648 秒**；另有 **17/17 组既有离线回归通过**，覆盖 RAG、引用/API/SSE、上下文预算、自动同步、Map/Store/Entity/Reduce、文件管理、会话、请求日志和使用模拟向量客户端的入库/同步。已检查差异空白错误；保留原文件未修改行中已有的空白。

新增测试可使用下列命令重跑（`python` 使用项目的 `agent_kb` 环境）：

```bash
DEEPSEEK_API_KEY=offline-test RAG_AUTO_SYNC_ENABLED=0 python -m unittest \
  tests.test_validation_input tests.test_evidence_verification \
  tests.test_analysis_coverage tests.test_analysis_workflow \
  tests.test_analysis_agent tests.test_deepseek_verification
```

本轮联合回归通过额外网络守卫阻断外网连接/DNS，日志位于 `/tmp/sweet-analysis-regression/unit.log`、`legacy.log` 和各组 `.log`。未运行会直接调用云端的手工演示脚本。没有调用真实 DeepSeek/Embedding，没有修改正式向量库；正式 Storage 中的 Catalog/Evidence/Synthesis 分析库也未因测试创建。这些结果不能冒充真实模型语义准确率或真实模型路由效果评估。

当前限制：Scope 仍采用既有受限自然语言规则；问题用于选择模板结果的呈现视图，不是任意语义查询引擎。解析格式、文件大小、批次与实体配对上限继承原模块。DeepSeek 核验单个目标的 JSON 输入超过 200,000 字符时明确失败，不静默截断；超大关联目标需注入能分批验证的后端。核验失败或模型响应不完整不发布成功结论。聊天展示上限不影响存储和来源查询。尚未进行真实模型、生产数据全量分析或大型资料的性能验证；没有新增独立分析 REST 路由、任务队列、取消按钮或 UI 页面。
