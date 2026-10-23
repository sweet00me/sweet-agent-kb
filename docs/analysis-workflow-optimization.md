# 文档集合分析工作流优化

日期：2026-10-22。本轮只优化现有模块，没有增加分析阶段、数据库表、Agent 或框架，没有重跑真实 DeepSeek 分析。

## 真实运行的只读定位

- Workflow：`be7c7b50791082f9d69be06191b2eba8feef2e21077758685b5a4ad01cb8b8eb`
- Map Task：`dd70b61fd0d1dfa4a6eb4da6c4193907d45f0d8a46697a71b0680c45d034ed7f`
- Verification：`b8e7b29ec3ef68db8641551b40551afdd2da8102acb3445c277be0234447c022`

实际任务已处理 2 份文档，产生 16 条证据。`general_facts` 为每条证据生成 subject/object 两个提及，共 32 个提及；缺少充分身份依据时保留 32 个分组，不等于确认了 32 个现实对象，也不能仅凭 25 个不同名称合并对象。

旧 Entity Resolution 对同类提及全部配对：`32 × 31 / 2 = 496`。其中 487 条为 `unknown`，9 条为 `possible_same`；Entity LLM 未启用。耗时来自后续 Verification 把这些关系、提及和分组再次送往语义验证器。

| 目标类型 | 原真实运行 | 优化后对同一证据的只读规划 | 优化后的验证方式 |
| --- | ---: | ---: | --- |
| Map 证据结论 | 16 | 16 | 语义核验 |
| 实体提及 | 32 | 32 | Python 结构与来源检查 |
| 实体关系 | 496 | 9 | Python 身份规则与冲突检查 |
| 实体分组 | 32 | 32 | Python 重放合并与冲突保护 |
| Reduce 结论 | 16 | 16 | 语义核验 |
| 比较 | 16 | 16 | 本次均为单一取值，仅做 Python 聚合检查 |
| 精确统计 | 8 | 8 | Python 重算 |
| **合计** | **616** | **129** | **89 项结构检查、8 项计算检查、32 项语义目标** |

旧验证结果中，608 项 `support_basis=semantic_validator`，8 项 `python_recalculation`；因此可以确认有 **608 次完成并保存结果的 DeepSeek 语义核验**，不是 616 次云端核验。核验用时约 17 分 56 秒。

两份文档还各有一个成功 Map 批次，因此成功路径至少包含 610 次分析阶段调用。旧任务没有逐次 Map/Verification 请求账本；文档 `attempts=3` 不是 API 调用次数，`llm_usage` 仅记录了 3 次 Agent 调用，不能据此准确还原历史失败、中断或未落库调用的总数和账单。

## 最小实现调整

### 候选关联

按原文可定位名称、同一出处、显式标识符命名空间及同证据中的明确别名/不同声明建立候选桶，不再为无依据的全部组合创建关系记录。没有在引文中定位的名称仍保留为 unresolved 提及和分组，不用于推测关联。

省略的组合标为 `not_assessed_not_different`，不把它们当作“明确不同”。同名关系仍只是候选；模型建议不能自动合并。保留标识符矛盾、代词、跨文档同名及传递合并冲突的保护。

### 核验分流

- 所有目标仍检查 Evidence ID、来源、位置、SHA256、版本、权限及引用集合。
- 实体提及、关系和分组采用 Python 校验；重放分组构建，防止格式有效但没有依据的合并。
- 数值统计与比较聚合采用 Python 重算，并缓存同一生产者的重算结果。篡改的数值不会进入云端后被误标支持。
- 单一取值比较不是实际比较结论，不额外调用模型。多种取值的比较、Map 结论、Reduce 结论、时间线和语义归纳仍需要语义核验。
- 结构目标保留 `semantic_status=not_checked`、`semantic_required=false`；不会伪造为 `supported`。
- `semantic_completed` 只统计真实返回并通过引用关系检查的语义判定。结构失败导致的拒绝、不调用模型的离线检查，都不算语义核验完成。
- Coverage 分开统计结构、计算、语义目标；保留 `document_processing_complete`、`verification_complete`、`analysis_complete` 与 `facts_exhaustive=false`。

### 共享预算与进度

复用 `synthesis_items` 保存核验计划、逐目标检查点、云端请求预留和结果元数据，不修改 SQLite Schema。预算不保存正文、Prompt、模型回答或 API Key，仅保存摘要、大小、额度和完成/失败状态。

