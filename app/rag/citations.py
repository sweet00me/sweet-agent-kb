"""核对本轮知识库引用：原件/缓存、逐段语义、精确引文；异常时拒绝草稿。

机械校验可以确定来源与引文一致，语义支持仍依赖模型判断，不能视为事实保证。
"""

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.rag.vector_sync import (
    CHUNK_DB, CHUNK_DIR, EXCLUDED_SOURCES, STORAGE_ROOT, _load_verified, _read_rows,
)

CITATION_RE = re.compile(r"【([0-9a-f]{32})】")
MAX_CLAIMS = 32
MAX_EVIDENCE = 16
MAX_ANSWER_CHARS = 16000
logger = logging.getLogger(__name__)


class CitationError(ValueError):
    """只携带稳定错误码，不把模型输出或文件异常直接返回客户端。"""


def current_turn(messages: list) -> list:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[index:]
    return []


def has_knowledge_call(messages: list) -> bool:
    return any(
        call.get("name") == "search_knowledge_tool"
        for message in current_turn(messages) if isinstance(message, AIMessage)
        for call in message.tool_calls
    )


def _retrieved_records(messages: list) -> list[dict]:
    turn = current_turn(messages)
    calls = {
        call["id"]
        for message in turn if isinstance(message, AIMessage)
        for call in message.tool_calls if call.get("name") == "search_knowledge_tool"
    }
    seen, records = set(), []
    for message in turn:
        if not isinstance(message, ToolMessage) or message.tool_call_id not in calls:
            continue
        if (message.tool_call_id in seen or message.name != "search_knowledge_tool"
                or not isinstance(message.content, str)):
            raise CitationError("invalid_tool_result")
        if message.status!="success":
            raise CitationError("retrieval_tool_error")
        seen.add(message.tool_call_id)
        try:
            payload = json.loads(message.content)
        except (ValueError, TypeError) as exc:
            raise CitationError("invalid_tool_result") from exc
        if isinstance(payload,dict) and (payload.get("error") or payload.get("error_code")):
            raise CitationError("retrieval_tool_error")
        if (not isinstance(payload, dict) or payload.get("collection") != "sweet_kb_storage_v1"
                or not isinstance(payload.get("matches"), list)
                or type(payload.get("count")) is not int
                or payload["count"] != len(payload["matches"])):
            raise CitationError("invalid_tool_result")
        records.extend(payload["matches"])
    if seen != calls:
        raise CitationError("retrieval_failed")
    if len(records) > MAX_EVIDENCE:
        raise CitationError("evidence_limit")
    return records


def _fresh_evidence(records: list[dict], source_root: Path,
                    chunk_db: Path, cache_dir: Path) -> dict[str, dict]:
    """复用入库前的路径、原文件哈希和内容寻址缓存校验。"""
    rows = {row["source"]: row for row in _read_rows(Path(chunk_db))}
    loaded, evidence = {}, {}
    for record in records:
        if not isinstance(record, dict):
            raise CitationError("invalid_evidence")
        source, chunk_id = record.get("source"), record.get("chunk_id")
        if (not isinstance(source, str) or source in EXCLUDED_SOURCES
                or not isinstance(chunk_id, str) or not re.fullmatch(r"[0-9a-f]{32}", chunk_id)
                or source not in rows):
            raise CitationError("invalid_evidence")
        if source not in loaded:
            loaded[source] = _load_verified(rows[source], Path(source_root), Path(cache_dir))
        chunk = next((c for c in loaded[source]["chunks"] if c["id"] == chunk_id), None)
        if (chunk is None or type(record.get("chunk_index")) is not int
                or any(record.get(key) != chunk.get(key) for key in (
                    "source", "content", "chunk_index", "document_id", "file_sha256", "file_type"))
                or chunk.get("review_required") or chunk.get("warnings")):
            raise CitationError("invalid_evidence")
        if chunk_id in evidence and evidence[chunk_id] != record:
            raise CitationError("invalid_evidence")
        evidence[chunk_id] = record
    return evidence


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def _numbers(text: str) -> set[str]:
    return {re.sub(r"\s+", "", match).replace(",", "") for match in re.findall(
        r"(?<![A-Za-z0-9_.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s*%)?", _normal(text),
    )}


def _claims(draft: str, evidence: dict[str, dict]) -> list[dict]:
    if not draft.strip() or len(draft) > MAX_ANSWER_CHARS:
        raise CitationError("invalid_answer")
    blocks = re.split(r"\n\s*\n", draft.strip())
    if len(blocks) > MAX_CLAIMS:
        raise CitationError("claim_limit")
    claims = []
    for index, block in enumerate(blocks):
        ids = list(dict.fromkeys(CITATION_RE.findall(block)))
        if not ids:
            raise CitationError("missing_citation")
        if any(chunk_id not in evidence for chunk_id in ids):
            raise CitationError("unknown_citation")
        text = CITATION_RE.sub("", block).strip()
        if not text:
            raise CitationError("invalid_answer")
        claims.append({"claim_id": index, "text": text, "chunk_ids": ids})
    return claims


