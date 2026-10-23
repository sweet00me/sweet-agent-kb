# 私人查询与消息输入修复（2026-10-22）

## 真实请求与根因

只读检查正式 Session、Request Journal、LangGraph Checkpoint、工具审计、节点 Trace 与模型调用记录；没有重跑真实请求，没有输出私人文档正文或 API Key。

会话 `research-474f86bc5d5447e7` 最新四轮聊天：

| UTC 时间 | request_id | 请求状态 | 实际工具调用 | citation_report |
| --- | --- | --- | --- | --- |
| 09:09:06 | `req_e5c89c852587f34bae575b27fb66bdb1` | completed | 0 | plain / clarify_scope |
| 09:09:18 | `req_1fb2ed49647859ae95bf069572639e50` | completed | 0 | plain / clarify_scope |
| 09:10:11 | `req_4117f32c24b5eae330ec4b236ca08bc8` | completed | 0 | plain / clarify_scope |
| 09:10:51 | `req_abbf632b530992d0c81b3560846aa515` | completed | 0 | plain / clarify_scope |

这四轮只有 `agent` 节点，每轮一次回答调用及一次来源依赖检查；没有进入新的 Approval / Execution Guard 或检索工具。Checkpoint 中已有 Diary 路径，但来源检查仍放行重复目录澄清。历史中的旧集合分析调用不属于这四轮的新工具调用。

两个问题的具体原因：

1. 上一轮通用化将“没有具体范围先询问目录/文件/日期”写得过宽，模型把普通私人知识查询也当成必须先指定集合范围。来源依赖检查只看草稿是不是澄清，没有区分普通 RAG 与完整集合分析，也没有拦截已知范围的重复确认。
2. 后端已增加 `plain`、`rag` 引用报告类型，前端 `agentStream.ts` 的解析器仍只接受旧类型。终止事件抛出“引用核验报告类型无效”，`submitChat()` 在流结束后才执行的清空逻辑因此未运行。React 输入框是受控组件，没有发现历史接口主动回填草稿；旧文本一直留在全局 `draft` 状态。另一个独立缺口是没有 Enter / 中文组合输入处理。

修复前，新增后端回归复现了无工具澄清与已知范围再次澄清；前端接口测试复现报告类型拒绝；真实 Chromium 的普通完成、接收时清空、Enter、IME 四个场景均失败。丢失接收事件的两个历史恢复场景也先失败，再补齐恢复衔接。

## Diary 索引只读检查

| 检查项 | 结果 |
| --- | --- |
| 允许访问的文档 | 45 份 |
| 解析缓存 / Chunk 缓存 / 向量清单 | 均覆盖 45 份 |
| 缓存 Chunk / 向量清单 / Chroma 活动 Chunk | 均为 62 个 |
| 来源 SHA256 不一致 | 0 |
| 缺失文档 / 待质量复核 Chunk | 0 |
| 排除文件的活动向量 | 0 |
| 索引正文中含“梦”的只读计数 | 18 份文档、23 个 Chunk |

最后一项只说明相关文字已入索引，不是梦境事件的完整统计。

将 Chroma 复制到临时目录后，使用一条既有文档向量做 Top-3 查询：1024 维，原 Chunk 正常返回，最近距离 0，未返回排除文件。外部网络被禁止，没有调用 Embedding。该检查证明索引与向量查询链可用；**没有重新生成两个自然语言问题的查询向量，所以未验证真实云端语义召回质量**。当前真实请求首先卡在工具调用之前，不能归因为 Top-K 无命中。

## 最小修复

### Agent

- 普通私人知识问题先尝试现有 RAG，不要求用户先提供路径；宽泛内容回忆的 Top-K 结果不等同于穷尽全文搜索。
- 现有来源依赖检查增加任务类型和范围状态。普通私人查询被识别为 `private_lookup` 时，即使草稿要求目录，也生成一次现有 `search_knowledge_tool` 调用，仍通过 Approval 与 Execution Guard；不直接执行工具，不升级为全文分析。
- 完整集合分析先复用本轮及同会话明确、对应本问题的范围。真正缺少必要范围才澄清；重复询问已知范围会触发现有一次重判。
- 来源检查接收会话中已提及的路径线索，不接收历史 AI 的事实断言作证据；线索只用于用户已指向或确认的范围，不授予额外权限。
- 任务分类不包含 Diary、梦境、LoRA 或指标名称分支。保留敏感文件排除、SHA256/原文引用检查、集合分析审批、独立云端授权与预算。

### 前端

- 同步 `plain`、`rag` 类型，继续严格检查状态、Chunk ID、SHA256、引文；普通模式不能冒充来源核验通过。
- `started` 必须匹配本次 `thread_id`、`request_id` 和 `running` 状态。确认接收后立即清空对应草稿，不等待生成结束；旧后端仅返回有效终止事件时仍可完成确认。
- 区分提交中、已接收、明确未接受、执行结果未确认。接收前失败保留草稿；接收后的执行错误或断线不回填已发送文本。
- 草稿按会话保存在页面内存中。会话切换保留尚未提交的下一条草稿，已确认发送的内容从草稿中移除；历史刷新不写回旧文本。
- 接收事件丢失时，保存当前页面内的原请求 ID。历史恢复只读核对这条 Request Journal；已保存最终结果后清空对应草稿，仍运行或不匹配时继续阻止重复发送。不会自动 POST 重试。
- Enter 发送、Shift+Enter 换行；组合输入期间及 `isComposing` / 229 按键不提交；沿用同步 busy Ref 阻止连续点击、重复表单提交。
- HTTP 错误复用现有 `ApiError`，没有增加错误处理框架。

