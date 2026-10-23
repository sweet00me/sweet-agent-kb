"""文档集合分析工具；只能使用 Execution Guard 注入的本次审批能力。"""
import json
import re
from pathlib import PurePosixPath
from typing import Annotated

from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId,tool
from pydantic import Field

from app.document_analysis.catalog import CatalogError,DocumentCatalog
from app.document_analysis.evidence_schema import StrictModel
from app.document_analysis.deepseek_evidence import DeepSeekEvidenceExtractor
from app.document_analysis.deepseek_verification import DeepSeekSemanticValidator
from app.document_analysis.workflow import AnalysisWorkflow,OUTPUT_FIELDS,requested_views,supported_output
from app.observability.tracing import trace_node
from app.workflows.tool_approval import tool_fingerprint


class AnalysisArguments(StrictModel):
    question: str = Field(min_length=1,max_length=4000)
    scope_query: str = Field(min_length=1,max_length=4000)
    template_id: str = "general_facts"
    snapshot_id: str | None = None
    analysis_id: str | None = None


def default_analysis_service():
    # 授权来自执行器和独立环境开关；不在这里开启任何环境开关或立即构建模型客户端。
    return AnalysisWorkflow(DocumentCatalog(),DeepSeekEvidenceExtractor(authorized=True),
        validator=DeepSeekSemanticValidator(authorized=True),cloud_authorized=True)


def failure_reason(code):
    return {
        "workflow_cloud_not_authorized":"完整文档云端分析未获独立授权，未开始分析。批准工具调用不会开启云端开关。",
        "deepseek_analysis_not_authorized":"文档提取的云端调用未获独立授权，分析未执行或未完成。",
        "synthesis_cloud_not_authorized":"关联、归纳或证据验证的云端调用未获独立授权，分析未完成。",
        "analysis_approval_required":"本次工具调用缺少有效审批，分析未执行。",
        "analysis_tool_failed":"集合分析执行失败，未获得可发布的完整结果。",
        "analysis_cloud_call_limit":"本任务的云端调用次数已达上限，后续核验已停止；未核验结论不视为已支持。",
        "analysis_cloud_token_limit":"本任务的预留 Token 预算已达上限，后续核验已停止。",
        "analysis_cloud_request_limit":"本次核验输入超过请求大小上限，未发送云端请求。",
        "verification_target_limit":"核验目标数量超过本任务上限，未开始云端核验。",
        "analysis_budget_configuration_changed":"恢复任务的预算配置发生变化，已停止调用；原预算不会自动重置。",
        "analysis_budget_owner_changed":"请恢复原分析任务继续核验，切换任务不会重置已有核验预算。",
        "deepseek_extraction_timeout":"DeepSeek 文档提取请求超时，已保留处理进度；本次未自动重试。",
        "deepseek_verification_timeout":"DeepSeek 语义核验请求超时，已保留核验进度；本次未自动重试。",
    }.get(code,"文档集合分析未完成："+str(code or "未产生可发布报告")+"。")


def analysis_tool_message(payload,tool_call_id):
    return ToolMessage(content=json.dumps(payload,ensure_ascii=False),name="analyze_document_collection",
        tool_call_id=tool_call_id,status="success" if payload["status"]=="completed" else "error")


def _answer_rows(result,checks):
    """历史展示窗口不是完整答案；从完整已存输出重新选择，逐项核对核验凭据。"""
    rows=list(result.get("presentation",[]))
    for field in OUTPUT_FIELDS:
        values=result.get("supported_claims" if field in {"claims","semantic_conclusions"} else "supported_"+field,[])
        rows.extend({"kind":field,"value":value} for value in values)
    producers=set(result.get("derived_run_ids",[]))
    by_id={}
    for check in checks:
        if check.get("producer_id") in producers:
            by_id.setdefault(check.get("object_id"),[]).append(check)
    sources={item["evidence_id"] for item in result.get("sources",[])}
    selected=[]; seen=set()
    for row in rows:
        kind,value=row["kind"],row["value"]
        if kind not in OUTPUT_FIELDS: continue
        key=value.get(OUTPUT_FIELDS[kind][0]); identity=(kind,key)
        if identity in seen or not set(value.get("evidence_ids",[]))<=sources: continue
        if not any(supported_output(kind,value,check) for check in by_id.get(key,[])): continue
        seen.add(identity); selected.append(row)
    return selected


