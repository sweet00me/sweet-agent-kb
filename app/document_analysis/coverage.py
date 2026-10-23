"""固定 Scope 的处理与验证覆盖率；不将 100% 字符覆盖当作事实穷尽。"""
from collections import Counter, defaultdict

from app.document_analysis.catalog import CatalogError, DEFAULT_EXCLUDED, DocumentRecord, fingerprint
from app.document_analysis.evidence_schema import ExtractionWindow, MapConfig
from app.document_analysis.evidence_verification import collect_targets, has_semantic_verdict, verification_mode
from app.document_analysis.synthesis_runtime import execute_stage
from app.document_analysis.validation_input import load_validation_input


def _progress(store,document,task):
    row=document["processing"]
    if row is None: return 0,["missing_processing_record"]
    if document["version"]["status"]!="valid": return 0,[document["version"]["code"]]
    entry=DocumentRecord(**document["entry"])
    body=store.catalog.read_verified(entry)
    config=MapConfig(**task["definition"]["config"])
    cursor=0
    for index,batch in enumerate(document["batches"]):
        end=min(cursor+config.batch_chars,len(body)); start=max(0,cursor-config.overlap_chars)
        if (cursor>=len(body) or batch["batch_index"]!=index or batch["new_start_char"]!=cursor
                or batch["start_char"]!=start or batch["end_char"]!=end):
            return cursor,["non_contiguous_batches"]
        window=ExtractionWindow(entry.source,entry.document_id,entry.sha256,index,start,cursor,end,len(body),body[start:end])
        if window.digest!=batch["window_digest"]:
            return cursor,["checkpoint_version_mismatch"]
        cursor=end
    if row["processed_chars"]!=cursor or row["last_batch"]!=len(document["batches"])-1:
        return cursor,["processing_cursor_mismatch"]
    if row["status"]=="completed" and cursor!=len(body):
        return cursor,["incomplete_document_analysis"]
    return cursor,[]


def _pipeline_complete(task_ids,producers):
    """必须存在覆盖整个任务集合、彼此匹配的 Entity → Reduce 链。"""
    full={key:value for key,value in producers.items() if set(value["definition"]["task_ids"])==set(task_ids)}
    entities={key:value for key,value in full.items() if value["definition"]["kind"]=="entity_resolution"}
    for value in full.values():
        dependency=value["definition"].get("dependency")
        if value["definition"]["kind"]!="evidence_reduce" or not isinstance(dependency,dict):
            continue
        parent=entities.get(dependency.get("run_id"))
        if parent and dependency.get("result_sha256")==fingerprint(parent["result"]):
            return True
    return False