## 新增与修改文件

| 文件 | 用途 |
| --- | --- |
| `app/agents/prompts.py` | 区分普通 RAG 与完整集合分析，范围复用及来源分类协议 |
| `app/agents/graph.py` | 按任务类型进行一次受控 RAG 兜底，调整现有重判提示 |
| `app/rag/citations.py` | 任务/范围状态检查及路径线索，拦截不必要澄清 |
| `tests/test_agent_routing.py` | 无路径查询、多主题实际引用、范围继承与分类失败保护 |
| `tests/test_analysis_agent.py` | 模拟分类器支持新任务协议 |
| `frontend/src/App.tsx` | 接收确认、会话草稿、原请求只读恢复与发送状态 |
| `frontend/src/components/chat/ChatInput.tsx` | Enter、换行、IME 与重复按键处理 |
| `frontend/src/pages/ChatPage.tsx` | 发送中 / 已接收状态展示 |
| `frontend/src/services/agentStream.ts` | 报告模式及请求接收事件校验 |
| `frontend/src/services/sse.ts` | 复用 HTTP 错误类型 |
| `frontend/src/types/api.ts` | 报告模式类型同步 |
| `frontend/tests/chat-input-ui.mjs`（新增） | 真实 Chromium / React / 增量 HTTP SSE 回归 |
| `frontend/tests/sse.test.mjs` | 模式兼容及错误模式、错误接收事件拒绝 |
| `frontend/tests/load-services.mjs` | 测试装载现有 ApiError 依赖 |
| `frontend/tests/review-ui.mjs` | 审批夹具与真实 started 协议一致 |
| `frontend/package.json` | `test:chat-ui` 测试入口 |
| `docs/chat-retrieval-input-fix.md`（新增） | 本报告 |
| `docs/agent-task-routing-fix.md` | 标注本次对旧澄清规则的修正 |

## 实际回归结果

- 后端离线全量回归：**374/374，通过，80.565 秒**，外部连接与 DNS 被禁止。
- 原 Agent 行为评测：**E01–E08 通过**，覆盖审批、执行拦截、ToolMessage 对应、Audit、Tracing、Metrics。
- 前端接口测试：**38/38 通过**；TypeScript / Vite 构建及 ESLint 通过。
- 真实 Chromium `156.0.8078.4`：**18/18 聊天场景通过**。使用真实 React、App、SSE 服务和本地增量 HTTP 服务器，Vite 的 API 代理明确指向隔离服务器。
- 原审批页面真实 Chromium 回归：**5/5 通过**，包括授权关闭、失败、历史接口失败、断线已保存、断线仍运行。
- 正式数据库四条已保存的终止报告只读回放：**4/4 通过前端真实解析器**，未重放原问题，回答正文未输出。
- 五个涉及的 Python 文件通过语法检查，`git diff --check` 通过。

18 个聊天场景：普通完成、RAG 完成、接收后生成前清空、Enter/换行、中文组合输入、同步重复提交、连续发送、明确 HTTP 拒绝、创建会话失败、新会话成功、接收前断线、接收后断线、执行异常、历史读取失败、接收事件丢失但结果已保存、接收事件丢失且仍运行、会话草稿切换、移动端。

后端新增测试使用临时文档、解析缓存和 SQLite，经过实际 Agent → Approval → Execution Guard → 工具 → 来源核验 → SSE 发布链路。不同问题使用对应的模拟文档和真实本地引用；没有用新增主题关键词替代任务判断。

当前环境的 Chromium 回归命令（在 `frontend/` 执行，复用已有测试依赖和浏览器缓存）：

```bash
LD_LIBRARY_PATH=/tmp/sweet-agent-ui-qa/libs/usr/lib/x86_64-linux-gnu \
SWEET_PLAYWRIGHT_CORE_PATH=/tmp/sweet-agent-ui-qa/node_modules/playwright-core/index.mjs \
npm run test:chat-ui
```

常规前端检查为 `npm test`、`npm run build`、`npm run lint`。Chromium 脚本自行启动隔离 Vite 与模拟 API，默认不会连接正式后端。

## 保留事项与验收边界

- 没有新增 Agent 节点、分析阶段或数据库表；Map、Entity、Reduce、Evidence Verification 算法不变。
- 四个算法文件以及正式 Evidence、Synthesis、Catalog、解析、Chunk、Vector、Chroma SQLite，共 **11 个文件的大小、修改时间及 SHA256 均与修复前一致**。
- 没有调用真实 DeepSeek / Embedding，没有重跑真实集合分析，没有删除、重建或同步正式知识库。没有改动云端开关或预算。
- 模拟回归证明工具选择与发布协议可用；真实模型的任务分类、具体问题的语义召回和答案相关性仍需另行真实验收。
- 完整文档遍历仍须人工审批、独立云端授权与预算。事件日期和文件修改日期仍沿用现有区分。
- 本次未重启生产服务。后端如未启用自动重载，需要按原部署方式重载；前端页面刷新后使用修复代码。既有日志、任务和聊天记录保持原样。
