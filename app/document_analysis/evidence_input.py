"""通过既有公开接口读取授权证据，固定派生分析输入及上游状态。"""
from dataclasses import dataclass
import re

from app.document_analysis.catalog import CatalogError, fingerprint
from app.document_analysis.evidence_schema import EvidenceRecord


@dataclass(frozen=True)
class AnalysisInput:
    task_ids: tuple[str, ...]
    tasks: tuple[dict, ...]
    documents: tuple[dict, ...]
    evidence: tuple[EvidenceRecord, ...]
    upstream_incomplete: bool
    root_id: str
    policy_digest: str
    source_location_verified: bool = True
    semantic_verified: bool = False

    @property
    def digest(self):
        return fingerprint({
            "tasks": self.tasks, "documents": self.documents,
            "evidence": [record.model_dump(mode="json") for record in self.evidence],
            "root_id": self.root_id, "policy_digest": self.policy_digest,
        })


def load_input(store, task_ids) -> AnalysisInput:
    if (not isinstance(task_ids, (list, tuple)) or not 1 <= len(task_ids) <= 32
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", value) for value in task_ids)):
        raise CatalogError("invalid_input_tasks")
    task_ids = tuple(sorted(set(task_ids)))
    tasks, documents, records = [], [], {}
    for task_id in task_ids:
        task = store.get_task(task_id)
        tasks.append(task)
        scope = task["definition"]["scope"]
        entries = {entry.source: entry for entry in store.catalog.get_snapshot(scope["snapshot_id"]).documents}
        for source in scope["sources"]:
            document = store.get_document(task_id, source)
            if document is None:
                raise CatalogError("analysis_input_integrity_error")
            documents.append(document)
            # 没提取到证据的已完成文档，也必须核对来源和缓存版本。
            if document["status"] == "completed":
                store.catalog.read_verified(entries[source])
        for item in store.list_evidence(task_id):
            record = EvidenceRecord.model_validate(item["evidence"])
            origin = store.get_origin(record.evidence_id)
            if not origin or origin["state"] != "verified":
                raise CatalogError("analysis_input_changed")
            if origin["version_status"] != "verified":
                raise CatalogError(origin["version_status"])
            if fingerprint(origin["evidence"]) != fingerprint(item["evidence"]):
                raise CatalogError("analysis_input_changed")
            records[record.evidence_id] = record
        if store.get_task(task_id) != task:
            raise CatalogError("analysis_input_changed")
        after = [item["evidence"] for item in store.list_evidence(task_id)]
        before = [record.model_dump(mode="json") for record in records.values() if record.task_id == task_id]
        if fingerprint(sorted(after, key=lambda value: value["evidence_id"])) != fingerprint(sorted(before, key=lambda value: value["evidence_id"])):
            raise CatalogError("analysis_input_changed")
    return AnalysisInput(task_ids, tuple(tasks), tuple(documents),
                         tuple(records[key] for key in sorted(records)),
                         any(task["status"] != "completed" for task in tasks),
                         store.catalog.root_id, store.catalog.policy.digest)
