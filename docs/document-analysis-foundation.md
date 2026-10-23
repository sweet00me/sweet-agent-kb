# 文档集合分析基础层

本阶段提供三个离线 Python 模块：`app/document_analysis/catalog.py`、`scope.py`、`reader.py`。共享路径读取位于 `security.py`，包入口为 `__init__.py`。不接入现有 Agent、HTTP/SSE 或 Web UI；没有 DeepSeek、Embedding、ChromaDB 调用，也没有 Map-Reduce。

新增文件：

| 位置 | 文件与职责 |
| --- | --- |
| `app/document_analysis/` | `__init__.py` 包入口；`security.py` 安全路径、描述符和 SQLite 连接；`catalog.py` 目录/快照；`scope.py` 范围解析；`reader.py` 全文分批/覆盖统计 |
| `tests/` | `document_analysis_support.py` 隔离夹具；`test_document_catalog.py`、`test_scope_resolver.py`、`test_document_reader.py` 分模块离线回归 |
| `docs/` | 本说明；`superpowers/specs/2026-10-21-document-analysis-foundation-design.md` 设计；`superpowers/plans/2026-10-21-document-analysis-foundation.md` 实施记录 |

## 已检查与复用的存储

| 现有 SQLite 表 | 检查时行数 | 本阶段用途 |
| --- | ---: | --- |
| `document_sync.sqlite3.parsed_documents` | 55 | 只读文档路径、源 SHA256、解析版本与缓存路径 |
| `chunk_sync.sqlite3.chunk_cache` | 55 | 仅检查结构，不用 Chunk 代替全文 |
| `vector_sync.sqlite3.vector_documents` | 54 | 仅检查结构，不参与范围或正文读取 |

原表 `parsed_documents(source_path, sha256, parser_version, parsed_path, updated_at)` 无需迁移。完整解析缓存沿用 `KnowledgeBase/parsed_documents/*.json`，支持现有 MD、TXT、DOCX 解析正文。目录和来源安全校验复用文件管理器的 `validate_relative_path()` 与 `open_directory()`；源文件和缓存再以 `O_NOFOLLOW` 逐级打开。

新建的 `KnowledgeBase/sqlite/document_catalog.sqlite3` 仅在显式创建快照时写入：

```sql
CREATE TABLE catalog_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);
```

清单 JSON 包含筛选条件、元数据、源与缓存哈希、根目录和权限策略指纹，不保存正文副本。规范 JSON 的 SHA256 是清单 ID；相同输入产生相同 ID，重复创建返回同一快照。UPDATE/DELETE 触发器禁止覆盖旧快照，读取时重新验证清单摘要。新库权限为 `0600`，不改动三个同步库、解析缓存或向量库。

配置的数据库路径必须位于指定 Storage 根下；逐级拒绝符号链接、穿越和硬链接别名。SQLite 从固定父目录描述符入口连接，连接前后核对文件身份；快照写入还会拒绝解析库的相同 inode 和包含其他业务表的库，避免错误配置污染既有状态。

## 调用示例

以下示例会创建一份元数据快照，并离线读取确定范围的全文；不调用云端：

```python
from pathlib import Path
from app.document_analysis.catalog import DocumentCatalog, CatalogFilter, CatalogPolicy
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.reader import DocumentReader

catalog = DocumentCatalog(
    Path("/home/sweet/Storage"),
    policy=CatalogPolicy(allowed_prefixes=("KnowledgeBase/documents/notes",)),
)

# 查询不落盘；目录递归，日期使用上海时区的文件修改日期，首尾日期包含。
filters = CatalogFilter(
    directories=("KnowledgeBase/documents/notes",),
    file_types=("md", "txt"),
    date_from="2024-01-01", date_to="2026-12-31",
)
view = catalog.list_documents(filters)
snapshot = catalog.create_snapshot(filters)

result = ScopeResolver(catalog).resolve("分析 notes 目录下的全部文档", snapshot.snapshot_id)
# 精确入口：ScopeResolver(catalog).resolve_filter(filters, snapshot.snapshot_id)
if result.status != "resolved":
    raise ValueError((result.status, result.code))

with DocumentReader(catalog).open(result.scope, batch_chars=32768) as session:
    for batch in session:
        consume(batch.source, batch.sha256, batch.start_char, batch.end_char, batch.text)
    report = session.coverage
```

`consume()` 是调用方自己的接收函数。本阶段不提供模型调用或后续分析函数。`DocumentCatalog` 可通过 `parsed_db`、`parsed_dir`、`snapshot_db` 参数使用隔离存储，测试全部使用临时目录。

## 1. Document Catalog

`list_documents()` 返回 `CatalogView`；`create_snapshot()` 和 `get_snapshot(id)` 返回不可变的 `CatalogSnapshot`。`documents` 中每条记录含来源、目录、类型、修改日期、大小、源/缓存 SHA256、解析版本、完整正文字符数、警告和状态。`excluded_count` 记录被权限或排除规则移出的登记记录数，`invalid_count` 记录非法登记记录数，`undated_count` 记录日期筛选时无法确认日期的候选文件数。

