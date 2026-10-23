# 来源引用核验

2026-10-21：引用守卫、回归测试和真实模型验收已实现。

## 行为

- 只接受本轮 `search_knowledge_tool` 的成功结果，并核对工具调用 ID；旧对话不能充当本轮证据。
- 回答的每个独立段落必须引用本轮实际返回的 `chunk_id`。
- 复用入库前的安全校验，核对原文件 SHA256、Chunk ID、序号、正文和缓存元数据；判断完成后再次核对原文件。
- 独立调用 DeepSeek 逐段判断全部事实是否受引用片段支持，并要求返回精确连续原文。
- 服务端检查核验结果是否完整、引文是否为原文的字面子串、数字是否有引文支持；不将 `A²` 当成 `A2`，也不将 `1,000` 拆成 `1`。
- 来源列表由后端生成，包括 Storage 相对路径、Chunk 序号和 ID；API 同时返回文件哈希、原文引文和核验时间。
- 无结果、过期文件、无效引用、语义不支持、格式异常或核验超时，均返回“证据不足”，不返回未经核验的回答。

同一个守卫应用于 HTTP 聊天、SSE 聊天和审批恢复。会话历史、请求重放和 SQLite Checkpoint 保存核验后的回答与报告。

SSE 保留进度、审批和终止事件，但正文等待 Agent 完成核验后才展示。普通回答也等待该轮模型完成，以免模型先输出结论、随后才决定检索。核验模型的 JSON 和中间草稿不会发送到前端。

## 接口

聊天与审批接口新增可空 `citation_report`；历史响应或审批暂停时可为空。状态：

| status | 含义 |
| --- | --- |
| `verified` | 本次规则与模型检查通过 |
| `rejected` | 核验未通过，正文为证据不足提示 |
| `not_applicable` | 本轮没有知识库检索，也没有知识库引用标记 |

报告含 `reason`、`claim_count`、`citations`；通过时另含 `checked_at`。每条 citation 含 `source`、`chunk_id`、`chunk_index`、`file_sha256`、`quotes`。前端现有 Markdown 组件直接展示核验后的正文与来源，类型定义已同步。

## 验证

```bash
python -m unittest tests.test_citation_verification tests.test_citation_api tests.test_file_preview tests.test_thread_management
python -m scripts.evaluate_citations --live --output /tmp/sweet-agent-citation-live-evaluation.json
```

- 47 项自动测试通过，其中 24 项覆盖引用守卫及真实 Graph/RAG/SQLite/HTTP/SSE，包括审批恢复、重放及 Checkpoint 重开。
- 17 个既有离线测试脚本通过；三个会访问真实环境的手工观测/工具演示未运行。
- 真实 DeepSeek 合成验收 12/12：4 个受支持陈述通过；8 个捏造数字、指标/单位混淆、否定反转、无依据比较/因果/概括被拒绝。
- 真实模型生成、真实模型核验和 HTTP/SSE 联动的两个合成问题通过，SSE 正文与最终核验回答一致。
- 当前运行的后端已加载新实现；经前端 5173 代理、正式 Chroma 检索和 SSE 的一次真实知识库查询通过，引用文件哈希与原件一致。验收会话已从历史列表软删除，本地记录保留。
- 前端 `npm run build`、`npm run lint` 通过。

实时模型验收仅使用临时合成文档，不读取私人资料或写入正式审计数据库；Token 数来自模型响应。默认离线测试不调用云端。

## 边界

`verified` 表示通过本次检查，不是事实正确性的绝对保证。来源和字面引文校验是确定性的，语义判定仍可能误判；12 个验收用例也不能外推为所有回答的准确率。

为优先保证引用可核对，格式不合格或核验模型不可用时会保守拒答。有效 RAG 回答通常增加一次模型核验调用及相应延迟；不自动重试核验。过期索引由 API 的[自动定时同步](auto-sync.md)在后续周期更新，当前请求仍需通过原件版本核验。

## 修改文件

- `app/rag/citations.py`
- `app/agents/graph.py`、`app/agents/state.py`、`app/agents/prompts.py`
- `app/tools/knowledge.py`
- `app/api/main.py`、`app/api/streaming.py`
- `app/observability/llm_metrics.py`
- `frontend/src/types/api.ts`
- `tests/test_citation_verification.py`、`tests/test_citation_api.py`
- `scripts/evaluate_citations.py`
- 本文档