def _relevant_rows(rows,question,aliases=(),limit=4):
    query=question.casefold()
    for alias in sorted(aliases,key=len,reverse=True): query=query.replace(alias.casefold()," ")
    for phrase in ("主要结论","主要内容","主要观点","核心知识","核心概念","核心内容","是什么","有哪些","哪些","是否存在","分别","各自",
            "笔记","文档","总结","概括","概述","整理","说明","回答","请","中的","关于","的结论",
            "介绍","重点","内容","事实","结论","事件","信息","汇总","所有","两篇","两份","这两","以及","一下","和","与","的","中","了"):
        query=query.replace(phrase," ")
    tokens=re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+[0-9]*",query)
    terms=set(tokens)
    for token in tokens:
        # 短主题/名称须完整匹配，不能因“主题甲/主题乙”共用两个字就答非所问。
        if len(token)>=5 and re.fullmatch(r"[\u4e00-\u9fff]+",token): terms.update(token[i:i+2] for i in range(len(token)-1))
    def row_text(row): return row["value"].get("text",row["value"].get("label","")).casefold()
    ranked=sorted(enumerate(rows),key=lambda pair:(-sum(len(term) for term in terms if term in row_text(pair[1])),pair[0]))
    if terms:
        ranked=[pair for pair in ranked if any(term in row_text(pair[1]) for term in terms)]
    result=[]; seen=set()
    for _,row in ranked:
        text=row["value"].get("text",json.dumps(row["value"],ensure_ascii=False,sort_keys=True))
        if text in seen: continue
        seen.add(text); result.append(row)
        if len(result)==limit: break
    return result


