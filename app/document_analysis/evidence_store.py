"""独立证据 SQLite：事务检查点、暂存/验证状态、幂等恢复和来源查询。"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from pydantic import ValidationError

from app.document_analysis.catalog import CatalogError, CatalogFilter, canonical, fingerprint
from app.document_analysis.evidence_schema import (
    EvidenceRecord, ExtractionResponse, ExtractorIdentity, MapConfig, bind_evidence, get_template,
)
from app.document_analysis.scope import ResolvedScope, ScopeResolver
from app.document_analysis.security import database_connection, database_relative, exclusive_analysis_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_tasks (
 task_id TEXT PRIMARY KEY, definition_hash TEXT NOT NULL, definition_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','partial','failed','cancelled','interrupted')),
 run_token TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_documents (
 task_id TEXT NOT NULL REFERENCES analysis_tasks(task_id), source TEXT NOT NULL,
 document_id TEXT NOT NULL, sha256 TEXT NOT NULL, content_chars INTEGER,
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','version_changed','cancelled','interrupted')),
 attempts INTEGER NOT NULL DEFAULT 0, processed_chars INTEGER NOT NULL DEFAULT 0,
 last_batch INTEGER NOT NULL DEFAULT -1, error_code TEXT,
 PRIMARY KEY(task_id,source), UNIQUE(task_id,document_id)
);
CREATE TABLE IF NOT EXISTS analysis_batches (
 task_id TEXT NOT NULL, source TEXT NOT NULL, batch_index INTEGER NOT NULL,
 start_char INTEGER NOT NULL, new_start_char INTEGER NOT NULL, end_char INTEGER NOT NULL,
 window_digest TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,source,batch_index),
 FOREIGN KEY(task_id,source) REFERENCES analysis_documents(task_id,source)
);
CREATE TABLE IF NOT EXISTS analysis_evidence (
 evidence_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source TEXT NOT NULL, start_char INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('provisional','verified','invalidated')),
 payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(task_id,source) REFERENCES analysis_documents(task_id,source)
);
CREATE INDEX IF NOT EXISTS analysis_evidence_document ON analysis_evidence(task_id,source,start_char);
"""
TABLES = {"analysis_tasks", "analysis_documents", "analysis_batches", "analysis_evidence"}
VERSION_ERRORS = {"source_changed", "source_missing", "cache_changed", "cache_missing", "cache_metadata_mismatch",
                  "evidence_version_mismatch", "checkpoint_version_mismatch"}


def now():
    return datetime.now(timezone.utc).isoformat()


