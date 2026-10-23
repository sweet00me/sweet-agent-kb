# Verification, Coverage and Controlled Analysis Workflow

本阶段顺序实施，复用既有模块与 SQLite，不增加多 Agent、新框架或知识图谱。遵循 TDD 和完成前验证；使用现有含未提交功能的工作目录，不回滚或提交他人改动。

## 设计与接口

- `validation_input.py`：通过 EvidenceStore/Catalog 的授权接口读取固定范围、文档状态、所有证据及当前来源/缓存指纹。允许记录源缺失/版本变化作为验证失败；权限撤销直接拒绝，不能返回历史内容。无需重新解析或写向量库。
- `verification_schema.py`、`evidence_verification.py`：`EvidenceVerifier(store, validator=None).run(task_ids, derived_run_ids=...)`。核对 Evidence ID、来源、全部版本字段、原文 Unicode 位置、引用关系及 Python 统计。语义接口显式返回 supported/partially_supported/contradicted/unsupported；默认 not_checked。引用有效、原文支持和现实真实性三个维度分开，现实真实性始终 not_checked。
- `coverage.py`：`CoverageReporter(store).run(task_ids, verification_run_id=..., derived_run_ids=...)`。按固定 Scope 内唯一来源统计 expected/analyzed/failed/missing/pending，单列快照内已知排除数；不得冒充整个磁盘的排除文件盘点。复查连续批次，字符覆盖率不重复累计重叠窗口；未知分母返回 null。文档处理完成、语义验证完成、分析完整分别报告，事实穷尽始终 false。
- 扩展现有 `SynthesisStore` 的阶段类型、依赖列表及显式历史读取；不削弱原 `get_run` 的新鲜度检查。复用同一结果/检查点表保存 Verification/Coverage/Workflow，无新业务数据库。Verification/Coverage 使用当前版本指纹，旧报告不能套用新内容。
- `workflow.py`：`AnalysisWorkflow(...).run(question, scope_query, template_id, snapshot_id=None, analysis_id=None)`。Catalog → Scope → Map 内的 Reader → Entity → Reduce → Verification → Coverage；已提交阶段恢复复用，失败阶段可重试，部分失败仍生成诚实报告。Workflow 使用单独锁分区，避免与内部阶段互锁或错误回收正在运行的工作流。
- `app/tools/document_analysis.py`：`analyze_document_collection` 受控工具，策略 approve。隐藏的 RunnableConfig 和 ToolCall ID 接收执行器生成、绑定参数指纹的审批能力，模型不能自行开启云端或注入目录权限。默认云端 Map 和语义核验适配器只有独立 Workflow 开关与阶段开关、人工批准都具备时才调用。
- Agent 提示词区分普通局部 RAG 和跨文档集合统计。完整分析返回确定性、有 Evidence ID 的报告，避免最终模型改写统计或扩大支持范围；RAG 继续使用原核验链。沿用原审批、执行守卫、Checkpoint、Tracing、HTTP/SSE，不改 UI。

## 安全与限制

- Diary/24.7.16.md 在 Scope、全文读取、历史结果与出处查询中继续强制排除。
- 云端 Workflow 与语义 Verification 各有独立默认关闭开关，并继续服从既有 Map/Entity/Reduce 开关。API Key 或自然语言同意不能替代现有人工审批。
- 默认离线测试，只有模型边界使用模拟器；禁止真实 DeepSeek/Embedding 调用和正式向量库修改。
- 校验器标签代表原文支持判断，不证明现实事实真实，也不保证模型判定无误。Coverage 100% 不等于事实没有遗漏。

## 实施顺序与测试

- [x] 输入和持久化：先失败测试，再实现验证输入、历史读取、类型/依赖与锁分区扩展；回归旧 Store/Map/Reduce。
- [x] Verification：先失败测试，再实现版本/引用/位置/Python 校验和可注入语义标签；验证未知 ID、假位置、失败恢复、权限和版本变化。
- [x] Coverage：先失败测试，再实现固定分母、缺失/失败/排除、连续正文覆盖、语义情况和完整性门禁。
- [x] Workflow：先失败测试，再串联阶段、状态、恢复、独立授权与来源报告；不完整阶段不缓存为成功。
- [x] Agent 集成：先失败测试，再注册 approve 工具与绑定审批能力，保留 RAG/Guard/Checkpoint/Trace/SSE；真实 HTTP/SSE 离线端到端回归。
- [x] 独立只读复核、必要修复及联合离线回归；记录完整文件清单、调用方式、测试结果与已知限制。

## 完成记录

只读复核发现的多任务范围漏检、问题与结构化结果输出缺失、语义限制丢失及混合 RAG 结果丢失均以失败测试复现后修复。另补齐实体内部引用、当前输入双次版本检查、历史 Diary 越界、结论所属任务的引用范围和进程中断恢复检查。

新增 57 项测试；联合 303 项 unittest 全部通过（38.648 秒），另 17/17 组离线回归通过。全部使用模拟模型和临时存储，外网连接/DNS 被阻断，没有真实模型或正式向量库调用。文件清单、调用说明、授权条件与限制见 `docs/analysis-verification-workflow.md`。
