"""Python 精确聚合与可注入语义归纳；不裁决事实，也不宣称穷尽统计。"""
from datetime import date
from decimal import Context, Decimal, InvalidOperation
import re

from app.document_analysis.catalog import CatalogError, canonical, fingerprint
from app.document_analysis.entity_resolution import origin_identity
from app.document_analysis.evidence_input import load_input
from app.document_analysis.synthesis_runtime import execute_stage
from app.document_analysis.synthesis_schema import ReduceConfig, ReduceRequest, SummaryResponse


def literal(value):
    return " ".join(value.split())


def numeric(value):
    if len(value) > 256 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return None
    try:
        number = Decimal(value)
        if not number.is_finite() or abs(number.as_tuple().exponent) > 1000:
            return None
        return number
    except InvalidOperation:
        return None


def value_key(value):
    number = numeric(value)
    return {"numeric":decimal_text(number)} if number is not None else {"literal":literal(value)}


def decimal_text(number):
    # Decimal.normalize 默认采用 28 位上下文，会把不同高精度参数误折叠。
    return str(number.normalize(context=Context(prec=max(1,len(number.as_tuple().digits)))))


def _dated(record):
    provided = record.details.get("time") if record.template_id == "people_events" else None
    if provided and provided not in record.quote:
        return None, "structured_time_not_in_quote"
    candidates = [provided] if provided else sorted(set(re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", record.quote)))
    if len(candidates) != 1 or not candidates[0]:
        return None, "unknown_or_ambiguous_time"
    try:
        value = candidates[0]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value).isoformat(), "date_quoted_semantics_unverified"
    except ValueError:
        return None, "unsupported_or_invalid_time"


def _observations(records):
    groups = {}
    for record in records:
        key = fingerprint({**origin_identity(record), "template":record.template_id, "details":record.details})
        group = groups.setdefault(key, {"record":record, "evidence_ids":[]})
        group["evidence_ids"].append(record.evidence_id)
    return [groups[key] for key in sorted(groups)]


def _entity_roles(resolution):
    by_mention = {key:entity for entity in resolution["entities"] for key in entity["mention_ids"]}
    result = {}
    for mention in resolution["mentions"]:
        for evidence_id in mention["evidence_ids"]:
            result[(evidence_id,mention["role"],mention["label"])] = by_mention[mention["mention_id"]]
    return result


def _base_result(data, resolution):
    observations = _observations(data.evidence)
    roles = _entity_roles(resolution)
    claims, comparisons, timelines = {}, {}, {}
    uncertainties = set(resolution["uncertainties"]) | {"evidence_set_only_not_exhaustive", "fact_semantics_not_verified"}

    def entity(record, role, label):
        value = roles.get((record.evidence_id,role,label))
        if value is None:
            raise CatalogError("resolution_missing_mention")
        return value["entity_id"]

    def add_claim(key, text, ids, *, dated=None, time_note=None):
        claim_id = fingerprint(key)
        claim = claims.setdefault(claim_id, {
            "claim_id":claim_id, "text":text, "evidence_ids":[], "uncertainty":"原文所述，事实语义未核验。",
            "source_location_verified":True, "semantic_verified":False,
        })
        claim["evidence_ids"] = sorted(set(claim["evidence_ids"] + ids))
        if time_note:
            item = timelines.setdefault(claim_id, {"timeline_id":fingerprint(["timeline",claim_id]), "date":dated,
                "text":text, "evidence_ids":[], "uncertainty":time_note,
                "source_location_verified":True, "semantic_verified":False})
            item["evidence_ids"] = sorted(set(item["evidence_ids"] + ids))

    def add_comparison(key, label, value, unit, ids, kind):
        comparison_id = fingerprint(key)
        group = comparisons.setdefault(comparison_id, {"comparison_id":comparison_id,"kind":kind,"label":label,
            "alternatives":{},"evidence_ids":[],"source_location_verified":True,"semantic_verified":False})
        vkey = fingerprint({"value":value_key(value), "unit":unit})
        option = group["alternatives"].setdefault(vkey, {"values":[], "unit":unit, "evidence_ids":[]})
        option["values"] = sorted(set(option["values"] + [value]))
        option["evidence_ids"] = sorted(set(option["evidence_ids"] + ids))
        group["evidence_ids"] = sorted(set(group["evidence_ids"] + ids))

    for observation in observations:
        record, ids = observation["record"], sorted(observation["evidence_ids"])
        details = record.details
        if record.template_id == "research_parameters":
            owner = entity(record, "experiment", details["experiment"])
            for parameter in details["parameters"]:
                name, value, unit = literal(parameter["name"]), parameter["value"], parameter["unit"]
                unit = literal(unit) if unit is not None else None
                key = {"kind":"parameter", "entity":owner, "name":name, "value":value_key(value), "unit":unit}
                add_claim(key, f"{details['experiment']}：{parameter['name']}={value}{unit or ''}", ids)
                add_comparison(["parameter",owner,name], parameter["name"], value, unit, ids, "parameter")
        elif record.template_id == "general_facts":
            owner = entity(record, "subject", details["subject"])
            target = roles.get((record.evidence_id,"object",details["object"]))
            obj = {"entity":target["entity_id"]} if target else value_key(details["object"])
            key = {"kind":"fact", "entity":owner, "predicate":literal(details["predicate"]), "object":obj}
            add_claim(key, f"{details['subject']}：{details['predicate']} {details['object']}", ids)
            add_comparison(["fact",owner,literal(details["predicate"])], details["predicate"], details["object"], None, ids, "fact")
        else:
            owners = sorted({entity(record,"person",label) for label in set(details["people"])})
            # 无人物的事件没有稳定指代；保留独立出处，不能跨文档推测同一事件。
            when, time_note = _dated(record)
            key = {"kind":"event_statement", "entities":owners, "event":literal(details["event"]),
                   "time":when, "raw_time":details.get("time"), "location":details.get("location")}
            if not owners or when is None:
                key["origin"] = origin_identity(record)
            add_claim(key, details["event"], ids, dated=when, time_note=time_note)

    finished_comparisons = []
    for group in comparisons.values():
        group["alternatives"] = sorted(group["alternatives"].values(), key=canonical)
        group["potential_conflict"] = len(group["alternatives"]) > 1
        group["uncertainties"] = ["alternatives_may_reflect_context_or_time"] if group["potential_conflict"] else []
        if group["potential_conflict"]:
            uncertainties.add("potential_evidence_conflict")
        units = {item["unit"] for item in group["alternatives"]}
        values = [numeric(value) for item in group["alternatives"] for value in item["values"]]
        comparable = group["kind"] == "parameter" and len(units) == 1 and all(value is not None for value in values)
        group["numeric_min"] = decimal_text(min(values)) if comparable else None
        group["numeric_max"] = decimal_text(max(values)) if comparable else None
        if len(units) > 1:
            group["uncertainties"].append("incomparable_units")
        finished_comparisons.append(group)

    all_ids = [record.evidence_id for record in data.evidence]
    unresolved_entities = {item["entity_id"] for item in resolution["entities"] if item["identity_status"] == "unresolved"}
    unresolved_mentions = {mention["origin_key"] for mention in resolution["mentions"]
                           if roles[(mention["evidence_ids"][0],mention["role"],mention["label"])]["entity_id"] in unresolved_entities}
    metrics = {"input_records":len(data.evidence), "unique_observations":len(observations),
               "duplicate_origin_records":len(data.evidence)-len(observations), "statement_groups":len(claims),
               "entity_groups":len(resolution["entities"]), "unresolved_mentions":len(unresolved_mentions),
               "potential_conflicts":sum(group["potential_conflict"] for group in finished_comparisons),
               "dated_items":sum(item["date"] is not None for item in timelines.values())}
    statistics = [{"statistic_id":fingerprint(["metric",name]),"metric":name,"value":value,"evidence_ids":all_ids,
                   "basis":"provided_evidence_only", "exhaustive":False,"source_location_verified":True,"semantic_verified":False}
                  for name,value in metrics.items()]
    return {"statistics":statistics, "claims":sorted(claims.values(),key=lambda item:item["claim_id"]),
            "comparisons":sorted(finished_comparisons,key=lambda item:item["comparison_id"]),
            "timeline":sorted((item for item in timelines.values() if item["date"] is not None), key=lambda item:(item["date"],item["timeline_id"])),
            "undated":sorted((item for item in timelines.values() if item["date"] is None),key=lambda item:item["timeline_id"]),
            "evidence_ids":all_ids,"uncertainties":sorted(uncertainties), "upstream_incomplete":data.upstream_incomplete,
            "exhaustive":False,"source_location_verified":True,"semantic_verified":False}


def _semantic_batches(data, config):
    batch, size = [], 0
    for record in data.evidence:
        length = len(canonical(record.model_dump(mode="json")))
        if length > config.semantic_batch_chars:
            raise CatalogError("semantic_record_too_large")
        if batch and (len(batch) >= config.semantic_batch_size or size + length > config.semantic_batch_chars):
            yield tuple(batch)
            batch, size = [], 0
        batch.append(record)
        size += length
    if batch:
        yield tuple(batch)


def _check_qualitative_text(text):
    # 输出协议限制，不能把模型文本当作完整性或计数证明；这不是事实语义核验器。
    forbidden = (r"(?:完整|全面|全部|穷尽).{0,8}统计|统计.{0,8}(?:完整|全面|全部|穷尽)|无遗漏|没有遗漏|所有文档|全部文档|"
                 r"(?:人数|数量|总数|总计|共有|共计|总共|一共)\s*[为是:：]?\s*[\d零一二三四五六七八九十百千万]|"
                 r"\b(?:all documents|exhaustive|complete count|exactly\s+\d|total(?:\s+of)?\s+\d)\b")
    if re.search(forbidden, text, flags=re.I):
        raise CatalogError("unsupported_completeness_claim")


class EvidenceReducer:
    def __init__(self, store, summarizer=None):
        self.store, self.summarizer = store, summarizer

    def run(self, resolution_run_id, config=ReduceConfig(), *, cancel=None):
        if not isinstance(config, ReduceConfig):
            raise CatalogError("invalid_reduce_config")
        parent = self.store.get_run(resolution_run_id)
        if parent["status"] != "completed" or parent["definition"]["kind"] != "entity_resolution":
            raise CatalogError("resolution_not_completed")
        resolution = parent["result"]
        data = load_input(self.store.evidence, parent["definition"]["task_ids"])
        dependency = {"run_id":resolution_run_id, "result_sha256":fingerprint(resolution)}
        def build(session):
            if not data.evidence:
                raise CatalogError("no_available_evidence")
            result = session.checkpoint("python-reduce", lambda:_base_result(data,resolution))
            # checkpoint 对象随后要添加语义输出，先复制，避免改变保存的确定性结果。
            result = {**result, "semantic_conclusions":[]}
            if self.summarizer is not None:
                for index, batch in enumerate(_semantic_batches(data,config)):
                    allowed = {record.evidence_id for record in batch}
                    def summarize():
                        session.before_model(self.summarizer,"evidence_reduce")
                        claims = tuple(claim for claim in result["claims"] if set(claim["evidence_ids"]) & allowed)
                        request = ReduceRequest(tuple(record.model_copy(deep=True) for record in batch),
                                                tuple({**claim,"evidence_ids":sorted(set(claim["evidence_ids"]) & allowed)} for claim in claims),
                                                tuple(result["uncertainties"]))
                        response = SummaryResponse.model_validate(self.summarizer.summarize(request))
                        if not response.complete:
                            raise CatalogError("incomplete_reduce_response")
                        conclusions = []
                        for conclusion in response.conclusions:
                            if not set(conclusion.evidence_ids) <= allowed:
                                raise CatalogError("unknown_evidence_reference")
                            _check_qualitative_text(conclusion.text)
                            value = conclusion.model_dump(mode="json")
                            conclusions.append({"claim_id":fingerprint(["semantic",index,value]),
                                **value, "text":"仅就已提供证据：" + value["text"],
                                "uncertainty":"非完整统计；事实语义未核验。" + value["uncertainty"],
                                "exhaustive":False,"source_location_verified":True,"semantic_verified":False})
                        return {"conclusions":conclusions}
                    result["semantic_conclusions"].extend(session.checkpoint(f"semantic:{index}", summarize)["conclusions"])
            return result
        return execute_stage(self.store,"evidence_reduce",data,
                             {"algorithm":"reduce-python-v1", **config.model_dump(mode="json")},self.summarizer,build,
                             dependency=dependency,cancel=cancel)