class CoverageReporter:
    def __init__(self,store): self.store=store

    def run(self,task_ids,*,verification_run_id=None,derived_run_ids=(),cancel=None):
        data=load_validation_input(self.store.evidence,task_ids)
        targets,dependencies,producers=collect_targets(self.store,data,derived_run_ids)
        verification=None; verification_error=None
        if verification_run_id:
            raw=self.store.read_historical(verification_run_id)
            if raw["definition"]["kind"]!="evidence_verification" or set(raw["definition"]["task_ids"])!=set(data.task_ids):
                raise CatalogError("verification_scope_mismatch")
            try:
                verification=self.store.get_run(verification_run_id)
            except CatalogError:
                verification_error="stale_verification"
            if raw["status"]=="completed":
                dependencies.append({"run_id":verification_run_id,"result_sha256":fingerprint(raw["result"])})
        def build(session):
            session.check_cancel()
            task_by_id={task["task_id"]:task for task in data.tasks}
            selected=defaultdict(list)
            for document in data.documents: selected[document["source"]].append(document)
            counts={"expected":len(selected),"analyzed":0,"failed":0,"missing":0,"pending":0}
            expected_chars=committed=verified_chars=0; unknown_chars=False; details=[]
            uncertainties=set()
            for source,documents in sorted(selected.items()):
                reasons=[]; progress=[]
                lengths={document["entry"]["content_chars"] for document in documents}
                versions={document["entry"]["sha256"] for document in documents}
                if len(versions)!=1: reasons.append("multiple_expected_versions")
                for document in documents:
                    amount,issues=_progress(self.store,document,task_by_id[document["task_id"]]); progress.append(amount); reasons.extend(issues)
                rows=[document["processing"] for document in documents]
                missing=any(row is None for row in rows) or "source_missing" in reasons
                failure=bool(reasons) or any(row and row["status"] in {"failed","version_changed"} for row in rows)
                complete=not failure and not missing and all(row["status"]=="completed" for row in rows)
                state="missing" if missing else "failed" if failure else "analyzed" if complete else "pending"
                counts[state]+=1
                length=next(iter(lengths)) if len(lengths)==1 else None
                if length is None: unknown_chars=True
                else: expected_chars+=length
                amount=min(progress) if progress else 0
                committed+=amount; verified_chars+=length if complete and length is not None else 0
                details.append({"source":source,"status":state,"expected_characters":length,
                    "committed_characters":amount,"errors":sorted(set(reasons))})
            expected={target.target_id:target for target in targets}
            checks=verification["result"]["checks"] if verification and verification["status"]=="completed" else []
            frequencies=Counter(check["verification_id"] for check in checks)
            by_id={check["verification_id"]:check for check in checks if check["verification_id"] in expected and frequencies[check["verification_id"]]==1}
            if len(by_id)!=len(checks): uncertainties.add("invalid_or_duplicate_verification_targets")
            stats={"expected_targets":len(expected),"checked_targets":0,"missing_targets":0,"valid_citations":0,
                   "invalid_citations":0,"supported":0,"partially_supported":0,"contradicted":0,"unsupported":0,"not_checked":0,
                   "semantic_targets":0,"semantic_completed":0,"semantic_supported":0,"structural_targets":0,"calculation_targets":0}
            by_kind={kind:{key:0 for key in stats} for kind in sorted({target.target_kind for target in targets})}
            for key,target in expected.items():
                kind_stats=by_kind[target.target_kind]; kind_stats["expected_targets"]+=1
                check=by_id.get(key)
                mode=check.get("verification_mode", "calculation" if target.target_kind=="statistic" else "semantic") if check else verification_mode(target)
                field=mode+"_targets"
                stats[field]+=1; kind_stats[field]+=1
                if check is None or set(check["declared_evidence_ids"])!=set(target.evidence_ids):
                    stats["missing_targets"]+=1; kind_stats["missing_targets"]+=1; continue
                stats["checked_targets"]+=1
                kind_stats["checked_targets"]+=1
                valid=check["citation_status"]=="valid"
                stats["valid_citations" if valid else "invalid_citations"]+=1
                kind_stats["valid_citations" if valid else "invalid_citations"]+=1
                stats[check["semantic_status"]]+=1
                kind_stats[check["semantic_status"]]+=1
                if mode=="semantic" and has_semantic_verdict(check):
                    stats["semantic_completed"]+=1; kind_stats["semantic_completed"]+=1
                    if check["semantic_status"]=="supported":
                        stats["semantic_supported"]+=1; kind_stats["semantic_supported"]+=1
            pipeline_complete=_pipeline_complete(data.task_ids,producers)
            limitations=[{"verification_id":check["verification_id"],"evidence_ids":check["evidence_ids"],
                "limitations":check["limitations"]} for check in by_id.values() if check.get("limitations")]
            if limitations: uncertainties.add("semantic_validation_limitations")
            blocked={"upstream_incomplete","identity_conflict","unresolved_identity","potential_evidence_conflict","no_available_evidence"}
            for producer in producers.values():
                uncertainties.update(set(producer["result"].get("uncertainties",[]))&blocked)
                if producer["result"].get("undated"): uncertainties.add("unknown_or_ambiguous_time")
            if any(task["status"]!="completed" for task in data.tasks): uncertainties.add("upstream_incomplete")
            if stats["missing_targets"] or stats["semantic_completed"]<stats["semantic_targets"]: uncertainties.add("verification_incomplete")
            if stats["partially_supported"] or stats["contradicted"] or stats["unsupported"] or stats["invalid_citations"]:
                uncertainties.add("unsupported_or_conflicting_conclusions")
            if verification_error: uncertainties.add(verification_error)
            if not pipeline_complete: uncertainties.add("pipeline_stages_missing")
            if not data.evidence: uncertainties.add("no_available_evidence")
            if any(snapshot["invalid_rows"] or snapshot["undated"] for snapshot in data.snapshots): uncertainties.add("catalog_inventory_uncertain")
            document_complete=bool(selected) and counts["analyzed"]==counts["expected"]
            verified_complete=bool(expected) and stats["checked_targets"]==len(expected) and not stats["missing_targets"] and stats["semantic_completed"]==stats["semantic_targets"]
            all_supported=bool(expected) and stats["semantic_supported"]==stats["semantic_targets"] and stats["supported"]==stats["semantic_targets"]+stats["calculation_targets"] and stats["valid_citations"]==len(expected)
            analysis_complete=document_complete and all_supported and pipeline_complete and not uncertainties
            excluded_sources=set().union(*(set(snapshot["sources"]) for snapshot in data.snapshots))-set(selected)
            catalog_excluded=max((snapshot["policy_excluded"] for snapshot in data.snapshots),default=0)
            body_expected=None if unknown_chars else expected_chars
            return {"coverage_id":fingerprint([data.digest,verification_run_id,sorted(derived_run_ids)]),
                "documents":counts,"document_details":details,"exclusions":{"known_catalog_excluded":catalog_excluded+len(excluded_sources),
                    "basis":"fixed_catalog_snapshot_known_counts","count_is_lower_bound":len(data.snapshots)>1,
                    "disk_inventory_complete":False,"enforced_paths":[DEFAULT_EXCLUDED]},
                "body":{"expected_characters":body_expected,"committed_characters":committed,"confirmed_complete_characters":verified_chars,
                    "processing_ratio":committed/body_expected if body_expected else None,
                    "completion_ratio":verified_chars/body_expected if body_expected else None},
                "verification":stats,"document_processing_complete":document_complete,"verification_complete":verified_complete,
                "verification_by_kind":by_kind,
                "all_targets_supported":all_supported,"semantic_limitations":limitations,
                "pipeline_complete":pipeline_complete,"analysis_complete":analysis_complete,"uncertainties":sorted(uncertainties),
                "evidence_ids":[record.evidence_id for record in data.evidence],"facts_exhaustive":False,"real_world_truth":"not_checked"}
        return execute_stage(self.store,"coverage_report",data,
            {"algorithm":"coverage-v4","verification_run_id":verification_run_id,"derived_run_ids":sorted(set(derived_run_ids))},
            None,build,dependency=dependencies,cancel=cancel)
