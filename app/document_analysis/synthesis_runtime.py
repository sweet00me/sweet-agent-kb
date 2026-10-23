"""两个阶段共享的可恢复执行与独立云端授权门禁。"""
import os
import re
from copy import copy
from dataclasses import asdict, dataclass, fields, is_dataclass
from time import monotonic

from app.document_analysis.catalog import CatalogError, canonical, fingerprint
from app.document_analysis.evidence_schema import ExtractorIdentity


class AnalysisCancelled(Exception):
    pass


@dataclass(frozen=True)
class CloudCallLimits:
    max_calls: int = 64
    max_reserved_tokens: int = 2_000_000
    max_request_bytes: int = 200_000
    request_timeout_seconds: float = 45.0
    max_output_tokens: int = 8192
    max_verification_targets: int = 10000

    def __post_init__(self):
        bounds = {"max_calls":4096,"max_reserved_tokens":100_000_000,"max_request_bytes":2_000_000,
                  "max_output_tokens":8192,"max_verification_targets":100000}
        if any(type(getattr(self,key)) is not int or not 1<=getattr(self,key)<=upper for key,upper in bounds.items()):
            raise CatalogError("invalid_analysis_cloud_limits")
        if type(self.request_timeout_seconds) not in (int,float) or not 0.1<=self.request_timeout_seconds<=300:
            raise CatalogError("invalid_analysis_cloud_limits")

    @classmethod
    def from_env(cls):
        names={"max_calls":"MAX_CLOUD_CALLS","max_reserved_tokens":"MAX_RESERVED_TOKENS",
               "max_request_bytes":"MAX_REQUEST_BYTES","request_timeout_seconds":"REQUEST_TIMEOUT_SECONDS",
               "max_verification_targets":"MAX_VERIFICATION_TARGETS"}
        try:
            values={key:(float(value) if key=="request_timeout_seconds" else int(value))
                for key,name in names.items() if (value:=os.getenv("DOCUMENT_ANALYSIS_"+name)) is not None}
            return cls(**values)
        except (ValueError,OverflowError) as exc:
            raise CatalogError("invalid_analysis_cloud_limits") from exc


