"""独立的关联/归纳结果库；增量检查点、版本门禁和原证据来源查询。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from app.document_analysis.catalog import CatalogError, canonical, fingerprint
from app.document_analysis.evidence_input import load_input
from app.document_analysis.evidence_store import now
from app.document_analysis.security import database_connection, database_relative, exclusive_analysis_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS synthesis_runs (
 run_id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, definition_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled','interrupted','version_changed')),
 run_token TEXT, result_json TEXT, result_sha256 TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS synthesis_items (
 run_id TEXT NOT NULL REFERENCES synthesis_runs(run_id), item_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,item_id)
);
"""
VALIDATION_KINDS = {"evidence_verification", "coverage_report", "analysis_workflow"}


def stage_input(evidence, task_ids, kind):
    if kind in VALIDATION_KINDS:
        from app.document_analysis.validation_input import load_validation_input, load_workflow_input
        return (load_workflow_input if kind == "analysis_workflow" else load_validation_input)(evidence,task_ids)
    return load_input(evidence,task_ids)


def evidence_references(value):
    """只收集显式证据引用，不把任意文本或实体 ID 当作来源。"""
    result = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids":
                if not isinstance(child, list) or any(not isinstance(item, str) for item in child):
                    raise CatalogError("invalid_evidence_references")
                result.update(child)
            else:
                result.update(evidence_references(child))
    elif isinstance(value, (tuple, list)):
        for child in value:
            result.update(evidence_references(child))
    return result


