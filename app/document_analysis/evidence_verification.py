"""逐目标核对 Map、关联与归纳；结果持久化，语义验证可注入且默认不调用。"""
from dataclasses import dataclass
from copy import deepcopy
from collections import Counter
from pydantic import ValidationError

from app.document_analysis.catalog import CatalogError, DocumentRecord, fingerprint
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionWindow, bind_evidence
from app.document_analysis.evidence_input import load_input
from app.document_analysis.evidence_reduce import _base_result
from app.document_analysis.entity_resolution import _group_entities, candidate_pairs, derive_mentions, rule_relation
from app.document_analysis.synthesis_schema import EntityRelation
from app.document_analysis.synthesis_runtime import CloudCallBudget, CloudCallLimits, BudgetedBackend, budget_backend, execute_stage
from app.document_analysis.validation_input import load_validation_input
from app.document_analysis.verification_schema import SemanticDecision, VerificationRequest


@dataclass(frozen=True)
class VerificationTarget:
    target_id: str
    target_kind: str
    producer_id: str
    object_id: str
    payload: dict
    evidence_ids: tuple
    producer_fresh: bool = True


def verification_mode(target):
    if target.target_kind == "statistic":
        return "calculation"
    if target.target_kind.startswith("entity_") or (target.target_kind == "comparison"
            and len(target.payload.get("alternatives", [])) < 2):
        return "structural"
    return "semantic"


def has_semantic_verdict(check):
    return check["support_basis"]=="semantic_validator" and check["semantic_status"] in {
        "supported","partially_supported","contradicted","unsupported"}


def collect_targets(store,data,derived_run_ids):
    targets=[]; dependencies=[]; producers={}
    for record in data.evidence:
        targets.append(VerificationTarget(fingerprint(["map",record.evidence_id]),"map_evidence",record.task_id,
            record.evidence_id,{"conclusion":record.conclusion,"details":record.details},(record.evidence_id,)))
    for run_id in sorted(set(derived_run_ids)):
        run=store.read_historical(run_id)
        if not set(run["definition"]["task_ids"])<=set(data.task_ids):
            raise CatalogError("derived_task_scope_mismatch")
        if run["status"]!="completed" or run["definition"]["kind"] not in {"entity_resolution","evidence_reduce"}:
            raise CatalogError("derived_analysis_not_completed")
        try:
            store.get_run(run_id); fresh=True
        except CatalogError:
            fresh=False
        producers[run_id]=run
        dependencies.append({"run_id":run_id,"result_sha256":fingerprint(run["result"])})
        groups=(
            ("mentions","entity_mention","mention_id"),("relations","entity_relation","relation_id"),
            ("entities","entity_group","entity_id"),("claims","claim","claim_id"),
            ("comparisons","comparison","comparison_id"),("timeline","timeline","timeline_id"),
            ("undated","timeline","timeline_id"),("statistics","statistic","statistic_id"),
            ("semantic_conclusions","semantic_summary","claim_id"),
        )
        for field,kind,key in groups:
            for value in run["result"].get(field,[]):
                if not isinstance(value,dict) or not isinstance(value.get(key),str) or not isinstance(value.get("evidence_ids"),list):
                    raise CatalogError("invalid_derived_target")
                targets.append(VerificationTarget(fingerprint([run_id,kind,value[key]]),kind,run_id,value[key],value,
                                                  tuple(value["evidence_ids"]),fresh))
    return tuple(sorted(targets,key=lambda value:value.target_id)),dependencies,producers


def _check_record(store,data,record):
    store.catalog.policy.require(record.source)
    document=next((item for item in data.documents if item["task_id"]==record.task_id and item["source"]==record.source),None)
    if document is None:
        return False,["document_outside_scope"]
    if document["version"]["status"]!="valid":
        return False,[document["version"]["code"]]
    try:
        entry=DocumentRecord(**document["entry"])
        body=store.catalog.read_verified(entry)
        start,end=record.location.start_char,record.location.end_char
        window=ExtractionWindow(entry.source,entry.document_id,entry.sha256,0,start,start,end,len(body),body[start:end])
        candidate=CandidateEvidence(quote=record.quote,conclusion=record.conclusion,uncertainty=record.uncertainty,
            details=record.details,start_char=0,end_char=len(record.quote))
        bound=bind_evidence(candidate,window,entry,body,record.task_id,record.template_id)
        if bound!=record:
            raise CatalogError("evidence_metadata_or_position_mismatch")
        errors=[]
        if data.evidence_states[record.evidence_id]!="verified" or not document["processing"] or document["processing"]["status"]!="completed":
            errors.append("map_analysis_not_completed")
        return True,errors
    except (CatalogError,ValidationError,ValueError) as exc:
        return False,[exc.code if isinstance(exc,CatalogError) else "invalid_evidence_record"]


