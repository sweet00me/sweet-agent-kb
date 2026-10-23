# 文档集合分析基础层

## 范围与复用

本阶段按 Catalog → Scope Resolver → Document Reader 实现可独立调用的 Python 服务，不接入 LangGraph、RAG、Web UI，不调用模型、Embedding 或 ChromaDB，也不实现 Map-Reduce。

已检查 `document_sync.sqlite3.parsed_documents`（55 行）、`chunk_sync.sqlite3.chunk_cache`（55 行）和 `vector_sync.sqlite3.vector_documents`（54 行）。目录以现有 `parsed_documents` 登记记录为来源，复用 `parsed_documents/*.json` 的完整解析正文，以及 `file_manager.validate_relative_path/open_directory` 的路径校验。新文件需先由现有解析同步登记；不另建解析器或文档索引。

## Catalog

新增 `app/document_analysis/catalog.py`、`security.py`。`DocumentCatalog.list_documents(CatalogFilter)` 返回目录、类型、修改日期、SHA256、解析版本、正文长度和可读状态。目录按路径边界递归匹配；类型支持 MD/TXT/DOCX；日期为文件修改日期，默认 Asia/Shanghai，起止日期均包含。

`create_snapshot()` 将排序后的文档元数据、缓存摘要、筛选条件、根目录与权限策略指纹存入单独的 `KnowledgeBase/sqlite/document_catalog.sqlite3`；不写入现有同步表。快照 ID 为规范 JSON 的 SHA256，重复创建幂等。清单不可更新/删除，加载时重算摘要；源文档或缓存后续变更不会改写旧清单，Reader 会报告版本冲突。快照不另存全文副本。

## 权限和路径

可信调用方提供不可由自然语言扩大范围的 `CatalogPolicy.allowed_prefixes`。默认信任指定 Storage 根，但继承现有同步的目录/文件排除，并强制排除 `KnowledgeBase/documents/Diary/24.7.16.md`。同一策略贯穿目录、范围和读取；策略变更需重建快照。明确请求被禁路径返回拒绝。

源文件、目录和缓存通过已有路径校验及逐级 `O_NOFOLLOW` 目录描述符打开；拒绝路径穿越、隐藏路径、符号链接、非普通文件、跨设备源文件和超限内容。数据库或缓存中的路径同样不可信，缓存必须位于配置的缓存根下。所有 SQLite 查询参数化。

## Scope Resolver

新增 `scope.py`。`ScopeResolver.resolve(text, snapshot_id, today=...)` 在指定快照内解析目录/文件名、文件类型、明确日期范围和常见相对日期；支持结构化筛选作为精确入口。识别不了、目录重名、约束含糊时返回 `needs_clarification`，越权返回 `denied`，无结果返回 `no_matches`；仅 `resolved` 提供 `ResolvedScope`。不得丢弃未理解的约束后扩大范围。

规则解析完全离线；不推断正文主题或文档的语义日期。返回规范条件、明确的文档路径和快照 ID，后续 Reader 重新核对清单成员及当前权限。

## Document Reader

新增 `reader.py`。`DocumentReader.open(scope, batch_chars=32768)` 返回迭代会话，按确定范围顺序读取完整解析缓存，不读取向量库或 Top-K。每批给出文件版本、起止字符位置、分批编号、正文和解析警告；切分不重复、不遗漏字符。

读取前核对源 SHA256、缓存 SHA256、来源/解析版本/稳定文档 ID/正文长度。输出完整文档后再次验证源文件；期间发生变化的文档不计入已验证覆盖。每份文档失败可继续下一份，并记录稳定错误码。统计已选择、已完成、失败、待处理文档、输出/已验证字符和批数；迭代提前关闭不得声称覆盖完成。

## 验证

全部新测试使用临时 Storage、实际 SQLite 和现有离线解析同步。每模块先运行失败测试，再实现并运行通过。验证筛选边界、快照幂等/篡改、路径和权限拒绝、排除文件、模糊自然语言、版本漂移、损坏/缺失缓存、长文分批和中断覆盖。最后运行现有离线回归，检查既有功能文件未改动。本阶段不运行会调用真实云端的验收脚本。