def _answer_sections(question,rows,document_sources,source_by_id):
    def origins(row): return {source_by_id[key] for key in row["value"]["evidence_ids"]}
    prose=[row for row in rows if row["kind"] in {"claims","semantic_conclusions"}]
    aliases={}
    for source in document_sources:
        path=PurePosixPath(source)
        names={path.name,path.stem}
        names.update(word for word in re.split(r"[_\W]+",path.stem) if len(word)>=3 and word not in {"intro","notes","note"})
        for row in prose:
            if origins(row)=={source}:
                subject=re.split(r"[：:]",row["value"]["text"],maxsplit=1)[0].strip()
                if 2<=len(subject)<=40 and re.search(re.escape(subject)+r"\s*(?:笔记|文档)",question,re.I): names.add(subject)
        aliases[source]=sorted(names,key=len,reverse=True)
    # 重复的文件词或主题名不能充当两个不同文档的身份标签。
    aliases={source:[name for name in names if sum(name.casefold() in {alias.casefold() for alias in other}
        for other in aliases.values())==1] for source,names in aliases.items()}
    def alias_match(alias,text):
        left=r"(?<![a-zA-Z0-9_])" if alias[:1].isascii() and alias[:1].isalnum() else ""
        right=r"(?![a-zA-Z0-9_])" if alias[-1:].isascii() and alias[-1:].isalnum() else ""
        return re.search(left+re.escape(alias)+right,text,re.I)
    def mentions(text,source): return any(alias_match(alias,text) for alias in aliases[source])
    def title(source):
        matches=[alias_match(alias,question) for alias in aliases[source]]
        match=next((match for match in matches if match),None)
        return (match.group()+"笔记" if match and "." not in match.group() and "笔记" in question else
                match.group() if match else PurePosixPath(source).name)
    clauses=[value.strip(" ，,、123456789.．)）") for value in re.split(
        r"[？?；;。！!\n]+|(?:，|,)?(?:以及|同时|并且|(?:并|及|和)(?:请)?(?=判断|分析|说明|比较))|[，,](?=判断)|(?:^|\s)[1-9][、.．)）]",question)]
    clauses=[clause for clause in clauses if clause]
    sections=[]
    all_aliases=[alias for names in aliases.values() for alias in names]
    requested_sources=[source for source in document_sources if mentions(question,source)]
    for clause in clauses:
        cross_document=(len(requested_sources)>=2 or re.search(r"两篇|两份|两者|二者|文档间|笔记间|跨文档",question))
        related=(len(document_sources)>1 and cross_document
            and re.search(r"联系|关联|关系|relationship|connect|related",clause,re.I))
        if related:
            candidates=[]
            scope=requested_sources if len(requested_sources)>=2 else document_sources
            for row in prose:
                text=row["value"]["text"]
                # 共同出现、共享词汇及身份结构检查均不能推出两篇文档有联系。
                if (re.search(r"联系|关联|关系|引用|引入|依赖|用于|应用于|结合|使用|connection|related|refer|link",text,re.I)
                        and sum(mentions(text,source) for source in scope)>=2):
                    candidates.append(row)
            sections.append(("两篇笔记是否存在明确联系" if len(document_sources)==2 else "文档间是否存在明确联系",
                candidates[:4],"现有已核验结果中，没有找到"+("两篇笔记" if len(document_sources)==2 else "这些文档")+
                    "存在明确联系的直接支持证据；这不等于确认不存在关联。"))
            continue
        summary=bool(re.search(r"总结|概括|概述|各自|分别|逐篇|每篇|介绍|核心概念|summari|overview",clause,re.I))
        matched=[source for source in document_sources if mentions(clause,source)]
        separate=summary and (matched or re.search(r"分别|各自|逐篇|每篇",clause))
        if separate:
            selected=(list(document_sources) if len(document_sources)==2 and re.search(r"两篇|两份|两者",clause)
                else matched or list(document_sources))
            selected.sort(key=lambda source:min((clause.casefold().find(alias.casefold()) for alias in aliases[source]
                if alias.casefold() in clause.casefold()),default=len(clause)))
            for source in selected:
                candidates=[row for row in prose if origins(row)=={source}]
                query=re.split(r"介绍|总结|概括|概述|归纳|各自",clause,maxsplit=1)[-1]
                sections.append((title(source),_relevant_rows(candidates,query,all_aliases),
                    "现有结果中，这篇文档的已核验结论不足以回答此项；不能用未核验内容补齐。"))
            continue
        clause_views=requested_views(clause)
        extremum=bool(re.search(r"最高|最低|最大|最小|极值|\b(?:max(?:imum)?|min(?:imum)?|highest|lowest)\b",clause,re.I))
        if extremum: clause_views=["comparisons"]
        selected=[row for row in rows if row["kind"] in clause_views or (row["kind"]=="semantic_conclusions" and "claims" in clause_views)
            or (row["kind"]=="undated" and "timeline" in clause_views)]
        if not selected: selected=prose
        if matched: selected=[row for row in selected if origins(row)&set(matched)]
        # 数值及比较视图已经按字段选择；其余问题按文本相关性选择。
        chosen=selected[:8] if any(view!="claims" for view in clause_views) else _relevant_rows(selected,clause,all_aliases,8)
        if extremum:
            chosen=_relevant_rows([row for row in selected if row["kind"]=="comparisons"],clause,all_aliases,8)
        label=clause if len(clauses)>1 else "主要结论"
        if not extremum and clause_views==["comparisons"] and all(row["kind"] in {"claims","semantic_conclusions"} for row in selected):
            label="相关已核验事实（暂无可发布的比较结论）"; chosen=prose[:4]
        elif clause_views==["timeline"] and all(row["kind"] in {"claims","semantic_conclusions"} for row in selected):
            label="相关已核验事实（暂无可发布的时间线结论）"; chosen=prose[:4]
        missing=("当前结果没有通过原文支持核验的极值比较结论，极值无法确定；不能用证据条数替代。" if extremum else
                 "现有已核验结果不足以回答此项，暂无通过原文支持核验的可发布结论。")
        sections.append((label,chosen,missing))
    return sections or [("主要结论",[],"现有已核验结果不足以回答此项。")]


