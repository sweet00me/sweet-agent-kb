"""从已有解析目录构建文档清单；独立 SQLite 只保存不可变元数据快照。"""
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from app.rag.document_loader import MAX_CONTENT_CHARS, MAX_FILE_BYTES, SUPPORTED_EXTENSIONS
from app.rag.document_sync import EXCLUDE_FILES, EXCLUDE_NAMES, EXCLUDE_PREFIXES, STORAGE_ROOT
from app.document_analysis.security import AnalysisError as CatalogError, checked_path, database_connection, database_relative, read_checked

DEFAULT_EXCLUDED = "KnowledgeBase/documents/Diary/24.7.16.md"
MAX_CACHE_BYTES = 8 * 1024 * 1024


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def within(path: str, parent: str) -> bool:
    return not parent or path == parent or path.startswith(parent + "/")


def date_value(value: str | None):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogError("invalid_date") from exc


def path_tuple(values, *, allow_root=False):
    if not isinstance(values, (tuple, list)):
        raise CatalogError("invalid_filter")
    return tuple(sorted({checked_path(value, allow_root=allow_root) for value in values}))


@dataclass(frozen=True)
class CatalogPolicy:
    """仅可信调用方可配置；自然语言不能改写此对象。"""
    allowed_prefixes: tuple[str, ...] = ("",)
    excluded_paths: tuple[str, ...] = ()
    excluded_prefixes: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "allowed_prefixes", path_tuple(self.allowed_prefixes, allow_root=True))
        object.__setattr__(self, "excluded_paths", path_tuple(path_tuple(self.excluded_paths) + tuple(EXCLUDE_FILES) + (DEFAULT_EXCLUDED,)))
        object.__setattr__(self, "excluded_prefixes", path_tuple(path_tuple(self.excluded_prefixes) + tuple(EXCLUDE_PREFIXES)))

    @property
    def digest(self):
        return fingerprint(asdict(self))

    def allows(self, source: str) -> bool:
        checked_path(source, allow_root=True)
        return (any(within(source, prefix) for prefix in self.allowed_prefixes)
                and source not in self.excluded_paths
                and not any(within(source, prefix) for prefix in self.excluded_prefixes)
                and not any(part in EXCLUDE_NAMES for part in source.split("/")))

    def require(self, source: str):
        if not self.allows(source):
            raise CatalogError("access_denied")


@dataclass(frozen=True)
class CatalogFilter:
    directories: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()
    date_from: str | None = None
    date_to: str | None = None
    exclude: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("directories", "sources", "exclude"):
            object.__setattr__(self, name, path_tuple(getattr(self, name), allow_root=name == "directories"))
        if not isinstance(self.file_types, (tuple, list)) or any(not isinstance(v, str) for v in self.file_types):
            raise CatalogError("invalid_file_type")
        types = tuple(sorted({v.lower().lstrip(".") for v in self.file_types}))
        if any("." + extension not in SUPPORTED_EXTENSIONS for extension in types):
            raise CatalogError("invalid_file_type")
        object.__setattr__(self, "file_types", types)
        first, last = date_value(self.date_from), date_value(self.date_to)
        if first and last and first > last:
            raise CatalogError("invalid_date_range")

    def matches(self, record) -> bool:
        if (self.directories or self.sources) and not (
                record.source in self.sources or any(within(record.source, p) for p in self.directories)):
            return False
        if self.file_types and record.file_type not in self.file_types:
            return False
        if any(within(record.source, path) for path in self.exclude):
            return False
        if self.date_from or self.date_to:
            if not record.modified_date:
                return False
            if self.date_from and record.modified_date < self.date_from:
                return False
            if self.date_to and record.modified_date > self.date_to:
                return False
        return True


@dataclass(frozen=True)
class DocumentRecord:
    source: str
    sha256: str
    parser_version: str
    file_type: str
    directory: str
    status: str
    error_code: str | None = None
    modified_at: str | None = None
    modified_date: str | None = None
    size_bytes: int | None = None
    cache_path: str | None = None
    cache_sha256: str | None = None
    content_sha256: str | None = None
    document_id: str | None = None
    content_chars: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogView:
    documents: tuple[DocumentRecord, ...]
    filters: CatalogFilter
    excluded_count: int
    invalid_count: int
    undated_count: int


@dataclass(frozen=True)
class CatalogSnapshot(CatalogView):
    snapshot_id: str
    created_at: str


