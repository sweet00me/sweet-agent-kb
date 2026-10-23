"""集合分析规模回归；仅使用临时 Storage 和模拟模型。"""
from collections import Counter
from copy import deepcopy
import os
from unittest.mock import patch

from app.document_analysis.catalog import CatalogFilter
from app.document_analysis.coverage import CoverageReporter
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_map import EvidenceMap
from app.document_analysis.evidence_reduce import EvidenceReducer
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, ExtractorIdentity
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.workflow import AnalysisWorkflow
from app.tools.document_analysis import render_report
from tests.synthesis_support import SynthesisFixture
from tests.test_entity_resolution import Assistant
from tests.test_evidence_verification import Validator
from tests.document_analysis_support import NOTES


class CloudExtractor:
    identity=ExtractorIdentity(name="fake-cloud-map",version="1",provider="deepseek")
    authorized=True
    def __init__(self,delegate): self.delegate,self.calls=delegate,[]
    def extract(self,request):
        self.calls.append(request)
        return self.delegate.extract(request)


class CollectionEfficiencyTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.store = SynthesisStore(self.evidence)
        subjects = ["主题甲"] * 3 + ["主题乙"] * 3 + ["主题丙"] * 2 + ["主题丁"] * 2 + ["主题戊"] * 2
        subjects += [f"主题单独{i}" for i in range(4)]
        self.sources = [NOTES + "/collection-a.md", NOTES + "/collection-b.md"]
        rows = {source: [] for source in self.sources}
        for index, subject in enumerate(subjects):
            obj = f"对象{index:02d}"
            text = f"{subject}关联{obj}。"
            rows[self.sources[index // 8]].append(CandidateEvidence(
                quote=text, conclusion=text, uncertainty="仅按给定原文，不判断现实真实性。",
                details={"subject": subject, "predicate": "关联", "object": obj}))
        for source, candidates in rows.items():
            self.write(source, "\n".join(candidate.quote for candidate in candidates))
        self.sync()
        self.snapshot = self.service.create_snapshot(CatalogFilter(sources=tuple(self.sources)))
        self.scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), self.snapshot.snapshot_id).scope
        class Extractor:
            identity = ExtractorIdentity(name="collection-efficiency-map", version="1")
            def extract(_, request):
                return ExtractionResponse(complete=True, candidates=rows[request.window.source])
        self.extractor = Extractor()
        self.task = EvidenceMap(self.service, self.evidence, self.extractor).run(self.scope, "general_facts")["task_id"]

    def analyze(self, validator=None):
        entity = EntityResolver(self.store).run([self.task])
        reduced = EvidenceReducer(self.store).run(entity["run_id"])
        ids = [entity["run_id"], reduced["run_id"]]
        verified = EvidenceVerifier(self.store, validator).run([self.task], derived_run_ids=ids)
        return entity, reduced, verified, ids

    def test_16_evidence_32_mentions_select_nine_candidates_and_32_semantic_calls(self):
        validator = Validator()
        entity, reduced, verified, ids = self.analyze(validator)
        self.assertEqual(len(entity["result"]["mentions"]), 32)
        self.assertEqual(len(entity["result"]["entities"]), 32)
        self.assertEqual(len(entity["result"]["relations"]), 9)
        self.assertEqual(verified["result"]["target_count"], 129)
        self.assertEqual(Counter(request.target_kind for request in validator.calls), {"map_evidence": 16, "claim": 16})
        self.assertTrue(all(check["semantic_status"] == "not_checked" for check in verified["result"]["checks"]
                            if check["target_kind"] in {"entity_mention", "entity_group", "entity_relation", "comparison"}))
        report = CoverageReporter(self.store).run([self.task], verification_run_id=verified["run_id"], derived_run_ids=ids)["result"]
        self.assertTrue(report["document_processing_complete"])
        self.assertEqual(report["body"]["processing_ratio"], 1.0)
        self.assertTrue(report["verification_complete"])
        self.assertFalse(report["analysis_complete"])
        self.assertIn("unresolved_identity", report["uncertainties"])
        self.assertEqual(report["verification"]["semantic_targets"], 32)
        self.assertEqual(report["verification"]["semantic_completed"], 32)

    def test_unrelated_entities_never_trigger_assistant_or_false_identity_merges(self):
        task = self.map_task({
            NOTES + "/person-a.md": ("甲参加会议。", {"people": ["甲"], "event": "参加会议"}),
            NOTES + "/person-b.md": ("乙参加会议。", {"people": ["乙"], "event": "参加会议"}),
        })
        assistant = Assistant()
        response = EntityResolver(self.store, assistant).run([task])
        self.assertEqual(assistant.calls, [])
        self.assertEqual(response["result"]["relations"], [])
        self.assertEqual(len(response["result"]["entities"]), 2)

    def test_answer_leads_with_conclusions_and_keeps_ids_in_queryable_sources(self):
        workflow = AnalysisWorkflow(self.service, self.extractor, store=self.store, validator=Validator())
        response = workflow.run("概括文档中的主要结论", "分析全部文档", snapshot_id=self.snapshot.snapshot_id)
        text = render_report(response)
        self.assertLess(text.index("关联"), text.index("文档处理"))
        self.assertIn("结论", text)
        self.assertIn("不确定", text)
        self.assertIn("100.0%", text)
        self.assertNotIn("【Evidence:", text)
        for source in response["result"]["sources"]:
            self.assertNotIn(source["evidence_id"], text)
            self.assertEqual(self.evidence.get_origin(source["evidence_id"])["version_status"], "verified")
        self.assertEqual(text.count(self.sources[0]), 1)
        self.assertEqual(text.count(self.sources[1]), 1)

    def test_single_value_comparisons_fall_back_to_verified_facts_with_an_explicit_limit(self):
        workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator())
        response=workflow.run("比较这两份文档的事实","分析全部文档",snapshot_id=self.snapshot.snapshot_id)
        text=render_report(response)
        self.assertIn("关联",text)
        self.assertIn("暂无可发布的比较结论",text)
        self.assertEqual(response["result"]["requested_views"],["comparisons"])

    def answer_response(self, question):
        workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator())
        return workflow.run(question,"分析全部文档",snapshot_id=self.snapshot.snapshot_id)

    def test_separate_summaries_and_relationship_question_each_receive_an_answer(self):
        response=self.answer_response("请分别总结 collection-a.md 和 collection-b.md，并判断两篇笔记是否存在明确联系。")
        text=render_report(response)
        answer=text.split("文档处理：")[0]
        self.assertIn("1. collection-a.md",answer)
        self.assertIn("2. collection-b.md",answer)
        self.assertIn("3. 两篇笔记是否存在明确联系",answer)
        first,second=answer.split("2. collection-b.md",1)
        self.assertTrue(any(f"对象{i:02d}" in first for i in range(8)))
        self.assertTrue(any(f"对象{i:02d}" in second for i in range(8,16)))
        self.assertIn("没有找到",second)
        self.assertIn("直接支持证据",second)
        self.assertIn("不等于确认不存在关联",second)
        self.assertIn("[1]",first); self.assertIn("[2]",second)
        self.assertNotIn("当前展示前8项",text)
        self.assertNotIn("事实语义未核验",text)
        self.assertIn("现实真实性未验证",text)

    def test_question_relevance_selects_a_matching_fact_beyond_the_old_preview(self):
        response=self.answer_response("请概括 collection-b.md 中关于对象15的结论。")
        # 模拟历史展示窗口已经截断，但完整、已核验的结论仍在结果库中。
        response["result"]["presentation"]=response["result"]["presentation"][:1]
        response["result"]["presentation_truncated"]=True
        answer=render_report(response).split("文档处理：")[0]
        self.assertIn("对象15",answer)
        self.assertNotIn("对象00",answer)

    def test_each_document_core_concepts_question_is_treated_as_separate_summaries(self):
        for topic in ("核心概念","核心知识"):
            with self.subTest(topic=topic):
                response=self.answer_response(f"这两篇文档（collection-a.md 和 collection-b.md）各自介绍了哪些{topic}？两篇笔记之间是否有明确的内容关联？")
                answer=render_report(response).split("文档处理：")[0]
                self.assertIn("1. collection-a.md",answer)
                self.assertIn("2. collection-b.md",answer)
                self.assertIn("3. 两篇笔记是否存在明确联系",answer)
                self.assertTrue(any(f"对象{i:02d}" in answer.split("2. collection-b.md")[0] for i in range(8)))
                self.assertTrue(any(f"对象{i:02d}" in answer.split("2. collection-b.md")[1] for i in range(8,16)))

    def test_an_additional_unanswered_subquestion_is_explicit_and_statistics_are_separate(self):
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md；统计输入证据条数；这两篇文档的作者是谁？")
        answer=render_report(response).split("文档处理：")[0]
        self.assertIn("1. collection-a.md",answer); self.assertIn("2. collection-b.md",answer)
        self.assertIn("输入证据记录数：16",answer)
        self.assertIn("4. 这两篇文档的作者是谁：现有已核验结果不足",answer)

    def test_missing_verification_receipts_never_promote_legacy_fact_labels(self):
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md")
        response["result"].pop("answer_checks",None)
        answer=render_report(response).split("文档处理：")[0]
        self.assertNotIn("对象",answer)
        self.assertIn("不足",answer)

    def test_single_document_topic_query_does_not_become_a_cross_document_relationship(self):
        response=self.answer_response("请概括 collection-b.md 中关于对象15的关联事实")
        answer=render_report(response).split("文档处理：")[0]
        self.assertIn("对象15",answer)
        self.assertNotIn("两篇笔记是否存在明确联系",answer)

    def test_a_topic_from_another_document_is_not_erased_as_a_document_alias(self):
        response=self.answer_response("请概括 collection-b.md 中关于主题甲的结论")
        answer=render_report(response).split("文档处理：")[0]
        self.assertNotIn("对象",answer)
        self.assertIn("不足",answer)

    def test_answer_publication_requires_semantic_support_and_matching_citations(self):
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md")
        checks=self.store.get_run(response["result"]["verification_run_id"])["result"]["checks"]
        for change in ({"semantic_status":"not_checked"},{"semantic_status":"unsupported"},
                       {"semantic_status":"partially_supported"},{"semantic_status":"contradicted"},
                       {"support_basis":"python_identity_rules","verification_mode":"structural"},
                       {"citation_status":"invalid"},{"position_status":"invalid"},
                       {"evidence_ids":[]},{"conclusion":"另一个未核验的结论"}):
            with self.subTest(change=change):
                altered=deepcopy(checks)
                for check in altered:
                    if check["target_kind"] in {"claim","semantic_summary"}: check.update(change)
                answer=render_report(response,verification_checks=altered).split("文档处理：")[0]
                self.assertNotIn("对象",answer)
                self.assertIn("不足",answer)

    def test_a_document_without_supported_conclusions_is_not_silently_omitted(self):
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md，并判断有无明确联系")
        checks=self.store.get_run(response["result"]["verification_run_id"])["result"]["checks"]
        by_id={source["evidence_id"]:source["source"] for source in response["result"]["sources"]}
        for check in checks:
            if check["target_kind"]=="claim" and any(by_id.get(key)==self.sources[1] for key in check["evidence_ids"]):
                check["semantic_status"]="unsupported"
        answer=render_report(response,verification_checks=checks).split("文档处理：")[0]
        self.assertIn("2. collection-b.md",answer)
        self.assertIn("不足",answer.split("2. collection-b.md")[1])
        self.assertTrue(any(f"对象{i:02d}" in answer for i in range(8)))
        self.assertFalse(any(f"对象{i:02d}" in answer for i in range(8,16)))

    def test_legacy_report_replay_is_read_only_and_never_invokes_a_model(self):
        import sqlite3
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md，并判断两篇笔记是否存在明确联系")
        def state():
            with sqlite3.connect(self.store.path) as db:
                return (db.execute("SELECT run_id,result_sha256,updated_at FROM synthesis_runs ORDER BY run_id").fetchall(),
                        db.execute("SELECT count(*) FROM synthesis_items").fetchone())
        before=state()
        stored=self.store.get_run(response["run_id"])
        stored["result"].pop("answer_checks",None)
        stored["result"].pop("question",None)
        stored["result"]["presentation"]=stored["result"]["presentation"][:8]
        checks=self.store.get_run(stored["result"]["verification_run_id"])["result"]["checks"]
        with patch.object(self.extractor,"extract",side_effect=AssertionError("no model during replay")), \
                patch.object(self.store,"save_item",side_effect=AssertionError("no write during replay")), \
                patch.object(AnalysisWorkflow,"run",side_effect=AssertionError("no workflow during replay")):
            text=render_report(stored,verification_checks=checks)
        self.assertIn("3. 两篇笔记是否存在明确联系",text)
        self.assertEqual(before,state())

    def test_cross_document_cooccurrence_is_not_treated_as_a_verified_connection(self):
        response=self.answer_response("分别总结 collection-a.md 和 collection-b.md，并判断两篇笔记是否存在明确联系")
        result=response["result"]
        checks=self.store.get_run(result["verification_run_id"])["result"]["checks"]
        claim=deepcopy(result["supported_claims"][0])
        claim.update(claim_id="joint-summary",text="collection-a.md 和 collection-b.md 分别记录了不同主题。",
                     evidence_ids=[source["evidence_id"] for source in result["sources"]])
        check=deepcopy(next(check for check in checks if check["target_kind"]=="claim"))
        check.update(object_id=claim["claim_id"],conclusion=claim["text"],
                     evidence_ids=claim["evidence_ids"],declared_evidence_ids=claim["evidence_ids"])
        checks.append(check); result["supported_claims"].append(claim)
        answer=render_report(response,verification_checks=checks).split("3. 两篇笔记是否存在明确联系")[1]
        self.assertIn("没有找到",answer)
        claim["text"]="collection-a.md 明确引用 collection-b.md 的结论。"
        check["conclusion"]=claim["text"]
        answer=render_report(response,verification_checks=checks).split("3. 两篇笔记是否存在明确联系")[1]
        self.assertIn(claim["text"],answer)
        self.assertNotIn("没有找到",answer.split("文档处理：")[0])

    def test_live_progress_is_read_only_and_does_not_retrigger_model_calls(self):
        validator=Validator(); verifier=EvidenceVerifier(self.store,validator); frames=[]
        def report(event):
            progress=verifier.progress(event["run_id"])
            frames.append(progress["completed_targets"])
        result=verifier.run([self.task],on_progress=report)
        self.assertEqual(frames[0],0)
        self.assertEqual(frames[-1],16)
        self.assertEqual(frames,sorted(frames))
        self.assertEqual(len(validator.calls),16)
        self.assertEqual(result["progress"]["completed_targets"],16)

    def test_offline_structure_is_not_counted_as_completed_semantic_verification(self):
        _,_,verified,ids=self.analyze()
        self.assertEqual(verified["progress"]["total_targets"],129)
        self.assertEqual(verified["progress"]["completed_targets"],97)
        self.assertEqual(verified["progress"]["semantic_completed"],0)
        self.assertEqual(verified["progress"]["semantic_targets"],32)
        report=CoverageReporter(self.store).run([self.task],verification_run_id=verified["run_id"],derived_run_ids=ids)["result"]
        self.assertTrue(report["document_processing_complete"])
        self.assertFalse(report["verification_complete"])

    def test_cloud_budget_and_progress_survive_failure_and_resume_without_reset(self):
        from app.document_analysis.synthesis_runtime import CloudCallLimits
        entity = EntityResolver(self.store).run([self.task])
        reduced = EvidenceReducer(self.store).run(entity["run_id"])
        validator = Validator()
        validator.identity = ExtractorIdentity(name="fake-cloud-budget", version="1", provider="deepseek")
        validator.authorized = True
        verifier = EvidenceVerifier(self.store, validator, cloud_limits=CloudCallLimits(max_calls=3))
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM": "1"}):
            first = verifier.run([self.task], derived_run_ids=[entity["run_id"], reduced["run_id"]])
            self.assertEqual(first["status"], "failed")
            self.assertEqual(first["error_code"], "analysis_cloud_call_limit")
            self.assertEqual(first["progress"]["total_targets"], 129)
            self.assertEqual(first["progress"]["semantic_completed"], 3)
            self.assertEqual(len(validator.calls), 3)
            repeated = verifier.run([self.task], derived_run_ids=[entity["run_id"], reduced["run_id"]])
            self.assertEqual(repeated["run_id"], first["run_id"])
            self.assertEqual(repeated["error_code"], "analysis_cloud_call_limit")
            self.assertEqual(len(validator.calls), 3)
            self.assertEqual(verifier.progress(first["run_id"])["model_calls"]["reserved_calls"], 3)

    def test_token_and_target_limits_stop_before_any_cloud_call(self):
        from app.document_analysis.synthesis_runtime import CloudCallLimits
        for limits, code in ((CloudCallLimits(max_reserved_tokens=1), "analysis_cloud_token_limit"),
                             (CloudCallLimits(max_verification_targets=1), "verification_target_limit")):
            with self.subTest(error=code):
                validator = Validator()
                validator.identity = ExtractorIdentity(name="fake-cloud-" + code, version="1", provider="deepseek")
                validator.authorized = True
                with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM": "1"}):
                    response = EvidenceVerifier(self.store, validator, cloud_limits=limits).run([self.task])
                self.assertEqual(response["status"], "failed")
                self.assertEqual(response["error_code"], code)
                self.assertEqual(validator.calls, [])

    def test_one_workflow_budget_covers_map_and_verification(self):
        from app.document_analysis.synthesis_runtime import CloudCallLimits
        extractor = CloudExtractor(self.extractor)
        validator = Validator()
        validator.identity = ExtractorIdentity(name="fake-cloud-shared", version="1", provider="deepseek")
        validator.authorized = True
        workflow = AnalysisWorkflow(self.service, extractor, store=self.store, validator=validator,
                                    cloud_authorized=True, cloud_limits=CloudCallLimits(max_calls=3))
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1",
                                     "DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM": "1",
                                     "DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM": "1"}):
            first = workflow.run("概括结论", "分析全部文档", snapshot_id=self.snapshot.snapshot_id)
            self.assertEqual(first["status"], "partial")
            self.assertTrue(first["result"]["coverage"]["document_processing_complete"])
            self.assertEqual(first["result"]["stages"]["verification"]["error_code"], "analysis_cloud_call_limit")
            self.assertEqual(len(extractor.calls), 2)
            self.assertEqual(len(validator.calls), 1)
            resumed = workflow.resume(first["run_id"])
            self.assertEqual(resumed["run_id"], first["run_id"])
            self.assertEqual(len(extractor.calls) + len(validator.calls), 3)
            self.assertEqual(workflow.status(first["run_id"])["model_calls"]["reserved_calls"], 3)

    def test_default_budget_finishes_34_mock_cloud_calls_and_repeat_uses_checkpoints(self):
        extractor=CloudExtractor(self.extractor)
        validator=Validator()
        validator.identity=ExtractorIdentity(name="fake-cloud-full",version="1",provider="deepseek")
        validator.authorized=True
        workflow=AnalysisWorkflow(self.service,extractor,store=self.store,validator=validator,cloud_authorized=True)
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1","DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"1",
                                    "DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}):
            result=workflow.run("概括结论","分析全部文档",snapshot_id=self.snapshot.snapshot_id)
            self.assertEqual(result["result"]["verification_progress"]["semantic_completed"],32)
            self.assertEqual(result["result"]["model_calls"]["completed_calls"],34)
            self.assertEqual(len(extractor.calls),2)
            self.assertEqual(len(validator.calls),32)
            workflow.resume(result["run_id"])
            self.assertEqual(len(extractor.calls)+len(validator.calls),34)

    def test_map_budget_limit_has_an_explicit_reason_in_the_final_report(self):
        from app.document_analysis.synthesis_runtime import CloudCallLimits
        extractor=CloudExtractor(self.extractor)
        workflow=AnalysisWorkflow(self.service,extractor,store=self.store,cloud_authorized=True,
                                 cloud_limits=CloudCallLimits(max_calls=1))
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1","DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"1"}):
            result=workflow.run("概括结论","分析全部文档",snapshot_id=self.snapshot.snapshot_id)
        self.assertEqual(len(extractor.calls),1)
        self.assertEqual(result["result"]["stages"]["map"]["error_code"],"analysis_cloud_call_limit")
        self.assertIn("云端调用次数已达上限",render_report(result))

    def test_failed_model_calls_consume_budget_and_completed_targets_are_reused(self):
        from app.document_analysis.synthesis_runtime import CloudCallLimits
        validator=Validator()
        validator.identity=ExtractorIdentity(name="cloud-failure-budget",version="1",provider="deepseek")
        validator.authorized=True; validator.fail_call=1
        verifier=EvidenceVerifier(self.store,validator,cloud_limits=CloudCallLimits(max_calls=3))
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}):
            failed=verifier.run([self.task])
            self.assertEqual(failed["status"],"failed")
            self.assertEqual(failed["progress"]["model_calls"]["failed_calls"],1)
            validator.fail_call=None
            resumed=verifier.run([self.task])
            self.assertEqual(resumed["error_code"],"analysis_cloud_call_limit")
            self.assertEqual(resumed["progress"]["semantic_completed"],2)
            self.assertEqual(resumed["progress"]["model_calls"]["reserved_calls"],3)
            self.assertEqual(len(validator.calls),3)
            verifier.run([self.task])
            self.assertEqual(len(validator.calls),3)

    def test_http_timeout_and_output_cap_are_configured_without_client_retries(self):
        from app.document_analysis.deepseek_evidence import create_json_model
        with patch.dict(os.environ,{"DEEPSEEK_API_KEY":"unused-test","DOCUMENT_ANALYSIS_REQUEST_TIMEOUT_SECONDS":"12"}), \
                patch("langchain_openai.ChatOpenAI") as client:
            create_json_model("fake",max_output_tokens=2048)
        self.assertEqual(client.call_args.kwargs["timeout"],12)
        self.assertEqual(client.call_args.kwargs["max_tokens"],2048)
        self.assertEqual(client.call_args.kwargs["max_retries"],0)


if __name__ == "__main__":
    import unittest
    unittest.main()