def render_report(response,*,question=None,verification_checks=None):
    result=response.get("result")
    if result is None:
        return failure_reason(response.get("error_code"))
    coverage=result["coverage"]; documents=coverage["documents"]
    ratio=coverage["body"]["processing_ratio"]
    metric_names={"input_records":"输入证据记录数","unique_observations":"去重后原文观察数",
        "duplicate_origin_records":"重复原文记录数","statement_groups":"陈述分组数","entity_groups":"实体分组数",
        "unresolved_mentions":"身份未确定的提及数","potential_conflicts":"潜在冲突组数","dated_items":"有日期的陈述数"}
    sources=result.get("sources",[])
    document_sources=result.get("document_sources") or sorted({item["source"] for item in sources})
    documents_by_source={value:index+1 for index,value in enumerate(sorted(document_sources))}
    source_by_id={item["evidence_id"]:item["source"] for item in sources}
    def citations(value):
        numbers=sorted({documents_by_source[source_by_id[key]] for key in value["evidence_ids"] if key in source_by_id})
        return " ".join("["+str(number)+"]" for number in numbers)
    question=question or result.get("question") or response.get("definition",{}).get("config",{}).get("request",{}).get("question") or "概括主要结论"
    checks=result.get("answer_checks",[]) if verification_checks is None else verification_checks
    rows=_answer_rows(result,checks)
    sections=_answer_sections(question,rows,document_sources,source_by_id)
    lines=[]; displayed=set()
    for index,(label,selected,missing) in enumerate(sections,1):
        sentences=[]
        for row in selected:
            value=row["value"]; kind=row["kind"]
            if kind=="statistics":
                line=metric_names.get(value["metric"],value["metric"])+"："+str(value["value"])
            elif kind in {"timeline","undated"}:
                line=(value["date"] or "日期无法确定")+"："+value["text"]
            elif kind=="comparisons":
                line=value["label"]+"："+"；".join("/".join(option["values"])+(option["unit"] or "") for option in value["alternatives"])
                if value["numeric_min"] is not None:
                    line+=f"；同单位数值范围 {value['numeric_min']}～{value['numeric_max']}"
                    if re.search(r"最高|最大|\b(?:max(?:imum)?|highest)\b",question,re.I):
                        line+=f"；已核验同组记录的最高值 {value['numeric_max']}"
                    if re.search(r"最低|最小|\b(?:min(?:imum)?|lowest)\b",question,re.I):
                        line+=f"；已核验同组记录的最低值 {value['numeric_min']}"
                elif re.search(r"最高|最低|最大|最小|极值|\b(?:max(?:imum)?|min(?:imum)?|highest|lowest)\b",question,re.I):
                    line+="；现有数值或单位不可直接比较，极值无法确定"
                if value["uncertainties"]: line+="；限制："+"、".join(value["uncertainties"])
            else:
                line=value["text"]
            sentences.append(line.rstrip("。； ")+"。"+citations(value))
            displayed.add((kind,value[OUTPUT_FIELDS[kind][0]]))
        if not sentences: sentences=[missing]
        # Reduce 的固定阶段说明早于 Verification；不把过期阶段状态当成当前结论的不确定性。
        uncertainties=list(dict.fromkeys(row["value"].get("uncertainty","") for row in selected
            if row["value"].get("uncertainty") not in {None,"","原文所述，事实语义未核验。"}))
        if uncertainties: sentences.append("说明："+"；".join(uncertainties[:2]))
        prefix=f"{index}. " if len(sections)>1 else ""
        lines.extend([prefix+label+"："+" ".join(sentences),""])
    lines.extend(["",f"文档处理：已分析 {documents['analyzed']}/{documents['expected']} 份，失败 {documents['failed']}，缺失 {documents['missing']}，待处理 {documents['pending']}；正文覆盖率 {f'{ratio:.1%}' if ratio is not None else '无法确定'}。"])
    if coverage["document_processing_complete"] and not coverage["analysis_complete"]:
        lines.append("文档处理完成；结论或身份关联仍存在不确定性。")
    progress=result.get("verification_progress")
    checks=coverage["verification"]
    if progress:
        lines.append(f"核验进度：{progress['completed_targets']}/{progress['total_targets']} 项；其中结论语义核验 {progress['semantic_completed']}/{progress['semantic_targets']} 项。结构检查不计为语义支持。")
    else:
        lines.append(f"核验进度：{checks['checked_targets']}/{checks['expected_targets']} 项；原文支持 {checks['supported']}，部分支持 {checks['partially_supported']}，矛盾 {checks['contradicted']}，不支持 {checks['unsupported']}。")
    if "statistics" in result["requested_views"]:
        lines.append("统计仅针对已提供证据，不等同于现实人数或事件总数；人物或对象的提及次数、实体分组数与可确认的不同对象数量不能互换。未提供对应已核验统计时，独立对象总数无法确定。")
    if re.search(r"最高|最低|最大|最小|极值|\b(?:max(?:imum)?|min(?:imum)?|highest|lowest)\b",question,re.I):
        lines.append("极值来自已核验的同组、同单位记录；分组间身份、单位或实验条件不同，不据此宣称全局最优。")
    for stage in result.get("stages",{}).values():
        if stage.get("error_code"): lines.append(failure_reason(stage["error_code"]))
    uncertainty_names={"unresolved_identity":"同名或指代身份尚不能确定，未自动合并",
        "identity_conflict":"实体身份依据存在冲突","potential_evidence_conflict":"文档陈述存在潜在冲突",
        "verification_incomplete":"部分结论尚未完成语义核验","semantic_validation_limitations":"语义核验仍有限制条件",
        "unsupported_or_conflicting_conclusions":"部分结论未获原文支持或存在矛盾",
        "upstream_incomplete":"部分文档处理未完成","unknown_or_ambiguous_time":"部分事件时间不明"}
    if coverage["uncertainties"]:
        lines.append("主要不确定性："+"；".join(uncertainty_names.get(code,code) for code in coverage["uncertainties"][:4])+"。")
    limitations=list(dict.fromkeys(text for item in coverage.get("semantic_limitations",[]) for text in item["limitations"]))
    if limitations: lines.append("核验限制："+"；".join(limitations[:3]))
    calls=result.get("model_calls")
    if calls and calls["reserved_calls"]:
        lines.append(f"云端调用额度：已占用 {calls['reserved_calls']}/{calls['limits']['max_calls']} 次；失败和中断同样占用额度。")
    lines.append("正文覆盖率不代表事实无遗漏，现实真实性未验证。")
    if documents_by_source:
        lines.extend(["","来源文档："]+[f"[{number}] {source}" for source,number in documents_by_source.items()])
    if len(displayed)<len(rows):
        lines.append("已按问题选取已核验结论；完整结果与详细来源保留可查询。")
    lines.append("分析任务："+response["run_id"]+"（可查询 Evidence ID、SHA256 和原文位置）。")
    return "\n".join(lines)