def _checked_quotes(result, claims: list[dict], evidence: dict[str, dict]) -> dict[str, list[str]]:
    if isinstance(result, str):
        if len(result) > 32000:
            raise CitationError("invalid_judgment")
        result = json.loads(result)
    checks = result.get("checks") if isinstance(result, dict) else None
    if not isinstance(checks, list) or len(checks) != len(claims):
        raise CitationError("invalid_judgment")
    by_id = {}
    for check in checks:
        if (not isinstance(check, dict) or type(check.get("claim_id")) is not int
                or check["claim_id"] in by_id):
            raise CitationError("invalid_judgment")
        by_id[check["claim_id"]] = check
    if set(by_id) != {claim["claim_id"] for claim in claims}:
        raise CitationError("invalid_judgment")
    quotes = {}
    for claim in claims:
        check = by_id[claim["claim_id"]]
        if check.get("supported") is not True:
            raise CitationError("unsupported_claim")
        passages = check.get("evidence")
        if not isinstance(passages, list) or not passages or len(passages) > MAX_EVIDENCE:
            raise CitationError("invalid_judgment")
        used, numbers = set(), set()
        for passage in passages:
            if not isinstance(passage, dict):
                raise CitationError("invalid_judgment")
            chunk_id, quote = passage.get("chunk_id"), passage.get("quote")
            if (not isinstance(chunk_id, str) or chunk_id not in claim["chunk_ids"]
                    or not isinstance(quote, str) or len(quote.strip()) < 4
                    or quote not in evidence[chunk_id]["content"]):
                raise CitationError("quote_mismatch")
            used.add(chunk_id)
            numbers.update(_numbers(quote))
            values = quotes.setdefault(chunk_id, [])
            if quote not in values:
                values.append(quote)
        if used != set(claim["chunk_ids"]):
            raise CitationError("unsupported_citation")
        if not _numbers(claim["text"]).issubset(numbers):
            raise CitationError("unsupported_number")
    return quotes


def _refusal(reason: str, claim_count: int = 0, *, mode="rag") -> tuple[str, dict]:
    if reason == "source_unavailable":
        answer = "证据不足：引用的原文件或索引版本无法核对，请先完成文档同步后重试。"
    elif reason == "retrieval_tool_error":
        answer = "知识库检索工具执行失败（retrieval_tool_error），本轮未完成检索，不能据此判断资料是否存在。"
    elif reason in {"retrieval_failed", "invalid_tool_result"}:
        answer = "知识库检索未完成：工具结果缺失或格式无效（"+reason+"），不能据此判断证据是否充足。"
    elif reason == "verification_unavailable":
        answer = "来源核验服务暂不可用（verification_unavailable），未发布未经核验的草稿。"
    elif reason == "no_evidence":
        answer = "证据不足：本轮检索未获得可核实的资料，无法确认这一结论。"
    elif reason == "private_lookup_required":
        answer = "本轮尚未查询所需的私人资料，无法确认答案。请明确要查询的文件、目录或日期范围；集合分析仍需审批和独立授权。"
    else:
        answer = "证据不足：未能通过来源与原文核验，无法确认草稿中的结论。请补充资料或缩小问题范围。"
    return answer, {"status": "rejected", "reason": reason,"mode":mode,
                    "claim_count": claim_count, "citations": []}


