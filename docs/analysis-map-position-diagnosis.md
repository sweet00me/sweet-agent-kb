# 集合分析 Map 引用定位失败诊断

检查日期：2026-10-22。先只读检查实际 SQLite、任务快照、审计和节点追踪，再执行模拟模型回归。本次没有重试真实云端分析。

## 实际任务与逐文档根因

- Workflow：`be7c7b50791082f9d69be06191b2eba8feef2e21077758685b5a4ad01cb8b8eb`
- Map Task：`dd70b61fd0d1dfa4a6eb4da6c4193907d45f0d8a46697a71b0680c45d034ed7f`
- 模型标识：`deepseek:deepseek-flash`，模板 `general_facts`。
- 批次配置：正文 8192 字符，重叠 1024 字符；两份文档各只有一个批次。

以下路径均相对于 Storage，未记录任何文档正文或模型引文。

| 文档 | 正文字数 | 实际错误码 | 阶段 | 处理结果 |
| --- | ---: | --- | --- | --- |
| `KnowledgeBase/documents/notes/rag_intro.md` | 338 | `quote_position_mismatch` | Map → `save_batch` → `bind_evidence` | 第一个批次的模型坐标与引文不一致，批次未提交 |
| `KnowledgeBase/documents/notes/segmentation_intro.md` | 315 | `quote_position_mismatch` | Map → `save_batch` → `bind_evidence` | 第一个批次的模型坐标与引文不一致，批次未提交 |

两份文档均为 `failed`、`attempts=1`、`processed_chars=0`、`last_batch=-1`。此 Map Task 的批次记录和证据记录均为 0；预期正文 653 字符，已提交 0 字符。

`evidence_schema.bind_evidence` 要求模型提供的 `start_char/end_char` 是窗口内 Unicode 字符坐标，且 `window.text[start:end] == quote`。旧 DeepSeek 适配器把模型返回的坐标原样传入此检查，导致任一候选计数错误就让整批保存失败。

历史存储不保留原始 Map 响应或失败候选，后端标准输出连接终端，没有发现可回放的响应日志。因此不能确定具体偏移量，也不能证明历史引文本身一定能在正文中唯一找到；不能直接宣称真实任务已恢复。

## 请求、Schema、权限及版本排查

- 请求使用 JSON Object 模式，Schema 随提示词发送，响应经严格 Pydantic `ExtractionResponse` 校验。进入 `quote_position_mismatch` 分支说明调用已经返回、完成标记及外层候选结构已经通过检查，不是本次请求的网络、凭据、JSON 解析或截断错误。
- 模板 `details` 的校验位于引用位置校验之后，不能将本次失败解释为全部模板字段已经验证通过。
- 审计记录：13:31:02（北京时间）审批 `approve`，13:31:09 执行 `tool_error`。Map 节点耗时约 7242ms，受控工具约 7683ms。
- 当前后端进程继承的 Workflow、Map、Verification 三个独立开关均为 `1`；任务也实际通过了授权检查。本次检查没有调整任何开关。
- 两份文档的当前原文件 SHA256、解析缓存哈希、解析正文哈希、解析版本与固定快照一致；任务定义、Workflow 定义及结果的存储摘要核对通过。没有发现版本变化或权限拒绝。
- `KnowledgeBase/documents/Diary/24.7.16.md` 仍由 Catalog 强制排除，没有读取其正文或历史证据。
- `llm_usage` 仅记录到 Agent 的选工具调用，没有保存本次 Map 的原始响应，不能用该表复原错误坐标。

## 下游错误含义

- Map：任务 `failed`，两份文档均无可发布证据。
- Entity Resolution：计算完成，但输入证据为空。
- Reduce：失败，`no_available_evidence`。
- Verification：执行完成，核验目标为 0，不代表文档或结论得到验证。
- Coverage：报告完成，`analysis_complete=false`，附加 `pipeline_stages_missing`、`upstream_incomplete`。
- Workflow 的存储状态 `completed` 表示报告计算结束；公开分析状态为 `partial`。这两种状态不能混同为分析成功。请求日志的 `completed` 同样仅表示审批请求处理结束。

## 最小修复及文件

1. 修改 `app/document_analysis/deepseek_evidence.py`：提示词优先要求省略字符坐标，由已有后端绑定逻辑定位逐字且唯一的引文。对于仍然返回错误坐标的候选，仅当完整引文在当前窗口中精确且唯一出现时，在适配层用 Python 计算正确坐标；不新增云端重试。
2. 补充 `tests/test_deepseek_evidence.py`：7 项离线回归，覆盖错误坐标、找不到/歧义引文、有效重复引文坐标、严格响应 Schema、Unicode 与跨批次定位、文档版本变化、两文档失败任务恢复与重复执行。
3. 新增本诊断文件。

未放宽通用 `bind_evidence` 的严格校验。引文不存在、有歧义且没有有效坐标、Schema 无效、文档或权限变化时仍拒绝保存；保持原 Schema 和提取器标识以兼容已有任务恢复，不迁移数据库。

## 实际测试结果

- 修改生产代码前，新增回归复现了 `quote_position_mismatch`；两文档工作流也复现了 0 证据、0% 覆盖率及相同下游错误。
- 修复后，`tests.test_deepseek_evidence`：13/13 通过。
- 相关 9 个测试模块：102/102 通过，用时约 23 秒；覆盖 DeepSeek Map/Verification、严格绑定、Evidence Store/Map、Verification、Coverage、Workflow 和 Agent 审批/工具调用。测试以临时 Storage 和模拟模型运行，并禁止外部网络及 DNS 访问。
- 模拟恢复沿用原任务 ID：两份文档均处理完成、正文覆盖率 100%，来源位置及 SHA256 可验证；后续各阶段均执行完成。模拟文档有跨文档身份歧义，因此公开结果仍如实保留 `partial/unresolved_identity`。再次恢复没有增加模型调用或重复证据。
- 最后只读复查：真实两份文档仍各尝试 1 次、失败、0 字符；真实任务和报告未被修改。

## 恢复方式与限制

当前后端未启用热重载，需要重启后端才能加载修复。本次没有执行重启。

两份文档的恢复方式相同：在用户明确同意再次调用云端后，通过现有受控工具 `analyze_document_collection` 使用原问题、原范围、原模板和上述 `analysis_id`，重新走审批及独立云端授权。可信后端的恢复入口是 `AnalysisWorkflow.resume(analysis_id)`，它会读取原任务参数；不能用它绕过外层审批。

未完成的文档会重新请求 Map，后续 Verification 也可能产生费用；已完成文档和已提交批次沿用现有幂等机制。若版本变化，应通过现有同步与新快照流程建立新分析，不修改旧任务来绕过版本校验。

本次只完成实现修复与离线验证。原始失败候选没有留存，真实模型再次生成缺失或歧义引文时仍会失败；没有声称真实 DeepSeek 分析已验证成功。未删除、重建或修改现有任务、SQLite、解析缓存及 ChromaDB。