@tool
def analyze_document_collection(question:str,scope_query:str,tool_call_id:Annotated[str,InjectedToolCallId],
        config:RunnableConfig,template_id:str="general_facts",snapshot_id:str|None=None,analysis_id:str|None=None)->ToolMessage:
    """授权文档集合分析：按任务需要逐篇阅读，跨文档汇总、统计、极值、比较或整理事件。question 保留完整内容问题与限定条件；scope_query 仅写用户明确的目录/文件/修改日期/类型/排除范围，范围不清先询问，不按主题猜目录或扫描全部文档。按证据结构选模板：人物事件 people_events，实验参数指标 research_parameters，主体关系事实 general_facts。需人工审批及独立云端授权；精确事实/片段用 search_knowledge_tool。"""
    try:
        arguments=AnalysisArguments(question=question,scope_query=scope_query,template_id=template_id,snapshot_id=snapshot_id,analysis_id=analysis_id)
        permit=config.get("configurable",{}).get("document_analysis_permits",{}).get(tool_call_id)
        if (not isinstance(permit,dict) or permit.get("decision")!="approve"
                or permit.get("name")!="analyze_document_collection"
                or permit.get("fingerprint")!=tool_fingerprint({"name":"analyze_document_collection","args":permit.get("args",{})})
                or AnalysisArguments.model_validate(permit["args"])!=arguments):
            raise CatalogError("analysis_approval_required")
        service=config.get("configurable",{}).get("document_analysis_service") or default_analysis_service()
        def runner(stage,action):
            return trace_node("analysis_"+stage,lambda state:action())({},config)
        response=service.run(**arguments.model_dump(),stage_runner=runner)
        result=response.get("result")
        checks=None
        if result and "answer_checks" not in result and result.get("verification_run_id"):
            checks=service.store.get_run(result["verification_run_id"])["result"]["checks"]
        return analysis_tool_message({"kind":"document_analysis","status":response["status"],"analysis_id":response.get("run_id"),
            "error_code":response.get("error_code"),
            "answer":render_report(response,question=question,verification_checks=checks),"analysis_complete":bool(result and result["coverage"]["analysis_complete"]),
            "coverage":result["coverage"] if result else None,"sources":result["sources"] if result else [],
            "real_world_truth":"not_checked"},tool_call_id)
    except CatalogError as exc:
        return analysis_tool_message({"kind":"document_analysis","status":"blocked","error_code":exc.code,
            "answer":failure_reason(exc.code),"sources":[],"real_world_truth":"not_checked"},tool_call_id)
    except Exception:
        return analysis_tool_message({"kind":"document_analysis","status":"failed","error_code":"analysis_tool_failed",
            "answer":failure_reason("analysis_tool_failed"),"sources":[],"real_world_truth":"not_checked"},tool_call_id)