def review_plain_answer(messages: list, draft: str, *, judge: Callable[[dict], object]) -> tuple[str,dict]:
    """只审查普通回答的来源依赖；绝不将此分类当作原文支持核验。"""
    if (CITATION_RE.search(draft) or re.search(r"〔\d+〕", draft)
            or re.search(r"(?m)^\[\d+\].*(?:Chunk|chunk_id|\.md|\.txt|\.docx)", draft)):
        return _refusal("no_current_evidence",mode="plain")
    # 仅用户提供的上下文与本轮匹配的工具结果可作输入，历史 AI 草稿/拒答不是证据。
    try:
        turn=current_turn(messages)
        calls={call["id"]:call["name"] for message in turn if isinstance(message,AIMessage) for call in message.tool_calls}
        result=judge({"mode":"source_dependency", "question":turn[0].content if turn else "",
            "draft":draft, "user_context":[message.content for message in messages if isinstance(message,HumanMessage)],
            # 仅传递路径定位线索，历史 AI 的事实断言仍不进入证据上下文。
            "scope_hints":list(dict.fromkeys(path for message in reversed(messages)
                if isinstance(message,AIMessage) and isinstance(message.content,str)
                for path in re.findall(r"(?<![\w./])(?:[\w.-]+/)+[\w.-]*",message.content)))[:32],
            "tool_results":[{"name":message.name,"status":message.status,"content":message.content}
                for message in turn if isinstance(message,ToolMessage) and calls.get(message.tool_call_id)==message.name]})
        if isinstance(result,str): result=json.loads(result)
        decision=result.get("decision") if isinstance(result,dict) else None
        task_type=result.get("task_type") if isinstance(result,dict) else None
        scope_status=result.get("scope_status") if isinstance(result,dict) else None
        if (task_type not in {None,"ordinary","provided","private_lookup","collection_analysis","other"}
                or scope_status not in {None,"known","unknown","not_required"}):
            raise CitationError("invalid_task_judgment")
        must_lookup=(task_type=="private_lookup" and not has_knowledge_call(messages)
            and decision in {"clarify_scope","requires_lookup","ordinary"})
        repeats_known_scope=(decision=="clarify_scope" and task_type=="collection_analysis" and scope_status=="known")
        if decision=="requires_lookup" or must_lookup or repeats_known_scope:
            answer,report=_refusal("private_lookup_required",mode="plain")
            return answer,report|({"task_type":task_type} if task_type else {})
        if decision=="clarify_scope" and not (
                (task_type=="collection_analysis" and scope_status=="unknown")
                or (task_type=="private_lookup" and has_knowledge_call(messages)) or task_type=="other"):
            raise CitationError("invalid_scope_judgment")
        if decision not in {"ordinary","provided","clarify_scope"}:
            raise CitationError("invalid_judgment")
        return draft, {"status":"not_applicable", "reason":"no_knowledge_lookup", "source_dependency":decision,
                       "claim_count":0, "citations":[], "mode":"plain"}
    except Exception as exc:
        logger.warning("Source dependency review unavailable (%s)",type(exc).__name__)
        return _refusal("verification_unavailable",mode="plain")


def verify_answer(messages: list, draft: str, *, judge: Callable[[dict], object],
                  source_root: Path = STORAGE_ROOT, chunk_db: Path = CHUNK_DB,
                  cache_dir: Path = CHUNK_DIR) -> tuple[str, dict]:
    """只引用本轮真实工具结果；不重试核验模型，不用旧对话补充证据。"""
    if not has_knowledge_call(messages):
        return review_plain_answer(messages,draft,judge=judge)
    try:
        records = _retrieved_records(messages)
        if not records:
            return _refusal("no_evidence")
        candidates = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("chunk_id"), str):
                raise CitationError("invalid_evidence")
            chunk_id = record["chunk_id"]
            # distance 是针对查询的排序分数，不属于原文或版本身份；其余字段仍逐项一致。
            comparable=lambda value:{key:child for key,child in value.items() if key!="distance"}
            if chunk_id in candidates and comparable(candidates[chunk_id]) != comparable(record):
                raise CitationError("invalid_evidence")
            candidates.setdefault(chunk_id,record)
        claims = _claims(draft, candidates)
        used_ids = list(dict.fromkeys(cid for claim in claims for cid in claim["chunk_ids"]))
        # 仅核验实际引用的候选，避免无关的旧索引阻止可核实的回答。
        cited_records = [candidates[cid] for cid in used_ids]
        try:
            evidence = _fresh_evidence(cited_records, source_root, chunk_db, cache_dir)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            raise CitationError("source_unavailable") from exc
        question = current_turn(messages)[0].content
        result = judge({"question": question, "claims": claims,
                        "evidence": [evidence[cid] for cid in used_ids]})
        quotes = _checked_quotes(result, claims, evidence)
        # 模型判断期间文件也可能被修改；返回前再次核对版本。
        if _fresh_evidence(cited_records, source_root, chunk_db, cache_dir) != evidence:
            raise CitationError("source_unavailable")
        citations, references = [], []
        for number, chunk_id in enumerate(used_ids, start=1):
            record = evidence[chunk_id]
            citations.append({key: record[key] for key in (
                "source", "chunk_id", "chunk_index", "file_sha256",
            )} | {"quotes": quotes[chunk_id]})
            # JSON 转义控制字符，避免文件名改变 Markdown 引用结构。
            source = json.dumps(record["source"], ensure_ascii=False).replace("`", "\\u0060")
            references.append(f'[{number}] `{source}` · Chunk {record["chunk_index"]} · ID `{chunk_id}`')
        numbers = {chunk_id: str(index) for index, chunk_id in enumerate(used_ids, start=1)}
        answer = CITATION_RE.sub(lambda match: f'〔{numbers[match[1]]}〕', draft)
        answer += "\n\n来源（已核对回答时的原文件与检索片段）：\n" + "\n\n".join(references)
        return answer, {"status": "verified", "reason": "checks_passed",
                        "claim_count": len(claims), "citations": citations,"mode":"rag",
                        "checked_at": datetime.now(timezone.utc).isoformat()}
    except CitationError as exc:
        return _refusal(str(exc))
    except Exception as exc:
        # 含超时、格式错误、SQLite/文件异常；失败时不得返回未经核验的草稿。
        logger.warning("Citation verification unavailable (%s)", type(exc).__name__)
        return _refusal("verification_unavailable")
