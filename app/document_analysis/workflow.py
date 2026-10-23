"""受控工具调用的完整分析工作流；复用所有阶段的事务检查点。"""
import os
import re

from app.document_analysis.catalog import CatalogError, fingerprint
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.evidence_map import EvidenceMap, require_deepseek_authorization
from app.document_analysis.evidence_schema import MapConfig, get_template
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.synthesis_runtime import AnalysisCancelled, CloudCallBudget, CloudCallLimits, authorize_backend, budget_backend
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_reduce import EvidenceReducer
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.coverage import CoverageReporter
from app.document_analysis.validation_input import load_workflow_input


OUTPUT_FIELDS={"claims":("claim_id","claim"),"semantic_conclusions":("claim_id","semantic_summary"),
    "statistics":("statistic_id","statistic"),"comparisons":("comparison_id","comparison"),
    "timeline":("timeline_id","timeline"),"undated":("timeline_id","timeline")}


def supported_output(kind,value,check):
    """发布门禁：来源位置正确与结论语义支持必须分别成立。"""
    key,target_kind=OUTPUT_FIELDS[kind]
    evidence=set(value.get("evidence_ids",[]))
    if (check.get("target_kind")!=target_kind or check.get("object_id")!=value.get(key)
            or check.get("citation_status")!="valid" or check.get("position_status")!="valid"
            or check.get("semantic_status")!="supported" or check.get("errors")
            or not evidence or evidence!=set(check.get("evidence_ids",[]))
            or evidence!=set(check.get("declared_evidence_ids",[]))):
        return False
    if kind=="statistics":
        return check.get("support_basis")=="python_recalculation" and check.get("verification_mode")=="calculation"
    return (check.get("support_basis")=="semantic_validator" and check.get("verification_mode")=="semantic"
        and check.get("conclusion","")==value.get("text",""))


def requested_views(question):
    patterns=(("timeline",r"时间线|按时间|timeline|chronolog"),
              ("comparisons",r"比较|对比|差异|最高|最低|最大|最小|极值|compar|\b(?:max(?:imum)?|min(?:imum)?|highest|lowest)\b"),
              ("statistics",r"统计|多少|数量|条数|去重|count|statistic"))
    views=[name for name,pattern in patterns if re.search(pattern,question,re.I)]
    if re.search(r"总结|概括|概述|summari|overview",question,re.I): views.insert(0,"claims")
    return views or ["claims"]


def _verified_presentation(question,reduced,checks):
    views=requested_views(question)
    outputs={field:[] for field in OUTPUT_FIELDS}
    if reduced and reduced["status"]=="completed":
        by_id={(check["producer_id"],check["object_id"]):check for check in checks}
        for field,(key,_) in OUTPUT_FIELDS.items():
            outputs[field]=[value for value in reduced["result"].get(field,[])
                if supported_output(field,value,by_id.get((reduced["run_id"],value[key]),{}))]
    rows=[]
    for view in views:
        groups=("claims","semantic_conclusions") if view=="claims" else ("timeline","undated") if view=="timeline" else (view,)
        rows.extend({"kind":field,"value":value} for field in groups for value in outputs[field])
    if not rows:
        rows.extend({"kind":field,"value":value} for field in ("claims","semantic_conclusions") for value in outputs[field])
    # 展示预算按子问题分配，不能在问题匹配之前丢弃后面的文档或结论。
    return views,outputs,rows,False