def check_verification_flags(value):
    if isinstance(value, dict):
        if (value.get("semantic_verified", False) is not False or value.get("exhaustive", False) is not False
                or value.get("source_location_verified", True) is not True):
            raise CatalogError("invalid_verification_claim")
        for child in value.values():
            check_verification_flags(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            check_verification_flags(child)


class SynthesisStore:
    def __init__(self, evidence, *, path=None):
        self.evidence, self.catalog = evidence, evidence.catalog
        self.path = Path(path or self.catalog.root / "KnowledgeBase/sqlite/evidence_synthesis.sqlite3").absolute()
        database_relative(self.catalog.root, self.path)
        if self.path in {evidence.path, self.catalog.parsed_db, self.catalog.snapshot_db}:
            raise CatalogError("synthesis_database_not_dedicated")

    @contextmanager
    def _connect(self, *, write=False, initialize=False):
        with database_connection(self.catalog.root, self.path, write=write, create=initialize,
                                 forbidden=self.evidence.path) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            if tables - {"synthesis_runs", "synthesis_items"} or db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise CatalogError("synthesis_database_not_dedicated")
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if initialize:
                db.executescript(SCHEMA + "\nPRAGMA user_version=1;")
            yield db

    def _raw(self, run_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM synthesis_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise CatalogError("synthesis_run_not_found")
        row = dict(row)
        try:
            definition = json.loads(row["definition_json"])
            if fingerprint(definition) != row["definition_sha256"] or row["run_id"] != fingerprint(definition):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise CatalogError("synthesis_integrity_error") from exc
        return row, definition

    def validate(self, run_id):
        row, definition = self._raw(run_id)
        data = stage_input(self.evidence, definition["task_ids"], definition["kind"])
        if data.digest != definition["input_digest"]:
            raise CatalogError("synthesis_input_changed")
        dependency = definition.get("dependency")
        for dependency in (dependency if isinstance(dependency,list) else [dependency] if dependency else []):
            parent, _ = self._raw(dependency["run_id"])
            if parent["status"] != "completed" or parent["result_sha256"] != dependency["result_sha256"]:
                raise CatalogError("synthesis_dependency_changed")
            self._payload(parent["result_json"], parent["result_sha256"])
        return row, definition, data

    def create(self, kind, data, config, *, backend=None, dependency=None):
        if kind not in {"entity_resolution", "evidence_reduce"} | VALIDATION_KINDS:
            raise CatalogError("invalid_synthesis_kind")
        if stage_input(self.evidence, data.task_ids,kind).digest != data.digest:
            raise CatalogError("synthesis_input_changed")
        definition = {"schema_version":1, "kind":kind, "task_ids":list(data.task_ids), "input_digest":data.digest,
                      "config":config, "backend":backend, "dependency":dependency}
        run_id = fingerprint(definition)
        with self._connect(write=True, initialize=True) as db:
            with db:
                db.execute("INSERT OR IGNORE INTO synthesis_runs VALUES (?,?,?,'pending',NULL,NULL,NULL,NULL,?,?)",
                           (run_id, canonical(definition), run_id, now(), now()))
        return {"run_id":run_id}

    @staticmethod
    def _lease(db, run_id, token):
        row = db.execute("SELECT status,run_token FROM synthesis_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row[0] != "running" or row[1] != token or not token:
            raise CatalogError("invalid_synthesis_lease")

    @contextmanager
    def run_guard(self, run_id):
        _,definition,_=self.validate(run_id)
        workflow=definition["kind"] == "analysis_workflow"
        lock = self.path.parent / (("workflow-run-" if workflow else "synthesis-run-") + fingerprint(str(self.path))[:16] + ".lock")
        with exclusive_analysis_lock(self.catalog.root, lock):
            token = uuid.uuid4().hex
            with self._connect(write=True) as db:
                with db:
                    running=db.execute("SELECT run_id,definition_json FROM synthesis_runs WHERE status='running'").fetchall()
                    for previous in running:
                        if (json.loads(previous[1])["kind"] == "analysis_workflow") == workflow:
                            db.execute("UPDATE synthesis_runs SET status='interrupted',run_token=NULL,updated_at=? WHERE run_id=?",(now(),previous[0]))
                    db.execute("UPDATE synthesis_runs SET status='running',run_token=?,result_json=NULL,result_sha256=NULL,error_code=NULL,updated_at=? WHERE run_id=?",
                               (token, now(), run_id))
            try:
                yield token
            finally:
                with self._connect(write=True) as db:
                    with db:
                        db.execute("UPDATE synthesis_runs SET status='interrupted',run_token=NULL,updated_at=? WHERE run_id=? AND status='running' AND run_token=?",
                                   (now(), run_id, token))

    @staticmethod
    def _payload(encoded, digest):
        try:
            value = json.loads(encoded)
            if fingerprint(value) != digest:
                raise ValueError()
            return value
        except (ValueError, TypeError) as exc:
            raise CatalogError("synthesis_integrity_error") from exc

    def item(self, run_id, item_id, token):
        with self._connect() as db:
            self._lease(db, run_id, token)
            row = db.execute("SELECT payload_json,payload_sha256 FROM synthesis_items WHERE run_id=? AND item_id=?", (run_id, item_id)).fetchone()
        return self._payload(*row) if row else None

    def checkpoint_items(self, run_id, prefix, *, token=None):
        """只读查询已保存进度；历史和运行中任务同样先检查当前权限及载荷完整性。"""
        _, definition = self._raw(run_id)
        for task_id in definition["task_ids"]:
            self.evidence.get_task(task_id)
        with self._connect() as db:
            if token is not None:
                self._lease(db, run_id, token)
            rows = db.execute("SELECT item_id,payload_json,payload_sha256,created_at FROM synthesis_items "
                              "WHERE run_id=? AND substr(item_id,1,?)=? ORDER BY created_at,item_id",
                              (run_id, len(prefix), prefix)).fetchall()
        return [{"item_id":row[0],"payload":self._payload(row[1],row[2]),"created_at":row[3]} for row in rows]

    def _check_result(self, run_id, payload):
        _, definition = self._raw(run_id)
        # 定义中的原始输入摘要已由运行会话校验；来源集合只能来自这些已授权任务。
        allowed = {item["evidence"]["evidence_id"] for task in definition["task_ids"]
                   for item in self.evidence.list_evidence(task,include_unverified=definition["kind"] in VALIDATION_KINDS)}
        if not evidence_references(payload) <= allowed:
            raise CatalogError("unknown_evidence_reference")
        check_verification_flags(payload)

    def save_item(self, run_id, item_id, token, payload):
        self._check_result(run_id, payload)
        with self._connect(write=True) as db:
            with db:
                self._lease(db, run_id, token)
                previous = db.execute("SELECT payload_sha256 FROM synthesis_items WHERE run_id=? AND item_id=?", (run_id, item_id)).fetchone()
                if previous and previous[0] != fingerprint(payload):
                    raise CatalogError("synthesis_checkpoint_conflict")
                db.execute("INSERT OR IGNORE INTO synthesis_items VALUES (?,?,?,?,?)",
                           (run_id, item_id, canonical(payload), fingerprint(payload), now()))

    def finish(self, run_id, token, *, result=None, status="completed", error=None):
        if status not in {"completed", "failed", "cancelled", "version_changed"}:
            raise CatalogError("invalid_synthesis_status")
        if status == "completed":
            self.validate(run_id)
            self._check_result(run_id, result)
            if not isinstance(result, dict):
                raise CatalogError("invalid_synthesis_result")
        else:
            result = None
        with self._connect(write=True) as db:
            with db:
                self._lease(db, run_id, token)
                db.execute("UPDATE synthesis_runs SET status=?,run_token=NULL,result_json=?,result_sha256=?,error_code=?,updated_at=? WHERE run_id=?",
                           (status, canonical(result) if result is not None else None,
                            fingerprint(result) if result is not None else None, error, now(), run_id))
        return {"run_id":run_id, "status":status, "result":result, "error_code":error}

    def get_run(self, run_id):
        row, definition, _ = self.validate(run_id)
        result = self._payload(row["result_json"], row["result_sha256"]) if row["status"] == "completed" else None
        if result and definition["kind"] == "analysis_workflow" and result.get("coverage_run_id"):
            self.validate(result["coverage_run_id"])
        return {"run_id":run_id, "status":row["status"], "result":result, "error_code":row["error_code"], "definition":definition}

    def read_historical(self,run_id):
        """专供验证的显式历史入口；校验权限与完整性，不承诺原文版本仍新鲜。"""
        row,definition=self._raw(run_id)
        for task_id in definition["task_ids"]:
            self.evidence.get_task(task_id)
        result=self._payload(row["result_json"],row["result_sha256"]) if row["status"]=="completed" else None
        return {"run_id":run_id,"status":row["status"],"result":result,"definition":definition,"historical":True}

    def get_origin(self, run_id, record_id):
        result = self.get_run(run_id)["result"]
        id_fields = {"entity_id", "relation_id", "claim_id", "statistic_id", "comparison_id", "timeline_id", "mention_id", "verification_id", "coverage_id"}
        def find(value):
            if isinstance(value, dict):
                if any(value.get(key) == record_id for key in id_fields) and "evidence_ids" in value:
                    return value["evidence_ids"]
                for child in value.values():
                    found = find(child)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for child in value:
                    found = find(child)
                    if found is not None:
                        return found
            return None
        ids = find(result)
        if ids is None:
            raise CatalogError("synthesis_record_not_found")
        return [self.evidence.get_origin(value) for value in sorted(set(ids))]