def analysis_answer(history):
    """仅消费当前用户轮次、匹配真实工具调用 ID 的结果；不让模型改写统计。"""
    start=max((index for index,message in enumerate(history) if isinstance(message,HumanMessage)),default=-1)
    calls={}; reports={}
    for message in history[start+1:]:
        if isinstance(message,AIMessage):
            for call in message.tool_calls:
                if call["name"]=="analyze_document_collection": calls[call["id"]]=call
        elif isinstance(message,ToolMessage) and message.name=="analyze_document_collection" and message.tool_call_id in calls:
            try: payload=json.loads(message.content)
            except (ValueError,TypeError): payload=None
            if not isinstance(payload,dict): payload={}
            if payload.get("kind")!="document_analysis":
                if payload.get("status")=="rejected":
                    payload={"status":"rejected","answer":"文档集合分析未执行：未获得有效审批。","sources":[]}
                elif message.status=="error":
                    # ToolNode 参数校验等错误可能是纯文本，也必须结束为明确工具失败。
                    payload={"status":"failed","error_code":"analysis_tool_failed",
                        "answer":failure_reason("analysis_tool_failed"),"sources":[]}
                else: continue
            answer=payload.get("answer")
            if not isinstance(answer,str): continue
            reports[message.tool_call_id]=payload
    if not reports: return None
    values=[reports[key] for key in calls if key in reports]
    complete=len(values)==len(calls) and all(value.get("status")=="completed" for value in values)
    answer=values[0]["answer"] if len(values)==1 else "\n\n".join(f"分析{index+1}：\n"+value["answer"] for index,value in enumerate(values))
    if len(values)!=len(calls): answer+="\n部分分析调用缺少结果，不能视为整轮分析完成。"
    sources={source["evidence_id"]:source for value in values for source in value.get("sources",[]) if isinstance(source,dict) and "evidence_id" in source}
    return answer,{"status":"not_applicable","reason":"document_analysis_report","claim_count":0,"citations":[],"mode":"document_analysis",
        "analysis_status":values[0].get("status") if len(calls)==len(values)==1 else "completed" if complete else "partial",
        "analysis_id":values[0].get("analysis_id") if len(values)==1 else None,
        "analysis_ids":[value["analysis_id"] for value in values if value.get("analysis_id")],
        "analysis_error_code":values[0].get("error_code") if len(values)==1 else None,
        "analysis_error_message":failure_reason(values[0].get("error_code")) if len(values)==1 and values[0].get("error_code") else None,
        "analysis_complete":complete and all(value.get("analysis_complete",False) for value in values),
        "sources":list(sources.values()),"real_world_truth":"not_checked"}


def other_tool_history(history):
    """为混合工具轮次组装其余工具上下文，原始 Checkpoint 消息保持完整。"""
    start=max((index for index,message in enumerate(history) if isinstance(message,HumanMessage)),default=-1)
    calls=[call for message in history[start+1:] if isinstance(message,AIMessage) for call in message.tool_calls]
    if not any(call["name"]!="analyze_document_collection" for call in calls):
        return None
    analysis_ids={call["id"] for call in calls if call["name"]=="analyze_document_collection"}
    result=list(history[:start+1])
    for message in history[start+1:]:
        if isinstance(message,ToolMessage) and message.tool_call_id in analysis_ids:
            continue
        if isinstance(message,AIMessage) and any(call["id"] in analysis_ids for call in message.tool_calls):
            remaining=[call for call in message.tool_calls if call["id"] not in analysis_ids]
            if not remaining and not message.content:
                continue
            message=message.model_copy(update={"tool_calls":remaining,
                "additional_kwargs":{key:value for key,value in message.additional_kwargs.items() if key!="tool_calls"}})
        result.append(message)
    return result