| 设置 | 默认值 |
| --- | ---: |
| `DOCUMENT_ANALYSIS_MAX_CLOUD_CALLS` | 64 次 |
| `DOCUMENT_ANALYSIS_MAX_RESERVED_TOKENS` | 2000000 |
| `DOCUMENT_ANALYSIS_MAX_REQUEST_BYTES` | 200000 字节 |
| `DOCUMENT_ANALYSIS_REQUEST_TIMEOUT_SECONDS` | 45 秒 |
| `DOCUMENT_ANALYSIS_MAX_VERIFICATION_TARGETS` | 10000 项 |

同一工作流内 Map、Entity、Reduce、Verification 的云端适配器调用共用额度；发请求前落盘，失败或中断不退还，恢复不重置，已保存目标不重复调用。正常 RAG 和 Agent 规划保留原有逻辑。

费用保护采用调用次数和保守 Token 预留上限，不是供应商美元账单。预留按序列化输入 UTF-8 字节数、固定提示词余量和最大输出计算；超限先停止，不截断资料。原有最大输出 8192 Token 保留。HTTP 客户端 `max_retries=0`；超时返回明确错误并保留进度。预算参数固定后，恢复时改变预算配置会明确拒绝，不会悄悄获得新额度。

进度包含 `total_targets`、`completed_targets`、`processed_targets`、`semantic_targets`、`semantic_completed` 和请求账本统计；支持回调及运行中只读查询。未调用语义模型时，本次规模为处理 129 个检查项、完成 97 个结构/计算目标、语义完成 0/32，不能显示为 129 项全部核验完成。

旧记录仍可查询：`confirmed_completed_semantic_calls` 可以给出上述 608 次已保存的语义核验；`ledger_present=false` 表示旧记录缺少新账本，不能把 `reserved_calls=0` 当作历史调用为零。新账本不能追溯计算升级前的全部花费。

### 回答展示

先显示当前问题所选的已核验结论、统计、比较或时间线，再显示文档覆盖、核验进度及主要不确定性。没有实际比较结果时，可展示相关已核验事实，并明确说明暂无可发布的比较结论。

回答按子问题及文档组织，先匹配完整已存结论，再分配各项的展示预算；不再全局机械截取前 8 项。总结使用自然语言段落，缺少可发布结论的文档及关系问题仍单独回答。来源使用文档编号 `[1]`、`[2]`，每份文档列一次。Evidence ID、SHA256、解析正文位置仍在结构化 `sources`、Evidence Store 和 Synthesis Store 中保留；不在正文重复堆叠。统计明确限定为提供的证据集合，不把实体分组数当作确认的现实对象数。

## 只读调用方式

```python
workflow.status(analysis_id)                     # 工作流与当前核验进度，不执行模型
verifier.progress(verification_run_id)            # 含运行中/失败任务的持久化进度
store.evidence.get_origin(evidence_id)            # 原始文档、版本、位置
store.get_origin(reduce_run_id, claim_id)         # 结论的证据出处

# 对历史已完成结果重新排版：只读；不调用 run/resume，不再请求 DeepSeek。
from app.tools.document_analysis import render_report
response = store.get_run(analysis_id)
verification = store.get_run(response["result"]["verification_run_id"])
answer = render_report(response, verification_checks=verification["result"]["checks"])
```

执行和恢复仍走原受控工具、审批、独立云端授权与 Execution Guard；本轮没有增加可绕过审批的执行接口。

## 实际验证

- 核验优化前的真实 616 项及 608 次完成语义调用，均通过只读 SQLite/Store 查询定位。
- 对原 16 条证据仅执行 Python 候选与目标规划：**9 条关联、129 个目标，其中 32 个语义目标**；没有调用提取器或语义模型，也没有保存新的真实分析任务。
- 临时 Storage 中使用同样规模的模拟数据，验证 **616 → 129 个目标、608 → 32 次语义调用**；完整模拟云端工作流为 **2 次 Map + 32 次 Verification = 34 次**，再次恢复新增调用 **0 次**。
- 最终离线回归：**331/331 通过，约 53.7 秒**，禁止外部网络及 DNS；覆盖 Catalog/Scope/Reader、Map/Store、候选关联、传递冲突、核验、覆盖率、预算耗尽、失败恢复、超时、来源查询、审批/Guard/Checkpoint/Tracing、真实本地 HTTP/SSE、RAG、文件预览与记录管理。
- 语法与空白检查通过。没有运行真实云端模型、Embedding 或 ChromaDB 写入。
- 最后复查原任务：两份文档仍完成、各尝试 3 次、证据仍为 16 条；原 Workflow 更新时间仍为 `2026-10-22T06:07:07.225167+00:00`，原 616 项 Verification 更新时间仍为 `2026-10-22T06:07:06.856010+00:00`。

## 文件清单

本轮修改既有实现：

