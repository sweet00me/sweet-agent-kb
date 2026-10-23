import os
import json
import sqlite3
from unittest.mock import patch

from app.document_analysis.catalog import CatalogError
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, ExtractorIdentity, MapConfig
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.workflow import AnalysisWorkflow
from tests.synthesis_support import SynthesisFixture
from tests.test_evidence_verification import Validator
from tests.document_analysis_support import NOTES, DIARY
from app.tools.document_analysis import render_report


class Extractor:
    identity=ExtractorIdentity(name="mock-workflow-map",version="1")
    def __init__(self): self.calls=[]; self.fail_call=None
    def extract(self,request):
        self.calls.append(request)
        if len(self.calls)==self.fail_call: raise RuntimeError("private model error")
        quote="张三于2024-01-01测试。"
        start=request.window.text.find(quote)
        candidates=[] if start<0 else [CandidateEvidence(quote=quote,conclusion="张三于2024-01-01测试。",uncertainty="仅按原文。",
            details={"people":["张三"],"event":"测试","time":"2024-01-01"},start_char=start,end_char=start+len(quote))]
        return ExtractionResponse(complete=True,candidates=candidates)


class AnalysisWorkflowTests(SynthesisFixture):
    def setUp(self):
        super().setUp(); self.write(self.source,"张三于2024-01-01测试。"); self.sync()
        self.store=SynthesisStore(self.evidence); self.extractor=Extractor()
        self.workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator())
        self.scope_query=f"分析 `{self.source}` 文档"

    def run_workflow(self,**kwargs):
        return self.workflow.run("整理测试事件",self.scope_query,"people_events",**kwargs)

    def test_complete_pipeline_persists_report_and_supported_evidence_sources(self):
        events=[]
        result=self.run_workflow(on_stage=lambda stage,status:events.append((stage,status)))
        self.assertEqual(result["status"],"completed",result)
        self.assertTrue(result["result"]["coverage"]["analysis_complete"])
        self.assertEqual({stage for stage,status in events if status=="completed"},{"map","entity","reduce","verification","coverage"})
        self.assertTrue(result["result"]["supported_claims"])
        self.assertEqual(self.workflow.status(result["run_id"])["status"],"completed")
        repeated=self.workflow.resume(result["run_id"])
        self.assertEqual(repeated["run_id"],result["run_id"])
        self.assertEqual(len(self.extractor.calls),1)

    def test_no_semantic_validator_is_explicitly_partial(self):
        workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store)
        result=workflow.run("整理",self.scope_query,"people_events")
        self.assertEqual(result["status"],"partial",result)
        self.assertFalse(result["result"]["coverage"]["analysis_complete"])
        self.assertEqual(result["result"]["supported_claims"],[])

    def test_generic_event_summary_does_not_require_the_word_event_in_each_fact(self):
        response=self.workflow.run("整理事件",self.scope_query,"people_events")
        answer=render_report(response).split("文档处理：")[0]
        self.assertIn("测试。",answer)
        self.assertIn("[1]",answer)

    def test_partial_map_failure_has_a_report_and_can_recover_saved_batches(self):
        self.write(self.source,"张三于2024-01-01测试。"+"补充正文。"*12); self.sync()
        workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator(),map_config=MapConfig(batch_chars=12,overlap_chars=20))
        self.extractor.fail_call=2
        failed=workflow.run("整理",self.scope_query,"people_events")
        self.assertEqual(failed["status"],"partial",failed)
        self.assertEqual(failed["result"]["coverage"]["documents"]["failed"],1)
        self.extractor.fail_call=None
        resumed=workflow.resume(failed["run_id"])
        self.assertEqual(resumed["status"],"completed",resumed)
        self.assertEqual(resumed["run_id"],failed["run_id"])
        self.assertEqual(sum(call.window.index==0 for call in self.extractor.calls),1)

    def test_cancelled_workflow_can_resume_and_does_not_report_success(self):
        cancelled=self.run_workflow(cancel=lambda:len(self.extractor.calls)>0)
        self.assertEqual(cancelled["status"],"cancelled")
        self.assertIsNone(cancelled["result"])
        resumed=self.workflow.resume(cancelled["run_id"])
        self.assertEqual(resumed["status"],"completed",resumed)

    def test_diary_and_unknown_scope_are_denied_without_opening_forbidden_body(self):
        with self.assertRaises(CatalogError):
            self.workflow.run("读取",f"读取 `{DIARY}` 文档","people_events")
        with self.assertRaises(CatalogError):
            self.workflow.run("读取","分析 `unknown/x.md` 文档","people_events")
        self.assertFalse(self.extractor.calls)

    def test_completed_result_cannot_be_reused_after_current_source_changes(self):
        result=self.run_workflow()
        self.write(self.source,"源已变化。")
        with self.assertRaises(CatalogError): self.workflow.status(result["run_id"])

    def test_cloud_workflow_requires_an_independent_switch_and_explicit_authorization(self):
        self.extractor.identity=ExtractorIdentity(name="mock-cloud-map",version="1",provider="deepseek")
        self.extractor.authorized=True
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1","DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"0"}):
            with self.assertRaises(CatalogError): self.run_workflow()
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1","DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"1"}):
            with self.assertRaises(CatalogError): self.run_workflow()
            enabled=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator(),cloud_authorized=True)
            result=enabled.run("整理",self.scope_query,"people_events")
            self.assertEqual(result["status"],"completed",result)

    def test_resume_rejects_a_different_runtime_backend_configuration(self):
        result=self.run_workflow()
        self.extractor.identity=ExtractorIdentity(name="changed-extractor",version="2")
        with self.assertRaises(CatalogError): self.workflow.resume(result["run_id"])

    def test_question_selects_verified_timeline_or_precise_evidence_set_statistics(self):
        timeline=self.workflow.run("按时间线整理测试事件",self.scope_query,"people_events")
        report=render_report(timeline)
        self.assertEqual(timeline["result"]["requested_views"],["timeline"])
        self.assertIn("2024-01-01：测试",report)
        self.assertIn("[1]",report)
        self.assertNotIn("【Evidence:",report)
        statistics=self.workflow.run("统计证据条数和去重情况",self.scope_query,"people_events")
        self.assertEqual(statistics["result"]["requested_views"],["statistics"])
        self.assertIn("去重后原文观察数：1",render_report(statistics))
        self.assertIn("仅针对已提供证据",render_report(statistics))
        self.assertEqual(len(self.extractor.calls),1)
        self.assertTrue(statistics["result"]["sources"])

    def test_comparison_publishes_verified_values_and_retains_conflict_uncertainty(self):
        uid="12345678-1234-4234-8234-123456789abc"
        text=f"实验A（全局ID: {uid}）学习率为0.001。\n实验A（全局ID: {uid}）学习率为0.002。"
        self.write(self.source,text); self.sync()
        class Parameters:
            identity=ExtractorIdentity(name="mock-parameters",version="1")
            def extract(_,request):
                return ExtractionResponse(complete=True,candidates=[CandidateEvidence(
                    quote=line,conclusion="原文参数",uncertainty="可能处于不同实验条件。",
                    details={"experiment":"实验A","parameters":[{"name":"学习率","value":value,"unit":None}]})
                    for line,value in zip(request.window.text.splitlines(),("0.001","0.002"))])
        workflow=AnalysisWorkflow(self.service,Parameters(),store=self.store,validator=Validator())
        result=workflow.run("比较实验学习率",self.scope_query,"research_parameters")
        self.assertEqual(result["status"],"partial",result)
        report=render_report(result)
        self.assertIn("0.001",report); self.assertIn("0.002",report)
        self.assertIn("潜在冲突",report)
        self.assertIn("potential_evidence_conflict",result["result"]["coverage"]["uncertainties"])
        self.assertEqual(len(result["result"]["sources"]),2)
        self.assertTrue(result["result"]["supported_comparisons"])

    def test_process_interruption_keeps_interrupted_status_and_resumes_committed_batches(self):
        workflow=AnalysisWorkflow(self.service,self.extractor,store=self.store,validator=Validator(),map_config=MapConfig(batch_chars=12,overlap_chars=20))
        original=self.extractor.extract
        def interrupt(request):
            if len(self.extractor.calls)==1: raise KeyboardInterrupt()
            return original(request)
        self.extractor.extract=interrupt
        with self.assertRaises(KeyboardInterrupt): workflow.run("整理",self.scope_query,"people_events")
        with sqlite3.connect(self.store.path) as db:
            run_id=next(row[0] for row in db.execute("SELECT run_id,definition_json FROM synthesis_runs") if json.loads(row[1])["kind"]=="analysis_workflow")
        self.assertEqual(workflow.status(run_id)["status"],"interrupted")
        self.extractor.extract=original
        result=workflow.resume(run_id)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(sum(request.window.index==0 for request in self.extractor.calls),1)


if __name__=="__main__":
    import unittest
    unittest.main()
