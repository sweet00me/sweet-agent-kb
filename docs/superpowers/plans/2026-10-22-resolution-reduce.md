# Entity Resolution and Evidence Reduce Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans sequentially in this session; TDD for each deliverable and one fresh final review.

**Goal:** 保存保守的跨文档实体关联，并对证据进行可溯源、可恢复的 Python Reduce。
**Architecture:** 共享输入快照与派生结果存储；Entity 规则优先，Reduce 精确计算与语义接口分离。
**Tech Stack:** Python 3.11、Pydantic 2、SQLite、Linux flock；不新增依赖。
**Spec:** `docs/superpowers/specs/2026-10-22-resolution-reduce-design.md`

## Global Constraints

- 不修改 RAG、LangGraph、UI；不实现 Verification、Coverage Report 或子图。
- 所有测试离线、临时目录；不调用 DeepSeek/Embedding，不写正式向量库。
- 复用原文/范围/权限/日记排除校验；semantic_verified 恒为 false。
- 用户已授权直接实现；在现有含未提交功能的工作目录内最小修改，不提交或回滚原有内容。

## Review Focus

- 同名、代词、未落在引用内的结构化名称不得成为自动合并依据。
- same 的传递闭包遇到 different 时不得悄悄合并。
- 零证据文档与已完成早期文档的版本变化也必须发现。
- 上游部分失败、模型截断/伪造 ID、恢复检查点不得变成完整分析。
- 去重必须保留所有出处，不把陈述组/提及数量写成现实事件或人数。

## Task 1: Schema 与已授权输入

Create `synthesis_schema.py`, `evidence_input.py`; Test `test_synthesis_input.py`。
Consumes EvidenceStore public APIs; produces `load_input(store, task_ids)` and typed mention/relation/assistant/summary models.
- [x] 写失败测试：来源/语义状态分离、上游失败、零证据源变化、未知证据/权限、输入稳定性。
- [x] 运行 `python -m unittest tests.test_synthesis_input`，确认缺失功能导致失败。
- [x] 实现输入指纹、引用校验和严格注入协议；运行测试至通过。

## Task 2: 派生 Store 与运行恢复

Create `synthesis_store.py`, `synthesis_runtime.py`; Modify shared lock helper in `security.py` and corresponding Map Store delegation; Test `test_synthesis_store.py`。
Produces `SynthesisStore`, incremental checkpoint/lease/result/provenance APIs and `execute_stage`。
- [x] 写失败测试：专用库、幂等、事务/载荷、锁、中断恢复、版本和出处。
- [x] 运行新测试确认失败，实施并验证新测试及既有 Evidence Store/Map 测试。

## Task 3: Entity Resolution

Create `entity_resolution.py`; Test `test_entity_resolution.py`。
Produces `EntityResolver(store, assistant=None).run(task_ids, config=..., cancel=None)`。
- [x] 写失败测试：四态、同名、代词、绑定标识、别名、不同约束、模型降级/引用、部分上游、检查点恢复。
- [x] 运行确认失败；实现派生提及、规则、辅助建议与保守分组；运行测试至通过。

## Task 4: Evidence Reduce

Create `evidence_reduce.py`; Test `test_evidence_reduce.py`。
Produces `EvidenceReducer(store, summarizer=None).run(resolution_run_id, config=..., cancel=None)`。
- [x] 写失败测试：原文重复、跨文档陈述组、参数 Decimal/单位、潜在冲突、时间线、未知来源、任务恢复。
- [x] 运行确认失败；实现 Python 聚合与可注入分批归纳；运行测试至通过。

## Task 5: 验证与交付

Create `docs/entity-resolution-reduce.md`; Modify `.env.example` default-off flags。
- [x] 最终独立只读代码复核，实质问题按先红后绿修复。
- [x] 新增测试联合现有离线单测和 17 组历史离线回归，禁止外部网络。
- [x] 记录文件清单、接口、测试结果和限制；检查 diff/正式 Storage 未创建派生库。

## 完成记录

- Task 1 完成：输入与 Schema 6 项离线测试通过。
- Task 2 完成：派生存储 8 项测试通过；共享锁改动已通过既有 Map/Store 回归。
- Task 3 完成：关联规则 18 项测试通过，包括姓名/别名边界、常见代词及跨三文档冲突来源。
- Task 4 完成：Reduce 19 项测试通过，包括精确小数、未知身份、冲突、部分任务、恢复及空证据拒绝。
- Task 5 完成：独立只读复核的两组实质问题已先红后绿修复；新测试共 51 项，联合 246 项测试通过（21.390s），17 组历史离线回归全部通过。
- 代码清单、公开接口与限制已写入 docs/entity-resolution-reduce.md；未调用云端或创建正式 Storage 的派生库。
- 执行说明：遵循用户直接实施要求，在现有脏工作目录内顺序实现，未提交或回滚他人已有修改。所有新结果暂未接入 RAG/Graph/UI。
