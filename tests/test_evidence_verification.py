import json
import os
import sqlite3
from unittest.mock import patch

from app.document_analysis.catalog import CatalogError, CatalogPolicy, canonical, fingerprint
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_reduce import EvidenceReducer
from app.document_analysis.evidence_schema import ExtractorIdentity
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.verification_schema import SemanticDecision, SemanticQuote
from app.document_analysis.synthesis_store import SynthesisStore
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES


class Validator:
    identity=ExtractorIdentity(name="mock-validator",version="1")

    def __init__(self,status="supported"):
        self.status,self.calls,self.fail_call,self.hook=status,[],None,None

    def verify(self,request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        if len(self.calls)==self.fail_call:
            raise RuntimeError("private verification failure")
        return SemanticDecision(complete=True,status=self.status,rationale="模拟判断，仅针对给定原文。",
            evidence=[SemanticQuote(evidence_id=record.evidence_id,quote=record.quote) for record in request.evidence])


class EvidenceVerificationTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.task=self.map_task({NOTES+"/a.md":("张三于2024-01-01测试。",{"people":["张三"],"event":"测试","time":"2024-01-01"})})
        self.store=SynthesisStore(self.evidence)

    def tamper_evidence(self,change):
        with sqlite3.connect(self.evidence.path) as db:
            row=db.execute("SELECT evidence_id,payload_json FROM analysis_evidence WHERE task_id=?",(self.task,)).fetchone()
            value=json.loads(row[1]); change(value)
            db.execute("UPDATE analysis_evidence SET payload_json=?,payload_sha256=? WHERE evidence_id=?",(canonical(value),fingerprint(value),row[0]))

    def test_location_semantic_support_and_real_world_truth_are_distinct(self):
        response=EvidenceVerifier(self.store).run([self.task])
        self.assertEqual(response["status"],"completed",response)
        check=response["result"]["checks"][0]
        self.assertEqual(check["citation_status"],"valid")
        self.assertEqual(check["semantic_status"],"not_checked")
        self.assertEqual(check["real_world_truth"],"not_checked")
        self.assertEqual(len(self.store.get_origin(response["run_id"],check["verification_id"])),1)

    def test_four_semantic_labels_are_saved_without_claiming_real_world_truth(self):
        for status in ("supported","partially_supported","contradicted","unsupported"):
            backend=Validator(status); backend.identity=ExtractorIdentity(name="mock-"+status,version="1")
            response=EvidenceVerifier(self.store,backend).run([self.task])
            check=response["result"]["checks"][0]
            self.assertEqual(check["semantic_status"],status)
            self.assertEqual(check["real_world_truth"],"not_checked")
            self.assertTrue(check["evidence_ids"])

    def test_false_offsets_hashes_and_line_numbers_are_rejected_before_semantic_calls(self):
        for change in (
            lambda value:value["location"].__setitem__("start_line",999),
            lambda value:value.__setitem__("document_sha256","0"*64),
            lambda value:value.__setitem__("cache_sha256","1"*64),
            lambda value:value["location"].__setitem__("end_char",1),
        ):
            original=self.evidence.list_evidence(self.task)[0]["evidence"]
            self.tamper_evidence(change)
            backend=Validator()
            response=EvidenceVerifier(self.store,backend).run([self.task])
            self.assertEqual(response["status"],"completed",response)
            self.assertEqual(response["result"]["checks"][0]["citation_status"],"invalid")
            self.assertFalse(backend.calls)
            self.assertEqual(response["progress"]["semantic_completed"],0)
            with sqlite3.connect(self.evidence.path) as db:
                db.execute("UPDATE analysis_evidence SET payload_json=?,payload_sha256=? WHERE task_id=?",(canonical(original),fingerprint(original),self.task))

    def test_changed_source_and_deleted_source_are_verification_failures(self):
        self.write(NOTES+"/a.md","原文已变化。")
        response=EvidenceVerifier(self.store,Validator()).run([self.task])
        self.assertEqual(response["result"]["checks"][0]["citation_status"],"invalid")
        (self.root/(NOTES+"/a.md")).unlink()
        second=EvidenceVerifier(self.store).run([self.task])
        self.assertNotEqual(response["run_id"],second["run_id"])
        self.assertIn("source_missing",second["result"]["checks"][0]["errors"])

    def test_entity_reduce_and_python_statistics_are_verified_and_traced(self):
        resolution=EntityResolver(self.store).run([self.task])
        reduced=EvidenceReducer(self.store).run(resolution["run_id"])
        response=EvidenceVerifier(self.store,Validator()).run([self.task],derived_run_ids=[resolution["run_id"],reduced["run_id"]])
        self.assertEqual(response["status"],"completed",response)
        checks=response["result"]["checks"]
        self.assertTrue(any(check["target_kind"]=="statistic" and check["support_basis"]=="python_recalculation" for check in checks))
        self.assertTrue(all(check["real_world_truth"]=="not_checked" for check in checks))

    def test_unknown_derived_evidence_id_is_diagnostic_not_a_fabricated_source(self):
        resolution=EntityResolver(self.store).run([self.task])["run_id"]
        reduced=EvidenceReducer(self.store).run(resolution)["run_id"]
        raw=self.store.read_historical(reduced)["result"]
        raw["claims"][0]["evidence_ids"]=["f"*64]
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",(canonical(raw),fingerprint(raw),reduced))
        response=EvidenceVerifier(self.store,Validator()).run([self.task],derived_run_ids=[resolution,reduced])
        invalid=[check for check in response["result"]["checks"] if "unknown_evidence_id" in check["errors"]]
        self.assertTrue(invalid)
        self.assertEqual(invalid[0]["evidence_ids"],[])

    def test_fake_semantic_quote_and_incomplete_response_do_not_publish_success(self):
        for complete in (True,False):
            backend=Validator()
            backend.verify=lambda request:SemanticDecision(complete=complete,status="supported",rationale="错误",
                evidence=[SemanticQuote(evidence_id=request.evidence[0].evidence_id,quote="伪造引用")])
            response=EvidenceVerifier(self.store,backend).run([self.task])
            self.assertEqual(response["status"],"failed")
            self.assertIsNone(response["result"])

    def test_failure_recovery_reuses_verified_targets_and_repeat_is_idempotent(self):
        second=self.map_task({NOTES+"/b.md":("李四测试。",{"people":["李四"],"event":"测试"})})
        backend=Validator(); backend.fail_call=2
        service=EvidenceVerifier(self.store,backend)
        failed=service.run([self.task,second]); self.assertEqual(failed["status"],"failed")
        backend.fail_call=None
        resumed=service.run([self.task,second]); self.assertEqual(resumed["status"],"completed")
        self.assertEqual(failed["run_id"],resumed["run_id"])
        self.assertEqual(len(backend.calls),3)
        self.assertTrue(service.run([second,self.task])["run_statistics"]["reused_result"])

    def test_permissions_and_independent_cloud_authorization_are_enforced(self):
        backend=Validator(); backend.identity=ExtractorIdentity(name="cloud-verifier",version="1",provider="deepseek"); backend.authorized=True
        with patch.dict(os.environ,{"DOCUMENT_ANALYSIS_ALLOW_VERIFICATION_LLM":"0","DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1"}):
            with self.assertRaises(CatalogError): EvidenceVerifier(self.store,backend).run([self.task])
        self.service.policy=CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError): EvidenceVerifier(self.store).run([self.task])

    def test_entity_mention_references_are_checked_in_python_without_semantic_calls(self):
        second=self.map_task({NOTES+"/b.md":("张三于2024-01-02测试。",{"people":["张三"],"event":"测试","time":"2024-01-02"})})
        resolution=EntityResolver(self.store).run([self.task,second])
        validator=Validator()
        verified=EvidenceVerifier(self.store,validator).run([self.task,second],derived_run_ids=[resolution["run_id"]])
        self.assertEqual(verified["status"],"completed",verified)
        self.assertEqual({request.target_kind for request in validator.calls},{"map_evidence"})
        relation=next(check for check in verified["result"]["checks"] if check["target_kind"]=="entity_relation")
        self.assertEqual(relation["verification_mode"],"structural")
        self.assertEqual(relation["semantic_status"],"not_checked")
        self.assertEqual(len(relation["evidence_ids"]),2)
        payload=resolution["result"]; payload["relations"][0]["left"]="unknown-mention"
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",(canonical(payload),fingerprint(payload),resolution["run_id"]))
        rejected=EvidenceVerifier(self.store,Validator()).run([self.task,second],derived_run_ids=[resolution["run_id"]])
        check=next(item for item in rejected["result"]["checks"] if item["target_kind"]=="entity_relation")
        self.assertEqual(check["position_status"],"valid")
        self.assertEqual(check["citation_status"],"invalid")
        self.assertIn("invalid_entity_reference",check["errors"])

    def test_a_known_evidence_id_from_another_task_cannot_be_attached_to_a_producers_conclusion(self):
        second=self.map_task({NOTES+"/b.md":("李四于2024-01-02测试。",{"people":["李四"],"event":"测试","time":"2024-01-02"})})
        er=EntityResolver(self.store).run([self.task]); reduced=EvidenceReducer(self.store).run(er["run_id"])
        payload=reduced["result"]
        payload["claims"][0]["evidence_ids"]=[self.evidence.list_evidence(second)[0]["evidence"]["evidence_id"]]
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",(canonical(payload),fingerprint(payload),reduced["run_id"]))
        validator=Validator()
        response=EvidenceVerifier(self.store,validator).run([self.task,second],derived_run_ids=[er["run_id"],reduced["run_id"]])
        check=next(item for item in response["result"]["checks"] if item["target_kind"]=="claim")
        self.assertEqual(check["position_status"],"valid")
        self.assertEqual(check["citation_status"],"invalid")
        self.assertIn("evidence_outside_producer_scope",check["errors"])
        self.assertNotIn(check["verification_id"],{request.target_id for request in validator.calls})

    def test_python_identity_check_rejects_a_well_formed_but_unjustified_entity_merge(self):
        second=self.map_task({NOTES+"/b.md":("张三于2024-01-02测试。",{"people":["张三"],"event":"测试","time":"2024-01-02"})})
        resolution=EntityResolver(self.store).run([self.task,second])
        payload=resolution["result"]
        keys=sorted(item["mention_id"] for item in payload["mentions"])
        group=dict(payload["entities"][0],entity_id=fingerprint(keys),mention_ids=keys,
            names=sorted({item["label"] for item in payload["mentions"]}),
            evidence_ids=sorted({key for item in payload["mentions"] for key in item["evidence_ids"]}),
            identity_status="linked_by_rule")
        payload["entities"]=[group]
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",
                (canonical(payload),fingerprint(payload),resolution["run_id"]))
        validator=Validator()
        response=EvidenceVerifier(self.store,validator).run([self.task,second],derived_run_ids=[resolution["run_id"]])
        check=next(item for item in response["result"]["checks"] if item["target_kind"]=="entity_group")
        self.assertEqual(check["citation_status"],"invalid")
        self.assertIn("entity_groups_mismatch",check["errors"])
        self.assertEqual({request.target_kind for request in validator.calls},{"map_evidence"})

    def test_python_numeric_check_rejects_tampered_comparison_before_semantic_calls(self):
        # Use a parameter task, since people/events do not generate comparisons.
        task=self.map_task({NOTES+"/parameter.md":("学习率=0.001",{"experiment":"实验A","parameters":[{"name":"学习率","value":"0.001","unit":None}]})},template="research_parameters")
        entity=EntityResolver(self.store).run([task]); reduced=EvidenceReducer(self.store).run(entity["run_id"])
        payload=reduced["result"]; payload["comparisons"][0]["numeric_max"]="999"
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",
                (canonical(payload),fingerprint(payload),reduced["run_id"]))
        validator=Validator()
        verified=EvidenceVerifier(self.store,validator).run([task],derived_run_ids=[entity["run_id"],reduced["run_id"]])
        check=next(item for item in verified["result"]["checks"] if item["target_kind"]=="comparison")
        self.assertEqual(check["semantic_status"],"unsupported")
        self.assertIn("python_recalculation_mismatch",check["errors"])
        self.assertNotIn("comparison",{request.target_kind for request in validator.calls})


if __name__=="__main__":
    import unittest
    unittest.main()
