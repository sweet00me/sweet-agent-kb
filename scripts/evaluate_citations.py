"""在隔离的合成文档上运行来源引用验收；--live 才调用真实 DeepSeek。

python -m scripts.evaluate_citations --live --output /tmp/citation-evaluation.json
不读取用户文档、不写正式 SQLite/Chroma、不执行真实写入工具。
"""

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.rag.chunk_sync import sync_chunks
from app.rag.document_sync import sync_storage
from app.rag.citations import verify_answer

CASES = [
    ("准确数值", "本次实验准确率为 92.5%。", True),
    ("训练配置", "本次实验学习率为 0.001。", True),
    ("原文否定", "本次实验未使用数据集 B，也未进行多卡训练。", True),
    ("完整归属", "本次实验仅使用数据集 A。", True),
    ("捏造数字", "本次实验准确率为 99.9%。", False),
    ("指标张冠李戴", "本次实验 mIoU 为 92.5%。", False),
    ("单位张冠李戴", "本次实验训练耗时为 92.5 秒。", False),
    ("否定反转", "本次实验使用了数据集 B。", False),
    ("训练条件反转", "本次实验进行了多卡训练。", False),
    ("无证据比较", "本次实验准确率显著优于基线。", False),
    ("无证据因果", "使用数据集 A 导致准确率提升。", False),
    ("过度概括", "该方法在所有数据集上都有很高的准确率。", False),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("真实语义验收需显式传 --live；离线检查使用 python -m unittest tests.test_citation_verification tests.test_citation_api")
    from app.agents.graph import llm
    from app.agents.prompts import CITATION_CHECK_PROMPT

    results, token_counts = [], []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        storage, parsed, cache = root / "Storage", root / "parsed", root / "chunks"
        (storage / "notes").mkdir(parents=True)
        (storage / "notes/experiment.md").write_text(
            "# 合成实验记录\n\n本次实验仅使用数据集 A。准确率为 92.5%。学习率为 0.001。"
            "实验未使用数据集 B，也未进行多卡训练。该记录没有报告 mIoU 或训练耗时。",
            encoding="utf-8",
        )
        source_db, chunk_db = root / "source.sqlite3", root / "chunks.sqlite3"
        sync_storage(storage, parsed, source_db, commit=True)
        sync_chunks(source_root=storage, parsed_root=parsed, source_db=source_db,
                    cache_dir=cache, chunk_db=chunk_db, write=True)
        chunk = json.loads(next(cache.glob("*.json")).read_text())["chunks"][0]
        record = {key: chunk[key] for key in (
            "source", "content", "chunk_index", "document_id", "file_sha256", "file_type",
        )} | {"chunk_id": chunk["id"], "distance": 0.15}
        messages = [HumanMessage(content="请依据实验记录回答。"), AIMessage(content="", tool_calls=[{
            "id": "lookup", "name": "search_knowledge_tool", "args": {"question": "实验"}, "type": "tool_call",
        }]), ToolMessage(name="search_knowledge_tool", tool_call_id="lookup", content=json.dumps({
            "collection": "sweet_kb_storage_v1", "count": 1, "matches": [record],
        }, ensure_ascii=False))]
        def judge(payload):
            # 验收独立采集返回的 Token；不调用任何正式审计/指标数据库入口。
            response = llm.invoke([SystemMessage(content=CITATION_CHECK_PROMPT),
                                   HumanMessage(content=json.dumps(payload, ensure_ascii=False))])
            token_counts.append((response.usage_metadata or {}).get("total_tokens"))
            return response.content
        for name, statement, supported in CASES:
            started = time.monotonic()
            _, report = verify_answer(messages, statement + f'【{chunk["id"]}】',
                judge=judge, source_root=storage, chunk_db=chunk_db, cache_dir=cache)
            passed = report["status"] == ("verified" if supported else "rejected")
            if not supported:
                passed = passed and report["reason"] in {"unsupported_claim", "unsupported_number"}
            results.append({"case": name, "expected_supported": supported, "passed": passed,
                            "duration_s": round(time.monotonic() - started, 2), "report": report})
            print(json.dumps({"case": name, "passed": passed, "reason": report["reason"]}, ensure_ascii=False), flush=True)
    output = {"model": llm.model_name, "cases": len(results),
              "passed": sum(item["passed"] for item in results), "results": results,
              "total_tokens": (sum(token_counts) if token_counts and all(type(n) is int for n in token_counts) else None),
              "note": "合成用例的验收结果；模型语义判定存在误判可能，不能外推为所有回答的准确率。"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f'验收：{output["passed"]}/{output["cases"]}；报告：{args.output}', flush=True)
    if output["passed"] != output["cases"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