def _python_statistic(store,data,target,producers,cache):
    producer=producers[target.producer_id]
    dependency=producer["definition"].get("dependency")
    if not isinstance(dependency,dict):
        return False
    if target.producer_id not in cache:
        parent=store.read_historical(dependency["run_id"])
        analysis=load_input(store.evidence,producer["definition"]["task_ids"])
        cache[target.producer_id]=_base_result(analysis,parent["result"])
    field="statistics" if target.target_kind=="statistic" else "comparisons"
    return any(value==target.payload for value in cache[target.producer_id][field])


def _entity_references(data,producer):
    """核对内部提及引用，并给语义验证器提供 ID 对应的名称与依据。"""
    result=producer["result"]; task_ids=set(producer["definition"]["task_ids"])
    records={record.evidence_id:record for record in data.evidence
        if record.task_id in task_ids and data.evidence_states[record.evidence_id]=="verified"}
    expected={item.mention_id:item for item in derive_mentions(records.values())}
    mentions={item["mention_id"]:item for item in result["mentions"]}
    global_errors=[]; errors={}; contexts={}
    if (len(mentions)!=len(result["mentions"]) or mentions!={key:item.model_dump(mode="json") for key,item in expected.items()}):
        global_errors.append("entity_mentions_mismatch")
    pairs=set()
    for relation in result["relations"]:
        key=relation["relation_id"]; left,right=relation["left"],relation["right"]
        issues=[]
        if left not in expected or right not in expected or left==right or expected[left].kind!=expected[right].kind:
            issues.append("invalid_entity_reference")
        else:
            pair=tuple(sorted((left,right)))
            if pair in pairs or key!=fingerprint(list(pair)): issues.append("invalid_entity_reference")
            pairs.add(pair)
            if not set(expected[left].evidence_ids+expected[right].evidence_ids)<=set(relation["evidence_ids"]):
                issues.append("invalid_entity_reference")
            if relation["automatic_merge"]:
                rule=rule_relation(expected[left],expected[right],records)
                if not rule.automatic_merge or relation["relation"]!="same" or relation["confidence_status"]!="rule_supported":
                    issues.append("unsupported_automatic_merge")
            if relation["confidence_status"]=="rule_supported":
                rule=rule_relation(expected[left],expected[right],records)
                if relation["relation"]!=rule.relation or relation["automatic_merge"]!=rule.automatic_merge:
                    issues.append("unsupported_identity_rule")
            contexts[key]={"mentions":[mentions.get(left,{}),mentions.get(right,{})]}
        errors[key]=issues
    if producer["definition"]["config"]["algorithm"]=="entity-rules-v2":
        required={tuple(sorted((left.mention_id,right.mention_id)))
            for left,right in candidate_pairs(expected.values(),records)}
        complete=pairs==required
    else:
        kinds=Counter(item.kind for item in expected.values())
        complete=len(pairs)==sum(count*(count-1)//2 for count in kinds.values())
    if not complete or len(pairs)!=len(result["relations"]):
        global_errors.append("incomplete_entity_relations")
    membership=Counter()
    for group in result["entities"]:
        keys=group["mention_ids"]; issues=[]; membership.update(keys)
        if not keys or len(set(keys))!=len(keys) or not set(keys)<=set(expected):
            issues.append("invalid_entity_reference")
        else:
            members=[expected[key] for key in keys]
            if (group["entity_id"]!=fingerprint(sorted(keys)) or group["names"]!=sorted({item.label for item in members})
                    or set(group["evidence_ids"])!={value for item in members for value in item.evidence_ids}
                    or any(item.kind!=group["kind"] for item in members)):
                issues.append("invalid_entity_reference")
            contexts[group["entity_id"]]={"mentions":[mentions.get(key,{}) for key in keys]}
        errors[group["entity_id"]]=issues
    if membership!=Counter({key:1 for key in expected}): global_errors.append("invalid_entity_reference")
    if not global_errors and not any(errors.values()):
        # 结构有效的成员列表仍不能证明可以合并；用 Python 重放合并和冲突保护。
        original=[]
        for value in result["relations"]:
            relation=EntityRelation.model_validate(value)
            if "inconsistent_same_component" in relation.rationale:
                relation=rule_relation(expected[relation.left],expected[relation.right],records)
            original.append(relation)
        groups,_=_group_entities(list(expected.values()),original)
        if sorted(result["entities"],key=lambda row:row["entity_id"])!=groups:
            global_errors.append("entity_groups_mismatch")
    return global_errors,errors,contexts


class EvidenceVerifier:
    def __init__(self,store,validator=None,*,cloud_limits=None):
        self.store,self.validator=store,validator
        self.cloud_limits=cloud_limits or (validator.budget.limits if isinstance(validator,BudgetedBackend) else CloudCallLimits.from_env())

    def progress(self,run_id):
        run=self.store.read_historical(run_id)
        if run["definition"]["kind"]!="evidence_verification": raise CatalogError("not_verification_run")
        plans=self.store.checkpoint_items(run_id,"verification_plan")
        plan=plans[0]["payload"] if plans else {}
        checks=[row["payload"] for row in self.store.checkpoint_items(run_id,"verify:")]
        def mode(row): return row.get("verification_mode","calculation" if row["target_kind"]=="statistic" else "semantic")
        budget_id=plan.get("budget_run_id",run_id)
        if budget_id!=run_id:
            owner=self.store.read_historical(budget_id)
            if set(owner["definition"]["task_ids"])!=set(run["definition"]["task_ids"]):
                raise CatalogError("verification_scope_mismatch")
        return {"run_id":run_id,"total_targets":plan.get("total_targets",len(checks)),
            "completed_targets":sum(mode(row)!="semantic" or has_semantic_verdict(row) for row in checks),"processed_targets":len(checks),
            "semantic_targets":plan.get("semantic_targets",sum(mode(row)=="semantic" for row in checks)),
            "semantic_completed":sum(mode(row)=="semantic" and has_semantic_verdict(row) for row in checks),
            "structural_targets":plan.get("structural_targets",sum(mode(row)=="structural" for row in checks)),
            "calculation_targets":plan.get("calculation_targets",sum(mode(row)=="calculation" for row in checks)),
            "confirmed_completed_semantic_calls":sum(row["support_basis"]=="semantic_validator" for row in checks)
                if (run["definition"].get("backend") or {}).get("provider")=="deepseek" else 0,
            "model_calls":CloudCallBudget.stats(self.store,budget_id)}

    def run(self,task_ids,*,derived_run_ids=(),cancel=None,on_progress=None):
        if not isinstance(derived_run_ids,(tuple,list)) or len(derived_run_ids)>64:
            raise CatalogError("invalid_derived_runs")
        data=load_validation_input(self.store.evidence,task_ids)
        targets,dependencies,producers=collect_targets(self.store,data,derived_run_ids)
        records={record.evidence_id:record for record in data.evidence}
        def build(session):
            budget=self.validator.budget if isinstance(self.validator,BudgetedBackend) else CloudCallBudget(
                self.store,session.run_id,session.token,self.cloud_limits)
            validator=budget_backend(self.validator,budget,"evidence_verification")
            modes=Counter(verification_mode(target) for target in targets)
            plan={"total_targets":len(targets),"semantic_targets":modes["semantic"],"structural_targets":modes["structural"],
                "calculation_targets":modes["calculation"],"budget_run_id":budget.run_id}
            saved_plan=session.checkpoint("verification_plan",lambda:plan)
            if saved_plan!=plan:
                raise CatalogError("analysis_budget_owner_changed")
            if on_progress: on_progress({"run_id":session.run_id,**plan})
            if len(targets)>self.cloud_limits.max_verification_targets:
                raise CatalogError("verification_target_limit")
            record_checks={key:_check_record(self.store,data,value) for key,value in records.items()}
            entity_checks={key:_entity_references(data,value) for key,value in producers.items()
                if value["definition"]["kind"]=="entity_resolution"}
            checks=[]
            aggregate_cache={}
            for target in targets:
                def verify():
                    known=sorted(set(target.evidence_ids)&set(records))
                    errors=[]
                    if not target.evidence_ids: errors.append("missing_evidence_reference")
                    if len(known)!=len(set(target.evidence_ids)): errors.append("unknown_evidence_id")
                    if not target.producer_fresh: errors.append("producer_input_changed")
                    if target.producer_id in producers:
                        producer_tasks=set(producers[target.producer_id]["definition"]["task_ids"])
                        if any(records[key].task_id not in producer_tasks for key in known):
                            errors.append("evidence_outside_producer_scope")
                    context=None
                    if target.producer_id in entity_checks:
                        common,specific,contexts=entity_checks[target.producer_id]
                        errors.extend(common); errors.extend(specific.get(target.object_id,[]))
                        context=contexts.get(target.object_id)
                    position_valid=bool(known)
                    for key in known:
                        valid,issues=record_checks[key]; position_valid &= valid; errors.extend(issues)
                    citation_valid=position_valid and not errors
                    status="not_checked" if citation_valid else "unsupported"
                    basis="not_checked" if citation_valid else "structural_failure"
                    rationale="原文位置与元数据核对，不代表结论已得到原文语义支持。"
                    limitations=[]
                    mode=verification_mode(target)
                    if citation_valid and target.target_kind in {"statistic","comparison"}:
                        if _python_statistic(self.store,data,target,producers,aggregate_cache):
                            if mode=="calculation":
                                status,basis="supported","python_recalculation"
                            else:
                                basis="python_aggregation"
                            rationale="Python 对当前证据集合重新计算，数值与范围限定一致；不表示事实穷尽或语义支持。"
                        else:
                            errors.append("python_recalculation_mismatch")
                            status,basis="unsupported","python_recalculation"
                            citation_valid=False
                    if citation_valid and mode=="structural":
                        basis="python_identity_rules" if target.target_kind.startswith("entity_") else "python_aggregation"
                        rationale="结构、来源及确定性规则已检查；未进行结论语义核验，不代表实体身份或现实事实真实。"
                    elif citation_valid and mode=="semantic" and validator is not None:
                        session.before_model(validator,"evidence_verification")
                        proposition=deepcopy(target.payload)
                        if context is not None: proposition["reference_context"]=deepcopy(context)
                        request=VerificationRequest(target.target_id,target.target_kind,
                            proposition,
                            tuple(records[key].model_copy(deep=True) for key in known))
                        decision=SemanticDecision.model_validate(validator.verify(request))
                        if not decision.complete:
                            raise CatalogError("incomplete_verification_response")
                        if {item.evidence_id for item in decision.evidence}!=set(known):
                            raise CatalogError("invalid_semantic_evidence_ids")
                        if any(item.quote not in records[item.evidence_id].quote for item in decision.evidence):
                            raise CatalogError("fabricated_semantic_quote")
                        status,basis,rationale=decision.status,"semantic_validator",decision.rationale
                        limitations=list(decision.limitations)
                    return {"verification_id":target.target_id,"target_kind":target.target_kind,"producer_id":target.producer_id,
                        "object_id":target.object_id,"declared_evidence_ids":list(target.evidence_ids),"evidence_ids":known,
                        "position_status":"valid" if position_valid else "invalid",
                        "citation_status":"valid" if citation_valid else "invalid","semantic_status":status,
                        "verification_mode":mode,"semantic_required":mode=="semantic",
                        "support_basis":basis,"rationale":rationale,"real_world_truth":"not_checked",
                        "limitations":limitations,
                        "errors":sorted(set(errors)),"conclusion":target.payload.get("conclusion",target.payload.get("text",""))}
                checks.append(session.checkpoint("verify:"+target.target_id,verify))
                if on_progress:
                    on_progress({"run_id":session.run_id,**plan,
                        "completed_targets":sum(row["verification_mode"]!="semantic" or has_semantic_verdict(row) for row in checks),
                        "semantic_completed":sum(row["verification_mode"]=="semantic" and has_semantic_verdict(row) for row in checks)})
            return {"checks":checks,"evidence_ids":sorted(records),"derived_run_ids":sorted(set(derived_run_ids)),
                "target_count":len(targets),"real_world_truth":"not_checked","fact_exhaustiveness":"not_established",
                "progress":self.progress(session.run_id)}
        response=execute_stage(self.store,"evidence_verification",data,
            {"algorithm":"evidence-verification-v4","derived_run_ids":sorted(set(derived_run_ids))},
            self.validator,build,dependency=dependencies,cancel=cancel)
        return response | {"progress":self.progress(response["run_id"])}
