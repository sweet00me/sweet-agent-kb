# Document Analysis Foundation Implementation Plan

> 执行方式：在当前会话按用户指定顺序逐模块完成；每步先红后绿，不提交 Git、不修改现有服务流程。

**Goal:** 建立离线、可审计的固定文档清单、分析范围解析和完整正文读取基础。

**Architecture:** 读取现有解析表与缓存，独立 SQLite 只保存不可变清单。权限在三层重复核对，范围始终是快照的子集。

**Tech Stack:** Python 3.11、SQLite、标准库、现有 document_sync/file_manager。

**Spec:** `docs/superpowers/specs/2026-10-21-document-analysis-foundation-design.md`

## Global Constraints

- 不调用 DeepSeek/Embedding，不访问 ChromaDB；不修改 LangGraph/RAG/Web UI 现有功能。
- 强制排除 `KnowledgeBase/documents/Diary/24.7.16.md`。
- 使用临时目录离线测试；本阶段不写入正式 Storage。

## Review Focus

- 数据库/缓存路径含穿越、符号链接或别名时必须拒绝。
- 自然语言包含未理解的否定、日期或目录限制时不能扩大范围。
- 文件/缓存在生成快照后发生变化时必须报告冲突。
- 权限收紧或伪造范围不能绕过读取时的校验。
- 读取中断和输出期间文件变更不能被统计为完整覆盖。

### Task 1: Document Catalog

**Files:** `app/document_analysis/{__init__,catalog,security}.py`、`tests/{document_analysis_support,test_document_catalog}.py`。

**Produces:** `CatalogFilter`、`CatalogPolicy`、`DocumentCatalog.list_documents/create_snapshot/get_snapshot`，以及共享的安全读取和缓存验证。

- [x] 编写目录/类型/日期交集、默认排除、权限、缓存状态、固定快照与安全路径测试。
- [x] 运行 `python -m unittest tests.test_document_catalog`，确认缺失实现导致失败。
- [x] 实现从 `parsed_documents` 读取元数据、校验缓存与源版本、单独 SQLite 清单持久化。
- [x] 重跑该测试（11 项通过）及现有解析同步测试，确认通过。

### Task 2: Scope Resolver

**Files:** `app/document_analysis/scope.py`、`tests/test_scope_resolver.py`。

**Consumes:** Catalog 固定快照与筛选、权限校验。

**Produces:** `ScopeResolver.resolve/resolve_filter`、`ScopeResolution`、`ResolvedScope`。

- [x] 编写目录/文件/类型/日期自然语言、重名、未知约束、拒绝/排除、快照子集测试。
- [x] 运行 `python -m unittest tests.test_scope_resolver`，确认失败。
- [x] 实现有限规则解析和结构化入口；无法明确解析的请求返回澄清，不作宽泛猜测。
- [x] 重跑 Catalog 和 Scope 离线测试，25 项通过。补充目录上下文与文件名取交集的先红后绿验证，避免扩大范围。

### Task 3: Document Reader

**Files:** `app/document_analysis/reader.py`、`tests/test_document_reader.py`、`docs/document-analysis-foundation.md`。

**Consumes:** Catalog 快照和 `ResolvedScope`，复用完整解析缓存。

**Produces:** `DocumentReader.open`、正文批次与覆盖统计。

- [x] 编写全文重组、长文/Unicode、SHA/缓存损坏、权限变化、伪造范围、提前关闭、读取中版本变化测试。
- [x] 运行 `python -m unittest tests.test_document_reader`，确认失败。
- [x] 实现逐文档读取、分批、源/缓存校验、错误隔离和最终覆盖统计。
- [x] 三模块联合运行，再执行既有离线回归；核对无云端调用、向量库写入与现有功能文件变动。
- [x] 完成用法、SQLite 表结构、验证结果和已知限制说明。

## 最终验证记录

- Catalog 18 项、Scope 18 项、Reader 17 项，合计 53 项新增测试通过；每模块均先观察缺失实现失败，再观察通过。
- 新旧联合 154 项单元/集成测试通过；17 个既有离线主入口模块通过。测试包装阻断外部 DNS/网络，HTTP/SSE 回归仅使用本地服务与模型替身。
- 独立只读复核发现的六项问题均已补先红后绿测试并修复：SQLite 别名隔离、否定条件、嵌套目录、逐批权限、读中缓存漂移、最后 yield 后的完成判定。
- 另补未知目录名不能转成类型/日期条件、目录与文件上下文不能取并集、未知日期须澄清、字符串策略不得弱化排除、数据库连接窗口替换、普通目录并列与嵌套语义区分，以及专用 SQLite 表检查。
- 当前改动仅新增模块、测试和文档；没有修改既有 Graph/RAG/UI 或触发实际云端验收，不创建正式清单库。
