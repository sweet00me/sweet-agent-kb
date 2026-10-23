# 日记问答 Agent 集成修复（2026-10-22）

> 历史报告：本报告的 Diary 主题前置路由、梦境专用展示现已由[通用任务工具选择修复](agent-task-routing-fix.md)替代；当前行为及最新回归结果以新报告为准。

## 只读定位到的根因

检查正式 Session/Request Journal、LangGraph Checkpoint、Audit 和 Node Trace；没有发起检索、Embedding 或 DeepSeek 请求，也没有打印私人日记正文。

最新三个请求仅经过 `agent`，没有进入工具审批和执行：

| 用户问题类型 | Request Journal（UTC） | 实际工具调用 | citation_report |
| --- | --- | --- | --- |
| 历次梦境概括 | 07:40:05，`req_cf9757c11a5844c2c96a9b9ae0ce5f60` | 无 | `rejected / no_current_evidence` |
| 梦境人物数量 | 07:40:17，`req_18813c06638ad0620e8c9e567d011d63` | 无 | `not_applicable / no_knowledge_lookup`，回答仍重复拒答 |
| 能否检索日记 | 07:40:26，`req_62907cc5d28419f319cbd2d9dcb36835` | 无 | `not_applicable / no_knowledge_lookup`，能力说明 |

第一类在没有本轮检索时使用来源标记，来源守卫正确拒绝旧轮次引用；第二类则是模型直接重复拒答，没有再次进入来源核验。这不是每轮都由 `citations.py` 新生成同一条拒答。

更早的两轮梦境/人数问答确实调用了普通 RAG，各有两次 `search_knowledge_tool`、每次成功返回 3 个 Diary 片段。合并后分别只有 4、5 个唯一 Chunk；同一 Chunk 的重复记录**仅 `distance` 字段不同**。原 `verify_answer` 比较完整字典，错误地将查询相关的距离差异判为 `invalid_evidence`。来源、正文及文件 SHA256 本身没有这项冲突。

原 Agent 缺少明确日记意图的集成兜底，且引用拒绝后直接结束本轮；私人资料提示词也将 Top-K 不足表述成整个知识库缺少信息。集合分析在 notes 上通过，并不表示这些日记问题曾调用集合分析。

## 最小修改

- `app/agents/graph.py`：仅对明确、无范围歧义的日记查询规划现有工具。精确日期或片段优先 RAG；历次梦境、跨日记汇总或人数问题申请 `analyze_document_collection`，模板 `people_events`，范围限定 Diary。日期范围、子目录、具体文件等限定交给模型保留范围或澄清，不替换成全部 Diary。能力说明、一般梦境解释不触发全文分析。
- `app/agents/prompts.py`：分别说明普通回答、RAG 和集合报告的发布要求；禁止 Top-K 冒充完整统计，禁止复制旧拒答、引用旧轮次来源或声称检索失败等于没有资料。
- `app/agents/state.py`：仅增加当前用户轮次的 `citation_replan` 检查点字段。RAG 没有证据、草稿不受支持或缺少引用时最多重新判断一次，继续使用已有 Agent、Approval 和 Execution Guard；重新判断仍检查上下文预算。工具执行错误、来源完整性错误、核验服务异常不会被静默转成集合分析。
- `app/agents/graph.py`：由这次重新判断转入的集合分析报告直接发布，保留 `rag_attempt` 原因，不再被此前的 RAG 拒答覆盖；既有并行混合工具问答仍保留分别核验。
- `app/rag/citations.py`：同一 Chunk 仅忽略查询相关的 `distance` 差异；正文、来源、位置、版本及其他元数据仍须一致，且继续执行原文件与缓存版本检查、原文语义核验、引文和数字检查。明确标记 `plain`、`rag`，集合报告沿用 `document_analysis`。工具失败、结果缺失/格式错误和核验服务不可用分别报告，不伪装成证据不存在。
- `app/tools/document_analysis.py`：仅调整已核验答案的梦境用语匹配和人数不确定性展示，不把输入证据条数、提及或实体分组数改名为不同人物数量。
- 测试修改：`tests/test_analysis_agent.py`、`tests/test_citation_verification.py`。

没有增加模块、分析阶段或 LangGraph 节点，没有修改 Map、Entity、Reduce、Verification 算法、云端开关、预算或知识库。

## 实际验证

- 新增 11 项离线测试：3 项引用回归、8 项模拟 Diary 集成测试。
- 模拟 Diary 使用临时 Storage、真实解析缓存、SQLite、Agent、审批、Execution Guard、Checkpoint、Tracing、HTTP/SSE 和来源核验；仅提取器、语义验证器、聊天模型、向量查询及问题向量替换为模拟依赖。
- 梦境概括：先暂停审批，批准后只处理两篇允许的模拟日记，按已核验来源发布结果；`Diary/24.7.16.md` 未进入提取器。
- 精确日期：普通 RAG 自动执行，无集合审批或全文提取，引用状态 `verified / rag`。
- 能力与一般梦境解释：`not_applicable / plain`，不读取全文。
- 拒绝审批及独立云端开关关闭：全文读取和提取次数均为 0，授权错误明确显示。
- 恢复已有 RAG 轮次：证据不足后最多重新判断一次，可请求集合分析审批；恢复后结果携带原 RAG 原因并正确发布，不重复旧拒答。真实工具错误不触发这次重新判断。
- 人物身份歧义：模拟同名和代词保持不确定；不输出虚假的准确不同人数。
- 既有集成回归 73 项通过；最终禁止外部网络/DNS的离线回归 **354/354 通过，约 72.7 秒**。另运行原 Agent 离线行为评测，E01–E08 审批、守卫、消息对应、审计、Tracing 和 Metrics 全部通过。7 个修改的 Python 文件通过语法与空白检查。
- 复核 4 个算法文件、Evidence/Synthesis/Catalog/解析索引 SQLite 和 Chroma SQLite，9 个文件的 SHA256、大小及修改时间均与修复前一致。没有新增真实云端请求、修改正式知识库或重跑真实日记分析。

## 限制及真实验收

本次只验证模拟日记闭环，**未通过真实 DeepSeek 的 Diary 验收**。真实环境仍需由用户另行批准限定范围并满足独立授权与预算后，验收检索相关性、梦境/现实语境、提取内容、引用语义及身份歧义；本次不自动执行这些请求。

现有已核验 Reduce 输出没有单列“梦境人物提及次数”和“可确认的不同人物总数”指标。本次遵守算法不变的限制，对缺失计数明确说明、对不同人物数量回答无法确定，不能用整个证据集条数或实体分组数替代。提及记录也不等同于原文词频或确认的独立人物。

既有聊天消息和分析任务未改写。后端需要重新加载才能使用集成修复；本次没有重启生产进程或开启云端开关。
