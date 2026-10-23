# 集合分析审批与执行故障排查

## 已确认的根因

本轮以 SQLite `mode=ro` 读取实际 Request Journal 和 LangGraph Checkpoint，没有恢复正式任务或调用真实模型。最近两个集合分析审批具有相同结果：

| 层级 | 实际状态 |
| --- | --- |
| 工具审批 | 已批准，`approval_records` 已清空 |
| ToolMessage 外层 | 原实现错误地标成 `success` |
| ToolMessage JSON | `status=blocked`，`error_code=workflow_cloud_not_authorized` |
| 分析报告 | `analysis_status=blocked`、`analysis_complete=false` |
| 审批 Request Journal | `completed`，保存的响应中 `pending_approvals=[]` |

Workflow 开关默认关闭，授权检查发生在读取正文或启动 Map 之前。因此 `controlled_tools` 约 3ms 是立即被授权门禁拦截；其后的 Agent 约 0.04ms 是直接发布服务器生成的工具报告，没有再次调用模型。

前端 `agentStream.ts` 原先只接受 RAG 引用报告，要求 `reason/claim_count/citations`。集合分析报告未包含这三个字段，导致真实 `completed` SSE 事件在客户端抛出“引用核验报告内容无效”。`submitReview` 将解析失败当成审批结果不明，未刷新真实状态，保留旧卡片并加入 `blockedReviewThreads`。原“检查会话状态”虽能看到完成，却不会解除这个锁，后续合法审批也会被阻止。

另一个同源问题：即使已经收到有效终止事件，后续历史接口短暂失败也会进入同一个 catch 分支，错误撤销审批确认。

## 修复范围

- 工具返回真实 `ToolMessage`：完整成功为 `success`，未授权、失败、部分完成等为 `error`，JSON 继续保留业务状态和错误码。现有 Executor 因而能将这些结果记为 `tool_error`，无需改审批或执行守卫。授权错误有明确中文原因，不伪装成证据不足。
- ToolNode 自身的参数校验可能产生纯文本错误；这些错误也转换为明确的分析失败报告，不再让 Agent 重新调用模型、把它变成审批结果未知。
- 集合分析报告补充公共字段，同时前端兼容旧的已保存集合分析报告；保留 `analysis_status`、错误原因、Evidence 来源及混合 RAG 报告。RAG 的 Chunk、SHA256、引文校验仍然严格，不能借分析格式冒充已核验事实。
- 收到并验证终止响应后立即清除旧审批、确认该次请求，并显示实际分析结果。历史列表或消息刷新失败只提示刷新失败，不再把已确认审批重新锁住；已持久化的终止答案可先显示。
- 审批发送前保存请求 ID 和工具指纹回执。断线后仅 GET 现有 `/api/v1/requests/{request_id}`，不重新 POST 审批。该只读诊断接口补充已持久化的终止响应，并沿用已删除会话的访问限制。
- 原请求已保存 `completed` 或新的 `pending_approval` 响应时，才解除原请求锁定；不能把原工具 ID 再变成可批准卡片。手动检查状态或重新打开会话会读取回执并恢复当前状态。
- 回执支持 `sessionStorage` 跨刷新恢复；浏览器存储不能写入/删除时，当前页仍有内存保护。只保存 ID 和指纹，不保存用户问题、审批决定或凭证。

`completed` SSE / Request Journal 表示**这次审批请求已有明确、已保存的终止响应**，不表示分析成功。是否分析成功由 ToolMessage 和 `citation_report.analysis_status/analysis_complete` 表达。云端未授权是可以明确确认的工具失败，应结束等待审批。

Journal 仍为 `accepted/running/uncertain`、记录缺失或终止响应无效时，继续禁止重复提交；即使 Checkpoint 已空闲也不推断该请求成功。后台保存结果后可使用页面现有“检查会话状态”按钮恢复。真正的未知状态仍需核对记录，不自动重试。

## 文件清单

后端修改：

- `app/tools/document_analysis.py`：工具错误状态、中文原因、兼容引用报告元数据。
- `app/api/recovery.py`：只读提供已保存的终止响应。
- `app/api/main.py`：诊断响应遵守会话软删除访问限制。

前端修改：

- `frontend/src/services/agentStream.ts`：解析分析、旧报告、混合报告，保留严格 RAG 校验。
- `frontend/src/types/api.ts`：分析状态与出处类型。
- `frontend/src/App.tsx`：审批确认、历史刷新失败和只读恢复状态衔接。
- 新增 `frontend/src/services/reviewRecovery.ts`：回执、只读核对及结果提示。

测试修改/新增：

- `tests/test_analysis_agent.py`：未授权、执行失败、参数校验失败、真实 HTTP 断线与 Journal/Checkpoint 不一致回归，并用真实前端 SSE 解析器读取后端响应。
- `frontend/tests/sse.test.mjs`、`frontend/tests/load-services.mjs`：分析报告协议回归。
- 新增 `frontend/tests/review-recovery.test.mjs`：只读恢复、错请求/错会话、原审批去重、持久化及存储异常。
- 新增 `frontend/tests/review-ui.mjs`：真实 Chromium/React 页面，模拟 HTTP 边界验证五种审批与恢复情形。

本轮未修改 Agent 图、RAG、知识库数据或 `.env`；云端开关保持关闭，没有重新设计 Agent 或引入新框架。原有未提交改动保留。

## 实际测试

- 后端联合离线回归：**308 项 unittest 通过，40.425 秒**，其中本轮新增 5 项；另 **17/17 组**既有离线回归通过。
- `frontend` 的 `npm test`：**33/33 通过，无跳过**。
- Chromium 实际页面：**5/5 通过**，覆盖云端未授权、工具失败、历史读取失败、断线后取回结果，以及运行中断线后跨刷新保护、确认后批准下一批新 ID。已确认的回执不会永久阻止下一次合法审批；原调用没有重复提交。
- `npm run build`、`npm run lint` 通过。
- 所有分析使用模拟模型、临时存储；真实 HTTP 断线测试仅连接本机临时端口，联合后端回归阻断外网。没有调用真实 DeepSeek/Embedding，也没有修改正式向量库。

重跑命令：

```bash
# 项目根目录，使用 agent_kb 环境
python -m unittest tests.test_analysis_agent

# frontend 目录
npm test
npm run build
npm run lint

# frontend 目录；使用现有 Playwright/Chromium，指向单独的本地 Vite 测试端口。
# HTTP API 均被模拟，不连接正式后端。
SWEET_PLAYWRIGHT_CORE_PATH=/path/to/playwright-core/index.mjs \
SWEET_REVIEW_UI_URL=http://127.0.0.1:5189 node tests/review-ui.mjs
```

本轮测试日志位于 `/tmp/sweet-review-unit.log`、`sweet-review-legacy.log`、`sweet-review-frontend-tests.log`、`sweet-review-ui-green.log`、`sweet-review-build.log` 和 `sweet-review-lint.log`。

## 生效与限制

当前正式后端进程没有启用热重载；代码修改需要重启该进程，前端需要刷新页面才能使用新构建。本轮没有重启正式服务，以免触发其启动时的知识库自动同步流程。无需再次批准已经结束的原工具调用；更新服务后检查会话状态即可读取已保存结果。

本轮仅验证模拟模型下的审批、执行、失败及恢复；没有真实云端分析验证。云端开关仍为 0 时，预期结果就是明确提示未获独立授权并结束等待，不会执行全文分析。浏览器禁止持久化时，跨刷新回执不可保证保存，后端原有的锁、指纹和 request_id 去重仍然有效。