- `app/document_analysis/entity_resolution.py`：候选筛选及规则版本。
- `app/document_analysis/evidence_verification.py`：结构/计算/语义分流、分组重放、进度、预算接入。
- `app/document_analysis/coverage.py`：适用语义目标与文档覆盖分别计数。
- `app/document_analysis/synthesis_runtime.py`：共享持久化额度与云端适配器包装。
- `app/document_analysis/synthesis_store.py`：授权后的只读检查点查询。
- `app/document_analysis/workflow.py`：预算共享、进度查询、已核验事实回退展示。
- `app/document_analysis/evidence_map.py`：将逐文档预算/超时错误传到工作流。
- `app/document_analysis/deepseek_evidence.py`、`deepseek_verification.py`：HTTP 超时、输出上限配置与明确超时码。
- `app/tools/document_analysis.py`：结论优先、文档编号引用、预算及不确定性说明。
- `.env.example`：预算配置示例；未修改实际 `.env` 或开启云端开关。

新增测试：`tests/test_analysis_efficiency.py`。更新测试：`test_entity_resolution.py`、`test_evidence_verification.py`、`test_analysis_workflow.py`、`test_analysis_agent.py`、`test_deepseek_verification.py`。新增本文档。

## 回答质量修复补充（2026-10-22）

- 根因：Workflow 先截断 20 项，工具再截断前 8 项，未拆分“逐篇概括＋跨文档关系”问题。已存的 16 条结论实际为每篇 8 条，没有已核验的跨文档关系结论。
- 只修改现有答案组织：`app/document_analysis/workflow.py`、`app/tools/document_analysis.py`；补充 `tests/test_analysis_efficiency.py`、`tests/test_analysis_workflow.py`，更新本文档。没有增加模块、分析阶段或云端请求。
- 发布必须匹配结论 ID、生成运行、完整 Evidence ID 集合及结论正文，要求位置及引用有效、语义状态 `supported`、依据 `semantic_validator` 且模式 `semantic`。Python 重新计算的统计单独展示；`not_checked`、部分支持、矛盾、不支持及结构检查均不能充当已支持事实。历史结果没有核验凭据时保守拒绝发布事实。
- 多子问题逐项回答，按文档与主题选取完整已存结论。共同出现、同名对象、共享词汇、结构检查或缺少关联记录都不证明存在／不存在明确联系。无直接支持时明确回答“没有找到直接支持证据；这不等于确认不存在关联”。保留来源编号、具体不确定性及覆盖率说明，过滤 Reduce 阶段固定且已过期的“事实语义未核验”展示文案。
- 真实任务 `023543ffdab4590e9fe2c76723c97d57d4d85d59581f3b87de36300d06ea6322` 已只读回放：2/2 文档、129/129 目标、32/32 语义核验；RAG 总结、语义分割总结、关系判断共 **3/3** 子问题有对应回答。两个总结各选取 4 条已支持结论；关系项明确证据不足，不推断不存在联系。
- 回放使用 SQLite `mode=ro`、`query_only=ON`、写操作拒绝及禁用网络；新增模型请求 **0**，数据库写操作 **0**。Evidence Store、Synthesis Store、Catalog、解析索引四个数据库的内容 SHA256、大小及修改时间在回放前后相同。未打印私人文档正文或密钥。
- 本次新增 **12** 项测试；回答与 Workflow 针对性回归 **36/36** 通过，最终完整离线回归 **343/343** 通过（约 70.0 秒），含审批、Execution Guard、Checkpoint、Tracing、本地 HTTP/SSE、预算恢复、RAG 及文件管理兼容测试。4 个修改的 Python 文件通过语法与空白检查；真实模型没有重新验证或调用。
- 本次仅重新组织既有已核验表述，不产生新的语义归纳或关系判断。文档及主题匹配是离线规则；复杂或缺少匹配结论的问法可能明确返回证据不足。既有聊天消息、ToolMessage 和 Checkpoint 未改写；历史任务可用上述只读方式重排，无需重新分析。

## 限制与加载

优化后的云端语义判定、真实账单及实际提速没有在线复测；32 次是同证据规划值和模拟验证值，真实调用失败时可能无法完成全部目标。不同算法版本保存为新的派生运行；不把旧结构检查继承为语义支持。已完成 Map 继续复用。

候选筛选保守，可能遗漏没有名称、标识符或明确别名依据的隐含关联，不自动合并或宣布这些组合明确不同。100% 文档覆盖不证明所有事实、身份关系或现实真相都已确认。

中断发生在模型返回之后、目标检查点保存之前时，该目标可能需要再次请求；原请求的预留仍占用额度，不承诺供应商端恰好一次执行。历史失败调用次数没有可补全的流水，不能伪造精确统计。

需要重新加载后端进程以使用修改。本轮没有重启生产服务、重试真实分析、删除或重建现有任务、SQLite、缓存及 ChromaDB。
