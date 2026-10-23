# Entity Resolution 与 Evidence Reduce

## 范围与成功条件

基于现有 Catalog、Scope、Reader、Evidence Map/Store 增加两个独立 Python 服务。默认纯离线；保留 Diary 强制排除及当前权限校验。不得改动 RAG、LangGraph、UI，不实现 Evidence Verification、Coverage Report 或子图。

实现规则优先的四态实体关联、可恢复保存、跨文档证据整理及有 Evidence ID 的统计/比较/时间线。原文位置校验成功不等于事实语义为真，所有输出的 semantic_verified 固定为 false。

## 输入和版本

从 EvidenceStore 的公开接口读取一个或多个任务及已完成文档的 verified 证据。复用 Catalog 复查完成文档（包括零证据文档）和 get_origin 的版本、位置校验。输入指纹包含任务定义/状态、文档状态和证据载荷；收尾及查询时再次核对。失败/未完成上游任务仍可整理已验证部分，但必须保留 upstream_incomplete，任何统计都不宣称穷尽。

## Entity Resolution

从既有三类模板派生人物、实验、对象提及。结构化名称须在原引用中出现；代词及未出现的名称保持 unresolved。同名只支持 possible_same。仅相同原文提及、名称后紧邻的 ORCID（人物）或明确全局 UUID，以及原文明确的别名声明，可支持 same；同一标识域的不同标识或明确否认可支持 different。其余 unknown。规则结果携带具体判断依据、Evidence ID 和置信状态。

可注入辅助接口只在规则不充分时使用；输出必须引用本次请求中的 Evidence ID。模型的 same 一律降为 possible_same，不单凭模型置信分数合并。规则产生的 same 连通分量若包含 different 约束，整个分量不自动合并，保留冲突。

## Reduce

先按原文来源、SHA、位置与结构化字段消除同一出处在多任务中的重复；再按已关联实体和精确字段整理相同陈述，保留全部 Evidence ID。陈述组数量不等于现实中的事件/人数。

Python 负责数量、Decimal 数值、参数比较和原文 ISO 日期排序。不同参数值或事实对象仅标记潜在冲突，不自动裁决真伪；不同单位不换算。未明确绑定的实体不做跨文档身份合并。时间未知/有歧义时保留未定位项，不用文件修改时间替代事件日期。

语义归纳是可注入接口，按有界批次执行；模型不能修改 Python 统计，不能注入未知 Evidence ID 或完整性/验证标签。所有派生陈述保留不确定性与原始出处。

## 存储、恢复和授权

新增独立 evidence_synthesis.sqlite3，包含 synthesis_runs 与 synthesis_items。复用安全 SQLite 连接；将现有 Map 的文件锁提取为共享 helper，锁语义保持不变。任务身份包含输入指纹、算法/配置/辅助器版本及依赖结果摘要。检查点增量提交、摘要校验；同输入重复运行返回相同结果，失败/取消可恢复，异常退出由锁释放和租约恢复处理。不发布未完成结果。

Entity 与 Reduce 云端辅助分别要求 DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM=1 / DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM=1，并显式 authorized=True。默认没有辅助器，不初始化云端客户端。此阶段仅提供注入协议，不增加真实云端适配器。

## 验证与限制

使用临时 Storage、真实本地解析/Map/SQLite、模拟辅助器测试：四态、同名、代词、标识/别名、传递冲突、去重、参数比较、日期、未知来源、部分上游、恢复、中断、权限、版本变化和独立授权。现有离线回归必须通过。

规则只覆盖明确的标记形式；不保证召回所有别名或隐含实体。来源校验无法证明模型结论语义正确；不实现语义核验。实体对有显式数量上限，超过即失败，不静默截断。统计只针对输入证据集合。