def _request_value(value):
    if not isinstance(value,type) and hasattr(value,"model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value,type):
        return {field.name:_request_value(getattr(value,field.name)) for field in fields(value)}
    if isinstance(value,dict): return {key:_request_value(child) for key,child in value.items()}
    if isinstance(value,(list,tuple)): return [_request_value(child) for child in value]
    if isinstance(value,(str,int,float,bool)) or value is None: return value
    return {"type":getattr(value,"__name__",type(value).__name__)}


class CloudCallBudget:
    """在发请求前持久化预留，失败/中断也不退还；不保存正文或模型返回值。"""
    def __init__(self,store,run_id,token,limits):
        self.store,self.run_id,self.token,self.limits=store,run_id,token,limits

    @staticmethod
    def stats(store,run_id):
        calls=store.checkpoint_items(run_id,"cloud_call:")
        outcomes=store.checkpoint_items(run_id,"cloud_result:")
        limits=store.checkpoint_items(run_id,"cloud_limits")
        return {"reserved_calls":len(calls),"completed_calls":sum(row["payload"]["status"]=="completed" for row in outcomes),
            "failed_calls":sum(row["payload"]["status"]=="failed" for row in outcomes),
            "reserved_tokens":sum(row["payload"]["reserved_tokens"] for row in calls),
            "limits":limits[0]["payload"] if limits else None,"ledger_present":bool(limits),
            "billing_basis":"conservative_reservation_not_invoice"}

    def call(self,kind,request,action):
        limits=asdict(self.limits)
        previous=self.store.item(self.run_id,"cloud_limits",self.token)
        if previous is not None and previous!=limits:
            raise CatalogError("analysis_budget_configuration_changed")
        self.store.save_item(self.run_id,"cloud_limits",self.token,limits)
        encoded=canonical(_request_value(request)).encode("utf-8")
        if len(encoded)>self.limits.max_request_bytes:
            raise CatalogError("analysis_cloud_request_limit")
        # UTF-8 字节数加固定提示词余量及最大输出，保守预留；不是供应商账单 Token。
        reserved=len(encoded)+16384+self.limits.max_output_tokens
        stats=self.stats(self.store,self.run_id)
        if stats["reserved_calls"]>=self.limits.max_calls:
            raise CatalogError("analysis_cloud_call_limit")
        if stats["reserved_tokens"]+reserved>self.limits.max_reserved_tokens:
            raise CatalogError("analysis_cloud_token_limit")
        number=stats["reserved_calls"]+1
        self.store.save_item(self.run_id,f"cloud_call:{number:08d}",self.token,
            {"kind":kind,"request_digest":fingerprint(_request_value(request)),"request_bytes":len(encoded),"reserved_tokens":reserved})
        started=monotonic()
        try:
            value=action()
        except BaseException:
            self.store.save_item(self.run_id,f"cloud_result:{number:08d}",self.token,
                {"kind":kind,"status":"failed","duration_ms":round((monotonic()-started)*1000,2)})
            raise
        self.store.save_item(self.run_id,f"cloud_result:{number:08d}",self.token,
            {"kind":kind,"status":"completed","duration_ms":round((monotonic()-started)*1000,2)})
        return value


class BudgetedBackend:
    def __init__(self,backend,budget,kind):
        self.backend,self.budget,self.kind=copy(backend),budget,kind
        if hasattr(self.backend,"_model") and not getattr(self.backend,"_injected_model",True):
            self.backend._model=None
        if hasattr(self.backend,"request_timeout_seconds"):
            self.backend.request_timeout_seconds=budget.limits.request_timeout_seconds
        if hasattr(self.backend,"max_output_tokens"):
            self.backend.max_output_tokens=min(self.backend.max_output_tokens,budget.limits.max_output_tokens)

    def __getattr__(self,key): return getattr(self.backend,key)
    def _call(self,method,request):
        return self.budget.call(self.kind,request,lambda:getattr(self.backend,method)(request))
    def extract(self,request): return self._call("extract",request)
    def verify(self,request): return self._call("verify",request)
    def resolve(self,request): return self._call("resolve",request)
    def summarize(self,request): return self._call("summarize",request)


def budget_backend(backend,budget,kind):
    if backend is None or backend.identity.provider=="offline" or isinstance(backend,BudgetedBackend): return backend
    return BudgetedBackend(backend,budget,kind)


def authorize_backend(backend, kind):
    if backend is None:
        return None
    identity = getattr(backend, "identity", None)
    if not isinstance(identity, ExtractorIdentity):
        raise CatalogError("invalid_synthesis_backend")
    if identity.provider != "offline":
        flag = {"entity_resolution":"DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM", "evidence_reduce":"DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM",
                "evidence_verification":"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM"}.get(kind)
        if flag is None:
            raise CatalogError("invalid_synthesis_backend")
        if getattr(backend, "authorized", False) is not True or os.getenv(flag, "0") != "1":
            raise CatalogError("synthesis_cloud_not_authorized")
    return identity.model_dump(mode="json")


class StageSession:
    def __init__(self, store, run_id, token, data, cancel):
        self.store, self.run_id, self.token, self.data, self.cancel = store, run_id, token, data, cancel
        self.statistics = {"computed_units":0, "skipped_units":0}

    def check_cancel(self):
        if self.cancel is not None and self.cancel():
            raise AnalysisCancelled()

    def checkpoint(self, key, compute):
        self.check_cancel()
        prior = self.store.item(self.run_id, key, self.token)
        if prior is not None:
            self.statistics["skipped_units"] += 1
            return prior
        payload = compute()
        self.check_cancel()
        self.store.save_item(self.run_id, key, self.token, payload)
        self.statistics["computed_units"] += 1
        return payload

    def before_model(self, backend, kind):
        self.check_cancel()
        self.store.validate(self.run_id)
        authorize_backend(backend, kind)


def execute_stage(store, kind, data, config, backend, build, *, dependency=None, cancel=None):
    identity = authorize_backend(backend, kind)
    run_id = store.create(kind, data, config, backend=identity, dependency=dependency)["run_id"]
    prior = store.get_run(run_id)
    if prior["status"] == "completed":
        return prior | {"run_statistics":{"reused_result":True}}
    with store.run_guard(run_id) as token:
        session = StageSession(store, run_id, token, data, cancel)
        try:
            result = build(session)
            session.check_cancel()
            response = store.finish(run_id, token, result=result)
        except AnalysisCancelled:
            response = store.finish(run_id, token, status="cancelled")
        except Exception as exc:
            code = exc.code if isinstance(exc, CatalogError) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code) else "synthesis_failed"
            changed = code.startswith(("source_", "cache_", "snapshot_")) or code in {
                "analysis_input_changed", "synthesis_input_changed", "synthesis_dependency_changed",
            }
            response = store.finish(run_id, token, status="version_changed" if changed else "failed", error=code)
        return response | {"run_statistics":dict(session.statistics)}
