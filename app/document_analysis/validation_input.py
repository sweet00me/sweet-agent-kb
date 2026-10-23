"""验证输入：保存版本观察而非吞掉失败；权限失败仍直接拒绝。"""
from dataclasses import asdict, dataclass
import re

from app.document_analysis.catalog import CatalogError, DocumentRecord, MAX_CACHE_BYTES, fingerprint
from app.document_analysis.evidence_schema import EvidenceRecord
from app.document_analysis.security import read_checked


@dataclass(frozen=True)
class ValidationInput:
    task_ids: tuple
    tasks: tuple
    documents: tuple
    evidence: tuple
    evidence_states: dict
    snapshots: tuple
    root_id: str
    policy_digest: str

    @property
    def digest(self):
        return fingerprint({"tasks":self.tasks,"documents":self.documents,
            "evidence":[record.model_dump(mode="json") for record in self.evidence],
            "states":self.evidence_states,"snapshots":self.snapshots,
            "root":self.root_id,"policy":self.policy_digest})


@dataclass(frozen=True)
class WorkflowInput:
    task_ids: tuple
    definitions: tuple
    root_id: str
    policy_digest: str

    @property
    def digest(self):
        return fingerprint(asdict(self))


def _tasks(store,task_ids):
    if (not isinstance(task_ids,(tuple,list)) or not 1<=len(task_ids)<=32
            or any(not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}",value) for value in task_ids)):
        raise CatalogError("invalid_input_tasks")
    return tuple(store.get_task(value) for value in sorted(set(task_ids)))


def load_workflow_input(store,task_ids):
    tasks=_tasks(store,task_ids)
    return WorkflowInput(tuple(task["task_id"] for task in tasks),
                         tuple(task["definition"] for task in tasks),store.catalog.root_id,store.catalog.policy.digest)


def _version(catalog,entry):
    catalog.policy.require(entry.source)
    source_sha=cache_sha=None
    try:
        source_sha=catalog.source_state(entry.source)[0]
    except CatalogError:
        pass
    if entry.cache_path:
        try:
            cache_sha=read_checked(catalog.parsed_dir,entry.cache_path,MAX_CACHE_BYTES)[0]
        except CatalogError:
            pass
    try:
        catalog.read_verified(entry)
        status,code="valid",None
    except CatalogError as exc:
        if exc.code in {"access_denied","snapshot_policy_changed"}:
            raise
        status,code="invalid",exc.code
    return {"status":status,"code":code,"source_sha256":source_sha,"cache_sha256":cache_sha}


def load_validation_input(store,task_ids):
    tasks=_tasks(store,task_ids)
    documents,records,states,snapshots=[],{}, {},{}
    policy=store.catalog.policy.digest
    for task in tasks:
        scope=task["definition"]["scope"]
        snapshot=store.catalog.get_snapshot(scope["snapshot_id"])
        snapshots[snapshot.snapshot_id]={"snapshot_id":snapshot.snapshot_id,"sources":[entry.source for entry in snapshot.documents],
            "policy_excluded":snapshot.excluded_count,"invalid_rows":snapshot.invalid_count,"undated":snapshot.undated_count}
        entries={entry.source:entry for entry in snapshot.documents}
        for source in scope["sources"]:
            entry=entries[source]
            documents.append({"task_id":task["task_id"],"source":source,"entry":asdict(entry),
                "processing":store.get_document(task["task_id"],source),
                "batches":store.get_batches(task["task_id"],source),"version":_version(store.catalog,entry)})
        for item in store.list_evidence(task["task_id"],include_unverified=True):
            record=EvidenceRecord.model_validate(item["evidence"])
            records[record.evidence_id]=record
            states[record.evidence_id]=item["state"]
        if store.get_task(task["task_id"])!=task:
            raise CatalogError("analysis_input_changed")
    # 后续文件的读取可能跨越早期文件的修改；不发布混合时刻的输入观察。
    for document in documents:
        if _version(store.catalog,DocumentRecord(**document["entry"]))!=document["version"]:
            raise CatalogError("analysis_input_changed")
    if store.catalog.policy.digest!=policy:
        raise CatalogError("snapshot_policy_changed")
    return ValidationInput(tuple(task["task_id"] for task in tasks),tasks,tuple(documents),
        tuple(records[key] for key in sorted(records)),states,tuple(snapshots[key] for key in sorted(snapshots)),
        store.catalog.root_id,policy)