筛选支持目录、精确来源、文件类型、起止日期和进一步排除路径。多个目录/来源的结构化选择为并集，再与类型、日期取交集；目录以路径段边界匹配，不把 `notes-other` 当成 `notes` 的子目录。

`ready` 表示源 SHA256、缓存元数据和正文长度通过校验；`stale`、`missing`、`access_denied`、`invalid_cache` 保留可识别的异常记录。缺失文件无法确认修改日期，不会被认定为满足日期条件，并计入 `undated_count`；涉及这些候选文件的日期范围解析要求澄清或先修复目录状态，不将遗漏冒充完整范围。新文件须先通过已有文档同步登记；本模块不扫描新文件、不重新解析、不向量化。

## 2. Scope Resolver

范围始终是指定快照内的子集。支持完整相对路径、唯一目录名/文件名、MD/Markdown、TXT、DOCX/Word，以及 ISO 日期范围、中文年月、今天/昨天、本月/上个月、今年/去年、最近 N 天/最近一周。相对日期允许传入 `today`，便于重现与测试。

示例：

- `分析 KnowledgeBase/documents/notes 目录下从2024-01-02到2024-01-03的 Markdown 和 TXT 文档`
- `读取 notes 目录下的 alpha.md 文件`
- `分析所有文档，排除 private 目录`
- `分析所有文档中最近7天修改的文档`

返回状态：`resolved`、`needs_clarification`、`denied`、`no_matches`。仅 `resolved` 返回 `ResolvedScope(snapshot_id, sources, filters)`；`scope_id` 标识规范范围。无法识别的语义约束、重名、含糊日期或目录与文件组合要求澄清，不丢弃条件后选取更大范围。精确控制应调用 `resolve_filter()`。

权限白名单只能由可信调用方设置。默认以配置的 Storage 根为边界，继承既有同步目录/文件排除，并始终排除 `KnowledgeBase/documents/Diary/24.7.16.md`；传入空自定义排除也不能解除。明确请求禁止路径会拒绝，权限策略改变后旧快照不能继续使用。当前没有多用户身份系统，本阶段不提供登录鉴权；未来 API 必须从服务端身份生成白名单，不能信任用户提交的策略。

## 3. Document Reader

Reader 重新核对快照和范围，不把 `ResolvedScope` 当作权限令牌。逐份读取完整解析正文，不使用检索距离、Top-K 或向量 Chunk。

每个 `DocumentBatch` 包含来源、文档 ID、源 SHA256、解析版本、从 0 开始的批次编号、Python Unicode 字符区间 `[start_char, end_char)`、总字符数、正文、`is_last` 和解析警告。默认每批 32,768 字符，可配置为 1–1,000,000 字符；无重叠切分，合并即原完整解析正文。内存最多持有当前一份解析 JSON/正文，单份沿用解析器的 20 MiB 文件和 1,000,000 字符限制。

读取开始前验证源文件和缓存 SHA256、来源、解析版本、稳定文档 ID 和正文长度；每批输出前复查权限和策略指纹，最后一批输出前检查源版本。最后一批交给消费方后，迭代继续时再次验证源和缓存，成功后才计为完成。`is_last` 仅表示该文档的数据末批，不代表最终核验完成。若读取中发生变化，前面已输出批次应视为暂存数据，该文档不会计入已验证覆盖。逐文档失败继续其他文件，错误仅给稳定错误码，如 `source_changed`、`cache_changed`、`source_missing`、`cache_missing`、`invalid_cache`、`access_denied`、`snapshot_policy_changed`。

`session.coverage` 返回快照/范围 ID、状态、所选/已完成/失败/待处理文档数、预计/已输出/已验证字符、批次数、按文档计算的覆盖率和逐文档错误。预计字符无法确认时为 `None`。只有所有所选文档完整输出且版本复查通过，才有 `complete=True`。提前停止须使用 `with` 或 `close()`，状态为 `cancelled`；未耗尽的迭代不能证明完整覆盖。

## 测试与限制

```bash
python -m unittest tests.test_document_catalog
python -m unittest tests.test_scope_resolver
python -m unittest tests.test_document_reader
```

测试使用实际临时 SQLite、现有离线解析同步和 MD/TXT/DOCX 缓存，并阻断 socket 连接。覆盖筛选、快照持久化/篡改、默认排除和权限、自然语言澄清、正文重组、长文/Unicode、文件和缓存漂移、读取中断及错误统计。

本次验收：Catalog 18 项、Scope 18 项、Reader 17 项，新增合计 **53 项通过**；结合既有功能共 **154 项单元/集成测试通过**，另有 **17 个离线模块通过**。联合回归阻断外部 DNS 和网络，允许既有测试所需的本地回环 HTTP。日志位于 `/tmp/sweet-document-foundation-regression/`。未运行真实云模型、Embedding 或向量库的在线验收。

限制：自然语言只支持明确规则，不能推断正文主题；日期指修改日期而非正文日期；快照固定清单和版本摘要，不归档旧正文，后续源/缓存变化需新建快照；DOCX 沿用现有解析器，图片、公式等未恢复部分保留警告，不能声称覆盖原二进制内容。目录描述符与 SQLite 入口依赖当前 Ubuntu/Linux 的 `/proc/self/fd`。未开发 HTTP 接口、Web UI 或 Map-Reduce。