class EvidenceStore:
    def __init__(self, catalog, *, path: Path | None = None):
        self.catalog = catalog
        self.path = Path(path or catalog.root / "KnowledgeBase/sqlite/evidence_store.sqlite3").absolute()
        database_relative(catalog.root, self.path)

    @contextmanager
    def _connect(self, *, write=False, initialize=False):
        with database_connection(self.catalog.root, self.path, write=write, create=initialize,
                                 forbidden=self.catalog.parsed_db) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            if tables - TABLES or db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise CatalogError("evidence_database_not_dedicated")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if initialize:
                db.executescript(SCHEMA + "\nPRAGMA user_version=1;")
            yield db

    def _raw_task(self, task_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM analysis_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise CatalogError("analysis_task_not_found")
        return dict(row)

    def _context(self, task_id):
        task = self._raw_task(task_id)
        try:
            definition = json.loads(task["definition_json"])
            if fingerprint(definition) != task["definition_hash"]:
                raise CatalogError("analysis_task_integrity_error")
            value = definition["scope"]
            scope = ResolvedScope(value["snapshot_id"], tuple(value["sources"]), CatalogFilter(**value["filters"]))
            resolution = ScopeResolver(self.catalog).resolve_filter(scope.filters, scope.snapshot_id)
            if resolution.status != "resolved" or resolution.scope != scope or definition["scope_id"] != scope.scope_id:
                raise CatalogError("analysis_scope_unavailable")
            snapshot = self.catalog.get_snapshot(scope.snapshot_id)
            return task, definition, scope, {entry.source: entry for entry in snapshot.documents if entry.source in scope.sources}
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CatalogError):
                raise
            raise CatalogError("analysis_task_integrity_error") from exc

    def create_task(self, scope: ResolvedScope, template_id: str, extractor: ExtractorIdentity,
                    config: MapConfig = MapConfig(), *, task_id=None):
        template = get_template(template_id)
        resolution = ScopeResolver(self.catalog).resolve_filter(scope.filters, scope.snapshot_id)
        if resolution.status != "resolved" or resolution.scope != scope:
            raise CatalogError("invalid_scope")
        definition = {"schema_version": 1, "scope": asdict(scope), "scope_id": scope.scope_id,
                      "template_id": template_id, "template_version": template.version,
                      "extractor": extractor.model_dump(mode="json"), "config": asdict(config)}
        digest = fingerprint(definition)
        task_id = task_id or digest
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", task_id):
            raise CatalogError("invalid_task_id")
        snapshot = self.catalog.get_snapshot(scope.snapshot_id)
        with self._connect(write=True, initialize=True) as db:
            with db:
                previous = db.execute("SELECT definition_hash FROM analysis_tasks WHERE task_id=?", (task_id,)).fetchone()
                if previous and previous[0] != digest:
                    raise CatalogError("analysis_task_conflict")
                db.execute("INSERT OR IGNORE INTO analysis_tasks VALUES (?,?,?,'pending',NULL,?,?)",
                           (task_id, digest, canonical(definition), now(), now()))
                for entry in snapshot.documents:
                    if entry.source in scope.sources:
                        document_id = entry.document_id or hashlib.sha256((entry.source + "\0" + entry.sha256).encode()).hexdigest()[:32]
                        db.execute("INSERT OR IGNORE INTO analysis_documents(task_id,source,document_id,sha256,content_chars,status) VALUES (?,?,?,?,?,'pending')",
                                   (task_id, entry.source, document_id, entry.sha256, entry.content_chars))
        return self.get_task(task_id)

    def _summary(self, task_id):
        task = self._raw_task(task_id)
        with self._connect() as db:
            groups = dict(db.execute("SELECT status,COUNT(*) FROM analysis_documents WHERE task_id=? GROUP BY status", (task_id,)).fetchall())
        return {"task_id": task_id, "status": task["status"], "counts": {"total_documents": sum(groups.values()), **groups}}

    def get_task(self, task_id):
        _, definition, _, _ = self._context(task_id)
        return self._summary(task_id) | {"definition": definition}

    def get_document(self, task_id, document):
        self._context(task_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM analysis_documents WHERE task_id=? AND (source=? OR document_id=?)",
                             (task_id, document, document)).fetchone()
        return dict(row) if row else None

    def get_batches(self, task_id, source):
        _, _, _, entries = self._context(task_id)
        if source not in entries:
            raise CatalogError("document_outside_scope")
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM analysis_batches WHERE task_id=? AND source=? ORDER BY batch_index", (task_id, source))]

    @staticmethod
    def _lease(db, task_id, token):
        row = db.execute("SELECT status,run_token FROM analysis_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row or row[0] != "running" or row[1] != token or not token:
            raise CatalogError("invalid_analysis_lease")

    @contextmanager
    def run_guard(self, task_id):
        self._context(task_id)
        lock_name = "evidence-run-" + fingerprint(str(self.path))[:16] + ".lock"
        with exclusive_analysis_lock(self.catalog.root, self.path.parent / lock_name):
            token = uuid.uuid4().hex
            with self._connect(write=True) as db:
                with db:
                    db.execute("UPDATE analysis_documents SET status='interrupted' WHERE status='running'")
                    db.execute("UPDATE analysis_tasks SET status='interrupted',run_token=NULL,updated_at=? WHERE status='running'", (now(),))
                    db.execute("UPDATE analysis_tasks SET status='running',run_token=?,updated_at=? WHERE task_id=?", (token, now(), task_id))
            try:
                yield token
            finally:
                with self._connect(write=True) as db:
                    with db:
                        owned = db.execute("SELECT 1 FROM analysis_tasks WHERE task_id=? AND status='running' AND run_token=?", (task_id, token)).fetchone()
                        if owned:
                            db.execute("UPDATE analysis_documents SET status='interrupted' WHERE task_id=? AND status='running'", (task_id,))
                            db.execute("UPDATE analysis_tasks SET status='interrupted',run_token=NULL,updated_at=? WHERE task_id=?", (now(), task_id))

    def begin_document(self, task_id, source, token):
        _, _, _, entries = self._context(task_id)
        if source not in entries:
            raise CatalogError("document_outside_scope")
        with self._connect(write=True) as db:
            with db:
                self._lease(db, task_id, token)
                db.execute("UPDATE analysis_documents SET status='running',attempts=attempts+1,error_code=NULL WHERE task_id=? AND source=?", (task_id, source))

    def _window(self, task_id, source, window):
        _, definition, _, entries = self._context(task_id)
        if source not in entries or window.source != source:
            raise CatalogError("document_outside_scope")
        entry = entries[source]
        text = self.catalog.read_verified(entry)
        config = MapConfig(**definition["config"])
        start = window.index * config.batch_chars
        if (window.document_id != entry.document_id or window.sha256 != entry.sha256
                or window.new_start_char != start or window.start_char != max(0, start - config.overlap_chars)
                or window.end_char != min(start + config.batch_chars, len(text))
                or window.total_chars != len(text) or window.text != text[window.start_char:window.end_char]):
            raise CatalogError("invalid_extraction_window")
        return entry, text, definition

    def check_batch(self, task_id, source, window):
        self._window(task_id, source, window)
        with self._connect() as db:
            row = db.execute("SELECT window_digest FROM analysis_batches WHERE task_id=? AND source=? AND batch_index=?",
                             (task_id, source, window.index)).fetchone()
        if row and row[0] != window.digest:
            raise CatalogError("checkpoint_version_mismatch")
        return bool(row)

    def save_batch(self, task_id, source, token, window, response: ExtractionResponse):
        entry, text, definition = self._window(task_id, source, window)
        if not isinstance(response, ExtractionResponse) or not response.complete:
            raise CatalogError("incomplete_extraction")
        records = [bind_evidence(candidate, window, entry, text, task_id, definition["template_id"]) for candidate in response.candidates]
        inserted = duplicates = 0
        with self._connect(write=True) as db:
            with db:
                self._lease(db, task_id, token)
                document = db.execute("SELECT status,processed_chars,last_batch FROM analysis_documents WHERE task_id=? AND source=?", (task_id, source)).fetchone()
                if not document or document[0] != "running":
                    raise CatalogError("document_not_running")
                prior = db.execute("SELECT window_digest FROM analysis_batches WHERE task_id=? AND source=? AND batch_index=?", (task_id, source, window.index)).fetchone()
                if prior:
                    if prior[0] != window.digest:
                        raise CatalogError("checkpoint_version_mismatch")
                    return {"inserted": 0, "duplicates": 0, "skipped": True}
                if document[1] != window.new_start_char or document[2] + 1 != window.index:
                    raise CatalogError("analysis_batch_gap")
                for record in records:
                    payload = record.model_dump(mode="json")
                    cursor = db.execute("INSERT OR IGNORE INTO analysis_evidence VALUES (?,?,?,?,'provisional',?,?,?)",
                                        (record.evidence_id, task_id, source, record.location.start_char, canonical(payload), fingerprint(payload), now()))
                    inserted += cursor.rowcount
                    duplicates += 1 - cursor.rowcount
                db.execute("INSERT INTO analysis_batches VALUES (?,?,?,?,?,?,?,?)",
                           (task_id, source, window.index, window.start_char, window.new_start_char, window.end_char, window.digest, now()))
                db.execute("UPDATE analysis_documents SET processed_chars=?,last_batch=? WHERE task_id=? AND source=?", (window.end_char, window.index, task_id, source))
        return {"inserted": inserted, "duplicates": duplicates, "skipped": False}

    def complete_document(self, task_id, source, token):
        _, definition, _, entries = self._context(task_id)
        if source not in entries:
            raise CatalogError("document_outside_scope")
        text = self.catalog.read_verified(entries[source])
        config = MapConfig(**definition["config"])
        with self._connect(write=True) as db:
            with db:
                self._lease(db, task_id, token)
                document = db.execute("SELECT status,processed_chars FROM analysis_documents WHERE task_id=? AND source=?", (task_id, source)).fetchone()
                rows = db.execute("SELECT batch_index,new_start_char,end_char FROM analysis_batches WHERE task_id=? AND source=? ORDER BY batch_index", (task_id, source)).fetchall()
                expected = [(i, start, min(start + config.batch_chars, len(text))) for i, start in enumerate(range(0, len(text), config.batch_chars))]
                if not document or document[0] != "running" or document[1] != len(text) or [tuple(row) for row in rows] != expected:
                    raise CatalogError("incomplete_document_analysis")
                db.execute("UPDATE analysis_documents SET status='completed',error_code=NULL WHERE task_id=? AND source=?", (task_id, source))
                db.execute("UPDATE analysis_evidence SET state='verified' WHERE task_id=? AND source=?", (task_id, source))

    def fail_document(self, task_id, source, token, code):
        # 清理只依赖运行租约；权限撤销后仍必须记录失败，不能泄漏或返回证据。
        with self._connect(write=True) as db:
            with db:
                self._lease(db, task_id, token)
                status = "version_changed" if code in VERSION_ERRORS else "failed"
                db.execute("UPDATE analysis_documents SET status=?,error_code=? WHERE task_id=? AND source=?", (status, code, task_id, source))
                db.execute("UPDATE analysis_evidence SET state='invalidated' WHERE task_id=? AND source=?", (task_id, source))

    def finish_task(self, task_id, token, *, status=None):
        if status not in (None, "cancelled", "interrupted"):
            raise CatalogError("invalid_task_finish_status")
        if status is None:
            # 后续文档分析期间，先前完成的来源/缓存也可能变化；任务成功前统一复查。
            with self._connect() as db:
                self._lease(db, task_id, token)
                completed = [row[0] for row in db.execute(
                    "SELECT source FROM analysis_documents WHERE task_id=? AND status='completed'", (task_id,))]
            try:
                _, _, _, entries = self._context(task_id)
            except CatalogError as exc:
                for source in completed:
                    self.fail_document(task_id, source, token, exc.code)
            else:
                for source in completed:
                    try:
                        if source not in entries:
                            raise CatalogError("document_outside_scope")
                        self.catalog.read_verified(entries[source])
                    except CatalogError as exc:
                        self.fail_document(task_id, source, token, exc.code)
        with self._connect(write=True) as db:
            with db:
                self._lease(db, task_id, token)
                counts = dict(db.execute("SELECT status,COUNT(*) FROM analysis_documents WHERE task_id=? GROUP BY status", (task_id,)).fetchall())
                if status is None:
                    if counts.get("completed", 0) == sum(counts.values()) and counts:
                        status = "completed"
                    elif counts.get("running") or counts.get("pending"):
                        status = "interrupted"
                    else:
                        status = "partial" if counts.get("completed") else "failed"
                db.execute("UPDATE analysis_documents SET status=? WHERE task_id=? AND status='running'", (status if status in {"cancelled", "interrupted"} else "interrupted", task_id))
                db.execute("UPDATE analysis_tasks SET status=?,run_token=NULL,updated_at=? WHERE task_id=?", (status, now(), task_id))
        return self._summary(task_id)

    def _decode_evidence(self, row):
        try:
            payload = json.loads(row["payload_json"])
            if fingerprint(payload) != row["payload_sha256"]:
                raise ValueError()
            record = EvidenceRecord.model_validate(payload)
            if record.evidence_id != row["evidence_id"] or record.task_id != row["task_id"] or record.source != row["source"]:
                raise ValueError()
            return {"evidence": record.model_dump(mode="json"), "state": row["state"]}
        except (ValueError, TypeError, ValidationError) as exc:
            raise CatalogError("evidence_integrity_error") from exc

    def list_evidence(self, task_id, *, source=None, document_id=None, include_unverified=False):
        _,_,_,entries=self._context(task_id)
        with self._connect() as db:
            rows = db.execute("""SELECT e.* FROM analysis_evidence e JOIN analysis_documents d
                ON e.task_id=d.task_id AND e.source=d.source WHERE e.task_id=?
                AND (? IS NULL OR e.source=?) AND (? IS NULL OR d.document_id=?)
                AND (? OR (e.state='verified' AND d.status='completed')) ORDER BY e.source,e.start_char,e.evidence_id""",
                              (task_id, source, source, document_id, document_id, bool(include_unverified))).fetchall()
        for row in rows:
            self.catalog.policy.require(row["source"])
            if row["source"] not in entries:
                raise CatalogError("document_outside_scope")
        return [self._decode_evidence(row) for row in rows]

    def get_evidence(self, evidence_id, *, include_unverified=False):
        if not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_id):
            raise CatalogError("invalid_evidence_id")
        with self._connect() as db:
            identity = db.execute("SELECT task_id,source FROM analysis_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
        if identity is None:
            return None
        _,_,_,entries=self._context(identity[0])
        self.catalog.policy.require(identity[1])
        if identity[1] not in entries:
            raise CatalogError("document_outside_scope")
        with self._connect() as db:
            row = db.execute("""SELECT e.* FROM analysis_evidence e JOIN analysis_documents d
                ON e.task_id=d.task_id AND e.source=d.source WHERE e.evidence_id=?
                AND (? OR (e.state='verified' AND d.status='completed'))""", (evidence_id, bool(include_unverified))).fetchone()
        return self._decode_evidence(row) if row else None

    def get_origin(self, evidence_id):
        result = self.get_evidence(evidence_id, include_unverified=True)
        if result is None:
            return None
        record = result["evidence"]
        _, definition, _, entries = self._context(record["task_id"])
        try:
            entry = entries[record["source"]]
            text = self.catalog.read_verified(entry)
            position = record["location"]
            if (record["document_sha256"] != entry.sha256
                    or text[position["start_char"]:position["end_char"]] != record["quote"]):
                raise CatalogError("stored_evidence_mismatch")
            version_status = "verified"
        except CatalogError as exc:
            version_status = exc.code
        return result | {"snapshot_id": definition["scope"]["snapshot_id"], "version_status": version_status}
