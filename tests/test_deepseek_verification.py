"""云端验证协议的离线适配器测试；所有响应来自内存模型。"""
import json
import os
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.document_analysis.catalog import CatalogError
from app.document_analysis.deepseek_verification import DeepSeekSemanticValidator
from app.document_analysis.evidence_schema import ExtractorIdentity
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.verification_schema import VerificationRequest
from app.tools import document_analysis as tool_module
from tests.synthesis_support import SynthesisFixture


class DeepSeekVerificationTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        task=self.map_task({self.source:("张三于2024-01-01测试。",{"people":["张三"],"event":"测试","time":"2024-01-01"})})
        self.task=task; self.calls=[]; self.finish="stop"; self.complete=True; self.bad_json=False; self.tools=[]
        self.record=self.evidence.get_evidence(self.evidence.list_evidence(task)[0]["evidence"]["evidence_id"])["evidence"]
        fixture=self
        class Model:
            def invoke(_,messages):
                fixture.calls.append(messages)
                payload=json.loads(messages[-1].content)
                content={"complete":fixture.complete,"status":"supported","rationale":"模拟原文支持判断。",
                    "evidence":[{"evidence_id":value["evidence_id"],"quote":value["quote"]} for value in payload["evidence"]],
                    "limitations":["不判断现实真实性。"]}
                return AIMessage(content="not-json" if fixture.bad_json else json.dumps(content,ensure_ascii=False),
                    tool_calls=fixture.tools,response_metadata={"finish_reason":fixture.finish})
        self.model=Model()
        from app.document_analysis.evidence_schema import EvidenceRecord
        record=EvidenceRecord.model_validate(self.record)
        self.request=VerificationRequest("test","map_evidence",{"conclusion":record.conclusion},(record,))

    def test_verifier_switch_and_explicit_authorization_are_both_required_even_with_map_enabled(self):
        for enabled,authorized in (("0",False),("0",True),("1",False)):
            with self.subTest(enabled=enabled,authorized=authorized),patch.dict(os.environ,{
                "DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1","DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":enabled}):
                with self.assertRaises(CatalogError):
                    DeepSeekSemanticValidator(model=self.model,authorized=authorized).verify(self.request)
        self.assertEqual(self.calls,[])

    def test_authorized_injected_model_uses_four_status_schema_and_untrusted_source_quotes(self):
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}):
            result=DeepSeekSemanticValidator(model=self.model,authorized=True).verify(self.request)
        self.assertEqual(result.status,"supported")
        self.assertEqual(result.limitations,["不判断现实真实性。"])
        self.assertIn("partially_supported",self.calls[0][0].content)
        self.assertIn("不判断现实真实性",self.calls[0][0].content)
        self.assertEqual(json.loads(self.calls[0][-1].content)["evidence"][0]["quote"],self.record["quote"])

    def test_invalid_truncated_or_incomplete_decisions_cannot_succeed(self):
        validator=DeepSeekSemanticValidator(model=self.model,authorized=True)
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}):
            for field,value in (("finish","length"),("complete",False),("bad_json",True),
                ("tools",[{"name":"unsafe","id":"tool","args":{}}])):
                original=getattr(self,field); setattr(self,field,value)
                with self.subTest(field=field),self.assertRaises(CatalogError): validator.verify(self.request)
                setattr(self,field,original)

    def test_real_verification_store_with_fake_cloud_adapter_preserves_truth_dimensions_and_revocation(self):
        validator=DeepSeekSemanticValidator(model=self.model,authorized=True)
        service=EvidenceVerifier(SynthesisStore(self.evidence),validator)
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}):
            response=service.run([self.task])
            self.assertEqual(response["status"],"completed",response)
            check=response["result"]["checks"][0]
            self.assertEqual(check["position_status"],"valid")
            self.assertEqual(check["semantic_status"],"supported")
            self.assertEqual(check["real_world_truth"],"not_checked")
            os.environ["DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM"]="0"
            with self.assertRaises(CatalogError): service.run([self.task])
        self.assertEqual(len(self.calls),1)

    def test_default_agent_service_has_verifier_but_never_opens_cloud_switches(self):
        with patch.object(tool_module,"DocumentCatalog",return_value=self.service),patch.dict(os.environ,{
            "DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM":"1","DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1",
            "DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"0"}):
            workflow=tool_module.default_analysis_service()
            self.assertIsInstance(workflow.validator,DeepSeekSemanticValidator)
            with self.assertRaises(CatalogError): workflow._authorize()
            self.assertEqual(os.environ["DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM"],"0")
        self.assertEqual(self.calls,[])

    def test_oversized_target_fails_before_sending_a_truncated_request(self):
        request=VerificationRequest("test","claim",{"text":"长"*200_001},self.request.evidence)
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}),self.assertRaises(CatalogError):
            DeepSeekSemanticValidator(model=self.model,authorized=True).verify(request)
        self.assertEqual(self.calls,[])

    def test_timeout_is_explicit_and_consumes_one_attempt_without_automatic_retry(self):
        validator=DeepSeekSemanticValidator(model=self.model,authorized=True)
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"1"}), \
                patch.object(self.model,"invoke",side_effect=TimeoutError("private transport detail")) as invoke:
            response=EvidenceVerifier(SynthesisStore(self.evidence),validator).run([self.task])
        self.assertEqual(response["status"],"failed")
        self.assertEqual(response["error_code"],"deepseek_verification_timeout")
        self.assertEqual(response["progress"]["model_calls"]["reserved_calls"],1)
        self.assertEqual(response["progress"]["model_calls"]["failed_calls"],1)
        self.assertEqual(response["progress"]["semantic_completed"],0)
        invoke.assert_called_once()


if __name__=="__main__":
    import unittest
    unittest.main()
