# 新 RAG 的 API/SSE 联动回归

2026-10-21：**已验证（新 Storage RAG）**。

## 结果

- 新增 12 项后端集成测试通过，复用真实 Graph、RAG、会话/请求日志及 SQLite Checkpoint；仅隔离云客户端和存储路径。
- 23 项前端协议测试通过，直接执行真实 TypeScript 服务，替换网络输入；覆盖单字节 UTF-8/CRLF 分片。
- 全部 59 项自动测试和 17 个既有离线脚本通过；前端 build、lint 通过。
- 独立代码审查通过，审查者复测了 16 项相关 Python 测试及 23 项前端测试。

## 修复

`frontend/src/services/agentStream.ts` 原先组装终止响应时遗漏 `citation_report`，导致后端已返回的引用核验信息在前端结果中丢失。

现在校验并保留核验状态、来源路径、Chunk ID/序号、文件哈希、精确引文和核验时间。旧响应缺省字段、审批响应空字段仍兼容；错误的报告结构会触发协议错误。

## 覆盖范围

| 场景 | 验证结果 |
| --- | --- |
| 新 RAG 正常检索 | HTTP/SSE 均返回核验后的回答与完整报告 |
| 空结果、原文件已修改 | 完成工作流并返回证据不足，报告为 `rejected` |
| 被排除的文档 | 不进入 HTTP/SSE 回答或来源列表 |
| 模型、Embedding 失败 | HTTP 500 或 SSE `error`；请求为 `uncertain`，同请求禁止重跑，不泄露内部异常 |
| 引用核验模型超时 | 安全拒答并正常完成，报告为 `rejected` |
| 保存回答时数据库失败 | 事务回滚，不插入助手消息、不发送 `completed`，请求为 `uncertain` |
| HTTP/SSE 相互重放 | 回答和报告完全一致，不重新检索、不重复模型调用或插入聊天消息 |
| 请求 ID 相同但内容不同 | HTTP 409，不改写历史、不重新执行 |
| 审批暂停及恢复 | 两种协议可交叉使用，保留本轮 RAG 证据与最终核验结果 |
| SSE 执行繁忙 | 登记请求之前返回 409 |
| 真实 TCP 断线 | 后台 RAG 继续完成，重连读取保存结果，只有一份用户/助手消息 |
| 前端协议异常 | 残缺事件、JSON 错误、错线程、重复终止、缺少终止、HTTP 错误及代理 HTML 均拒绝，不自动重发 |

SSE 的 HTTP 200 仅表示流连接建立，前端必须收到合法的 `completed` 或 `pending_approval` 才承认工作流结果。正文仍遵循引用核验策略：核验完成后才展示。

## 当前运行服务验收

链路：前端真实 JS 服务 → `100.71.65.23:5173` 代理 → `127.0.0.1:8010` → 正式 Chroma/百炼查询向量/DeepSeek。

查询 `rag_intro.md` 的 RAG 定义：

| 协议 | 结果 | 含跨协议重放与历史核对的耗时 |
| --- | --- | --- |
| HTTP | `verified`；原文件 SHA256 匹配；SSE 重放一致 | 3.52 秒 |
| SSE | `verified`；正文与最终结果一致；HTTP 重放一致 | 3.15 秒 |

两次原始执行均只生成一份用户/助手消息；测试会话已软删除，本地聊天及审计记录保留。原有待审批任务保留。

机器报告：`/tmp/sweet-agent-rag-api-sse-live.json`。耗时是本次观测值，不代表性能保证。

## 复现

后端使用项目 Python 环境执行：

```bash
python -m unittest tests.test_rag_api_sse tests.test_citation_api
```

前端目录执行：

```bash
npm test
npm run build
npm run lint
npm run test:live -- --live --base-url http://100.71.65.23:5173 --output /tmp/sweet-agent-rag-api-sse-live.json
```

`npm test` 不联网。`test:live` 会实际调用已配置的知识库和云模型，并归档自己创建的测试会话。三个直接访问真实环境的手工观测/工具演示不属于本次离线测试运行。

## 修改文件

- `frontend/src/services/agentStream.ts`、`frontend/package.json`
- `frontend/tests/sse.test.mjs`、`frontend/tests/load-services.mjs`、`frontend/tests/live-rag.mjs`
- `tests/test_rag_api_sse.py`、`tests/test_citation_api.py`（抽取共用测试夹具）
- 本文档