class DocumentCatalog:
    def __init__(self, storage_root: Path = STORAGE_ROOT, *, parsed_db: Path | None = None,
                 parsed_dir: Path | None = None, snapshot_db: Path | None = None,
                 policy: CatalogPolicy | None = None, timezone_name="Asia/Shanghai"):
        self.root = Path(storage_root).absolute()
        self.parsed_dir = Path(parsed_dir or self.root / "KnowledgeBase/parsed_documents").absolute()
        self.parsed_db = Path(parsed_db or self.root / "KnowledgeBase/sqlite/document_sync.sqlite3").absolute()
        self.snapshot_db = Path(snapshot_db or self.root / "KnowledgeBase/sqlite/document_catalog.sqlite3").absolute()
        for path in (self.parsed_db, self.snapshot_db):
            database_relative(self.root, path)
        if self.snapshot_db == self.parsed_db:
            raise CatalogError("snapshot_database_must_be_separate")
        self.policy = policy or CatalogPolicy()
        self.timezone = ZoneInfo(timezone_name)
        self.root_id = fingerprint({"root": str(self.root), "parsed_dir": str(self.parsed_dir),
                                    "timezone": timezone_name})

    def check_filter(self, filters: CatalogFilter):
        for source in filters.directories + filters.sources:
            # 空目录表示当前权限允许的全部文档，不扩充权限。
            if source:
                self.policy.require(source)

    def source_state(self, source: str):
        self.policy.require(source)
        try:
            digest, _, info = read_checked(self.root, source, MAX_FILE_BYTES)
        except CatalogError as exc:
            if exc.code == "file_missing":
                raise CatalogError("source_missing") from exc
            raise
        return digest, info

    def _cache_relative(self, value: str) -> str:
        if not isinstance(value, str):
            raise CatalogError("invalid_cache")
        try:
            path = Path(value)
            relative = path.relative_to(self.parsed_dir).as_posix() if path.is_absolute() else value
            return checked_path(relative)
        except (ValueError, CatalogError) as exc:
            raise CatalogError("invalid_cache") from exc

    def _read_cache(self, relative: str):
        try:
            digest, raw, _ = read_checked(self.parsed_dir, relative, MAX_CACHE_BYTES, keep_bytes=True)
            record = json.loads(raw.decode("utf-8"))
            if not isinstance(record, dict):
                raise ValueError()
            return digest, record
        except CatalogError as exc:
            raise CatalogError("cache_missing" if exc.code == "file_missing" else "invalid_cache") from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CatalogError("invalid_cache") from exc

    def _validate_cache(self, cached: dict, source: str, sha: str, version: str):
        content = cached.get("content")
        identity = hashlib.sha256((source + "\0" + sha).encode("utf-8")).hexdigest()[:32]
        warnings = cached.get("warnings")
        if (cached.get("source") != source or cached.get("sha256") != sha
                or cached.get("parser_version") != version or cached.get("document_id") != identity
                or cached.get("file_type") != Path(source).suffix.lower().lstrip(".")
                or not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT_CHARS
                or type(cached.get("content_chars")) is not int or cached["content_chars"] != len(content)
                or not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings)):
            raise CatalogError("cache_metadata_mismatch")
        try:
            content.encode("utf-8")
        except UnicodeError as exc:
            raise CatalogError("cache_metadata_mismatch") from exc
        return content

    def _inspect(self, row) -> DocumentRecord:
        source, sha, version, parsed_path = row
        values = dict(source=source, sha256=sha, parser_version=version,
                      file_type=Path(source).suffix.lower().lstrip("."), directory=source.rpartition("/")[0])
        try:
            current, info = self.source_state(source)
            modified = datetime.fromtimestamp(info.st_mtime, timezone.utc)
            values.update(modified_at=modified.isoformat(), modified_date=modified.astimezone(self.timezone).date().isoformat(),
                          size_bytes=info.st_size)
            if current != sha:
                raise CatalogError("source_changed")
            relative = self._cache_relative(parsed_path)
            cache_sha, cached = self._read_cache(relative)
            content = self._validate_cache(cached, source, sha, version)
            if self.source_state(source)[0] != sha:
                raise CatalogError("source_changed")
            values.update(cache_path=relative, cache_sha256=cache_sha,
                          content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                          document_id=cached["document_id"], content_chars=len(content), warnings=tuple(cached["warnings"]))
            return DocumentRecord(**values, status="ready")
        except CatalogError as exc:
            status = {"source_missing": "missing", "source_changed": "stale", "access_denied": "access_denied"}.get(exc.code, "invalid_cache")
            return DocumentRecord(**values, status=status, error_code=exc.code)

    def list_documents(self, filters: CatalogFilter | None = None) -> CatalogView:
        filters = filters or CatalogFilter()
        self.check_filter(filters)
        try:
            with database_connection(self.root, self.parsed_db) as connection:
                rows = connection.execute("SELECT source_path, sha256, parser_version, parsed_path FROM parsed_documents ORDER BY source_path").fetchall()
        except sqlite3.Error as exc:
            raise CatalogError("parsed_index_unavailable") from exc
        documents, excluded, invalid, undated = [], 0, 0, 0
        for row in rows:
            try:
                source = checked_path(row[0])
                if not isinstance(row[1], str) or not re.fullmatch(r"[0-9a-f]{64}", row[1]) or not isinstance(row[2], str) or not row[2]:
                    raise CatalogError("invalid_catalog_row")
                if not self.policy.allows(source):
                    excluded += 1
                    continue
                if Path(source).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    raise CatalogError("invalid_catalog_row")
            except CatalogError:
                invalid += 1
                continue
            # 先按路径/类型缩小，再读取元数据；不打开被筛掉的正文。
            selector = DocumentRecord(source, row[1], row[2], Path(source).suffix.lower().lstrip("."), source.rpartition("/")[0], "pending")
            without_dates = CatalogFilter(filters.directories, filters.sources, filters.file_types, exclude=filters.exclude)
            if without_dates.matches(selector):
                document = self._inspect(row)
                if (filters.date_from or filters.date_to) and document.modified_date is None:
                    undated += 1
                if filters.matches(document):
                    documents.append(document)
        return CatalogView(tuple(documents), filters, excluded, invalid, undated)

    def read_verified(self, entry: DocumentRecord) -> str:
        self.policy.require(entry.source)
        if self.source_state(entry.source)[0] != entry.sha256:
            raise CatalogError("source_changed")
        if entry.status != "ready" or not entry.cache_path:
            raise CatalogError(entry.error_code or "document_not_ready")
        digest, cached = self._read_cache(entry.cache_path)
        if digest != entry.cache_sha256:
            raise CatalogError("cache_changed")
        content = self._validate_cache(cached, entry.source, entry.sha256, entry.parser_version)
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != entry.content_sha256:
            raise CatalogError("cache_changed")
        if self.source_state(entry.source)[0] != entry.sha256:
            raise CatalogError("source_changed")
        return content

    def create_snapshot(self, filters: CatalogFilter | None = None) -> CatalogSnapshot:
        view = self.list_documents(filters)
        manifest = {"format_version": 1, "root_id": self.root_id, "policy": self.policy.digest,
                    "filters": asdict(view.filters), "documents": [asdict(d) for d in view.documents],
                    "excluded_count": view.excluded_count, "invalid_count": view.invalid_count, "undated_count": view.undated_count}
        encoded = canonical(manifest)
        snapshot_id = fingerprint(manifest)
        with database_connection(self.root, self.snapshot_db, write=True, create=True, forbidden=self.parsed_db) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            if tables - {"catalog_snapshots"}:
                raise CatalogError("snapshot_database_not_dedicated")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS catalog_snapshots (
                    snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, manifest_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS catalog_snapshots_no_update BEFORE UPDATE ON catalog_snapshots
                BEGIN SELECT RAISE(ABORT, 'immutable_snapshot'); END;
                CREATE TRIGGER IF NOT EXISTS catalog_snapshots_no_delete BEFORE DELETE ON catalog_snapshots
                BEGIN SELECT RAISE(ABORT, 'immutable_snapshot'); END;
            """)
            with connection:
                connection.execute("INSERT OR IGNORE INTO catalog_snapshots VALUES (?,?,?)",
                                   (snapshot_id, datetime.now(timezone.utc).isoformat(), encoded))
        return self.get_snapshot(snapshot_id)

    def get_snapshot(self, snapshot_id: str) -> CatalogSnapshot:
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
            raise CatalogError("invalid_snapshot_id")
        try:
            with database_connection(self.root, self.snapshot_db) as connection:
                row = connection.execute("SELECT created_at,manifest_json FROM catalog_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        except sqlite3.Error as exc:
            raise CatalogError("snapshot_unavailable") from exc
        if row is None:
            raise CatalogError("snapshot_not_found")
        try:
            manifest = json.loads(row[1])
            if fingerprint(manifest) != snapshot_id or manifest.get("format_version") != 1 or manifest.get("root_id") != self.root_id:
                raise CatalogError("snapshot_integrity_error")
            if manifest.get("policy") != self.policy.digest:
                raise CatalogError("snapshot_policy_changed")
            filters = CatalogFilter(**manifest["filters"])
            self.check_filter(filters)
            documents = []
            for value in manifest["documents"]:
                self.policy.require(value["source"])
                documents.append(DocumentRecord(**(value | {"warnings": tuple(value["warnings"])})))
            return CatalogSnapshot(tuple(documents), filters, manifest["excluded_count"], manifest["invalid_count"], manifest["undated_count"], snapshot_id, row[0])
        except CatalogError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CatalogError("snapshot_integrity_error") from exc