class AnalysisWorkflow:
    def __init__(self,catalog,extractor,*,store=None,assistant=None,summarizer=None,validator=None,
                 cloud_authorized=False,map_config=MapConfig(),cloud_limits=None):
        self.catalog,self.extractor=catalog,extractor
        self.store=store or SynthesisStore(EvidenceStore(catalog))
        if self.store.catalog is not catalog: raise CatalogError("invalid_analysis_store")
        self.evidence=self.store.evidence
        self.assistant,self.summarizer,self.validator=assistant,summarizer,validator
        self.cloud_authorized,self.map_config=cloud_authorized,map_config
        self.cloud_limits=cloud_limits or CloudCallLimits.from_env()

    def _bindings(self):
        return {key:backend.identity.model_dump(mode="json") if backend is not None else None
            for key,backend in (("map",self.extractor),("entity",self.assistant),("reduce",self.summarizer),("verification",self.validator))}

    def _authorize(self):
        backends=[self.extractor,self.assistant,self.summarizer,self.validator]
        if any(backend is not None and backend.identity.provider!="offline" for backend in backends):
            if self.cloud_authorized is not True or os.getenv("DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM","0")!="1":
                raise CatalogError("workflow_cloud_not_authorized")
        if self.extractor.identity.provider=="deepseek":
            require_deepseek_authorization(getattr(self.extractor,"authorized",False))
        for kind,backend in (("entity_resolution",self.assistant),("evidence_reduce",self.summarizer),("evidence_verification",self.validator)):
            authorize_backend(backend,kind)

    @staticmethod
    def _public(response):
        result=response.get("result")
        return response | {"status":result["analysis_status"] if result else response["status"]}

    def status(self,analysis_id):
        response=self._public(self.store.get_run(analysis_id))
        progress=self.store.checkpoint_items(analysis_id,"verification_run:")
        if progress:
            response["verification_progress"]=EvidenceVerifier(self.store).progress(progress[-1]["payload"]["run_id"])
        return response | {"model_calls":CloudCallBudget.stats(self.store,analysis_id)}

    def resume(self,analysis_id,**kwargs):
        run=self.store.read_historical(analysis_id)
        if run["definition"]["kind"]!="analysis_workflow": raise CatalogError("not_analysis_workflow")
        request=run["definition"]["config"]["request"]
        return self.run(**request,analysis_id=analysis_id,**kwargs)

    def run(self,question,scope_query,template_id="general_facts",*,snapshot_id=None,analysis_id=None,
            cancel=None,on_stage=None,stage_runner=None):
        self._authorize()
        if any(not isinstance(value,str) or not value.strip() or len(value)>4000 for value in (question,scope_query)):
            raise CatalogError("invalid_analysis_request")
        get_template(template_id)
        if analysis_id:
            previous=self.store.read_historical(analysis_id)
            old=previous["definition"]["config"]
            if old["bindings"]!=self._bindings() or old["map_config"]!=self.map_config.__dict__:
                raise CatalogError("analysis_configuration_changed")
            snapshot_id=snapshot_id or old["request"]["snapshot_id"]
        snapshot=self.catalog.get_snapshot(snapshot_id) if snapshot_id else self.catalog.create_snapshot()
        resolution=ScopeResolver(self.catalog).resolve(scope_query,snapshot.snapshot_id)
        if resolution.status!="resolved": raise CatalogError(resolution.code)
        scope=resolution.scope
        task=self.evidence.create_task(scope,template_id,self.extractor.identity,self.map_config)
        request={"question":question,"scope_query":scope_query,"template_id":template_id,"snapshot_id":snapshot.snapshot_id}
        config={"algorithm":"analysis-workflow-v2","request":request,"bindings":self._bindings(),"map_config":self.map_config.__dict__}
        data=load_workflow_input(self.evidence,[task["task_id"]])
        run_id=self.store.create("analysis_workflow",data,config)["run_id"]
        if analysis_id and run_id!=analysis_id: raise CatalogError("analysis_configuration_changed")
        previous=self.store.read_historical(run_id)
        if previous["result"] and previous["result"]["analysis_status"]=="completed":
            return self.status(run_id)
        def check_cancel():
            self._authorize()
            return bool(cancel and cancel())
        def emit(stage,status):
            if on_stage:
                try: on_stage(stage,status)
                except Exception: pass
        with self.store.run_guard(run_id) as token:
            stages={}; derived=[]
            budget=CloudCallBudget(self.store,run_id,token,self.cloud_limits)
            extractor=budget_backend(self.extractor,budget,"map")
            assistant=budget_backend(self.assistant,budget,"entity_resolution")
            summarizer=budget_backend(self.summarizer,budget,"evidence_reduce")
            validator=budget_backend(self.validator,budget,"evidence_verification")
            remembered_verifications=set()
            def remember_verification(progress):
                if progress["run_id"] not in remembered_verifications:
                    self.store.save_item(run_id,"verification_run:"+progress["run_id"],token,{"run_id":progress["run_id"]})
                    remembered_verifications.add(progress["run_id"])
            def invoke(stage,action):
                if check_cancel(): raise AnalysisCancelled()
                emit(stage,"running")
                try:
                    value=stage_runner(stage,action) if stage_runner else action()
                except CatalogError as exc:
                    if exc.code in {"access_denied","snapshot_policy_changed","workflow_cloud_not_authorized","synthesis_cloud_not_authorized","deepseek_analysis_not_authorized"}:
                        raise
                    value={"status":"failed","error_code":exc.code,"result":None}
                stages[stage]={"status":value["status"],"run_id":value.get("run_id"),"error_code":value.get("error_code")}
                if value.get("progress") is not None: stages[stage]["progress"]=value["progress"]
                self.store.save_item(run_id,"stage:"+stage+":"+fingerprint(stages[stage]),token,stages[stage])
                emit(stage,value["status"])
                if value["status"]=="cancelled": raise AnalysisCancelled()
                return value
            try:
                mapped=invoke("map",lambda:EvidenceMap(self.catalog,self.evidence,extractor).run(
                    scope,template_id,self.map_config,cancel=check_cancel))
                er=invoke("entity",lambda:EntityResolver(self.store,assistant).run([task["task_id"]],cancel=check_cancel))
                reduced=None
                if er["status"]=="completed":
                    derived.append(er["run_id"])
                    reduced=invoke("reduce",lambda:EvidenceReducer(self.store,summarizer).run(er["run_id"],cancel=check_cancel))
                    if reduced["status"]=="completed": derived.append(reduced["run_id"])
                verified=invoke("verification",lambda:EvidenceVerifier(self.store,validator,cloud_limits=self.cloud_limits).run(
                    [task["task_id"]],derived_run_ids=derived,cancel=check_cancel,on_progress=remember_verification))
                verification_id=verified.get("run_id") if verified["status"]=="completed" else None
                coverage=invoke("coverage",lambda:CoverageReporter(self.store).run(
                    [task["task_id"]],verification_run_id=verification_id,derived_run_ids=derived,cancel=check_cancel))
                if coverage["status"]!="completed": raise CatalogError("coverage_not_completed")
                checks=verified["result"]["checks"] if verification_id else []
                views,outputs,presentation,truncated=_verified_presentation(question,reduced,checks)
                source_ids=sorted({key for values in outputs.values() for value in values for key in value["evidence_ids"]}
                    | {key for item in coverage["result"]["semantic_limitations"][:10] for key in item["evidence_ids"]})
                sources=[]
                for evidence_id in source_ids:
                    origin=self.evidence.get_origin(evidence_id)
                    if not origin or origin["version_status"]!="verified": raise CatalogError("analysis_input_changed")
                    record=origin["evidence"]
                    sources.append({key:record[key] for key in ("evidence_id","source","document_sha256","location")})
                self.store.get_run(coverage["run_id"])
                if check_cancel(): raise AnalysisCancelled()
                result={"analysis_status":"completed" if coverage["result"]["analysis_complete"] else "partial",
                    "task_ids":[task["task_id"]],"snapshot_id":snapshot.snapshot_id,"scope_id":scope.scope_id,
                    "stages":stages,"derived_run_ids":derived,"verification_run_id":verification_id,
                    "coverage_run_id":coverage["run_id"],"coverage":coverage["result"],
                    "question":question,"document_sources":list(scope.sources),
                    "requested_views":views,"presentation":presentation,"presentation_truncated":truncated,
                    "answer_checks":[check for check in checks if reduced and check["producer_id"]==reduced["run_id"]],
                    "supported_claims":outputs["claims"]+outputs["semantic_conclusions"],
                    "verification_progress":verified.get("progress"),"model_calls":CloudCallBudget.stats(self.store,run_id),
                    **{"supported_"+field:outputs[field] for field in ("statistics","comparisons","timeline","undated")},
                    "sources":sources,"evidence_ids":source_ids,"real_world_truth":"not_checked"}
                return self._public(self.store.finish(run_id,token,result=result))
            except AnalysisCancelled:
                return self.store.finish(run_id,token,status="cancelled")
            except Exception as exc:
                code=exc.code if isinstance(exc,CatalogError) else "analysis_workflow_failed"
                return self.store.finish(run_id,token,status="failed",error=code)
