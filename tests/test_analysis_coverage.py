import sqlite3

from app.document_analysis.catalog import CatalogError, CatalogFilter, canonical, fingerprint
from app.document_analysis.coverage import CoverageReporter
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_reduce import EvidenceReducer
from app.document_analysis.evidence_verification import EvidenceVerifier
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.evidence_map import EvidenceMap
from app.document_analysis.evidence_schema import MapConfig
from tests.test_evidence_map import MockExtractor
from tests.test_evidence_verification import Validator
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES, DIARY

TEXT_A="张三（ORCID: 0000-0002-1825-0097）于2024-01-01测试。"
TEXT_B="李四（ORCID: 0000-0001-5109-3700）于2024-01-02测试。"


class CoverageTests(SynthesisFixture):
    def setUp(self):
        super().setUp(); self.store=SynthesisStore(self.evidence)

    def task(self,fail=False):
        return self.map_task({
            NOTES+"/a.md":(TEXT_A,{"people":["张三"],"event":"测试","time":"2024-01-01"}),
            NOTES+"/b.md":(TEXT_B,{"people":["李四"],"event":"测试","time":"2024-01-02"}),
        },fail_sources=(NOTES+"/b.md",) if fail else ())

    def analyze(self,task,validator=None):
        er=EntityResolver(self.store).run([task]); reduce=EvidenceReducer(self.store).run(er["run_id"])
        runs=[er["run_id"],reduce["run_id"]]
        verification=EvidenceVerifier(self.store,validator).run([task],derived_run_ids=runs)
        return CoverageReporter(self.store).run([task],verification_run_id=verification["run_id"],derived_run_ids=runs)

    def test_complete_scope_and_supported_claims_do_not_claim_fact_exhaustiveness(self):
        report=self.analyze(self.task(),Validator())["result"]
        self.assertEqual(report["documents"],{"expected":2,"analyzed":2,"failed":0,"missing":0,"pending":0})
        self.assertEqual(report["body"]["processing_ratio"],1.0)
        self.assertTrue(report["analysis_complete"])
        self.assertFalse(report["facts_exhaustive"])
        self.assertEqual(report["real_world_truth"],"not_checked")

    def test_full_document_coverage_without_semantic_validation_is_not_complete_analysis(self):
        report=self.analyze(self.task())["result"]
        self.assertTrue(report["document_processing_complete"])
        self.assertFalse(report["analysis_complete"])
        self.assertGreater(report["verification"]["not_checked"],0)

    def test_partial_failure_and_missing_files_have_distinct_document_counts(self):
        task=self.task(True)
        report=self.analyze(task,Validator())["result"]
        self.assertEqual(report["documents"]["failed"],1)
        self.assertEqual(report["documents"]["analyzed"],1)
        self.assertFalse(report["analysis_complete"])
        (self.root/(NOTES+"/a.md")).unlink()
        missing=CoverageReporter(self.store).run([task])["result"]
        self.assertEqual(missing["documents"]["missing"],1)

    def test_batch_overlap_is_not_double_counted_and_gaps_cannot_report_full_processing(self):
        extractor=MockExtractor(self.identity)
        result=EvidenceMap(self.service,self.evidence,extractor).run(self.scope,"general_facts",MapConfig(batch_chars=12,overlap_chars=20))
        task=result["task_id"]
        report=CoverageReporter(self.store).run([task])["result"]
        self.assertEqual(report["body"]["committed_characters"],len(self.text))
        self.assertEqual(report["body"]["expected_characters"],len(self.text))
        with sqlite3.connect(self.evidence.path) as db:
            db.execute("DELETE FROM analysis_batches WHERE task_id=? AND batch_index=1",(task,))
        broken=CoverageReporter(self.store).run([task])["result"]
        self.assertFalse(broken["document_processing_complete"])
        self.assertLess(broken["body"]["processing_ratio"],1)

    def test_same_document_in_multiple_map_tasks_has_one_document_denominator(self):
        first=self.task(); second=self.map_task({
            NOTES+"/a.md":(TEXT_A,{"people":["张三"],"event":"测试","time":"2024-01-01"}),
        },version="2")
        report=CoverageReporter(self.store).run([first,second])["result"]
        self.assertEqual(report["documents"]["expected"],2)
        self.assertEqual(report["documents"]["analyzed"],2)

    def test_unindexed_exclusions_are_not_invented_as_an_exact_disk_inventory(self):
        task=self.task()
        report=CoverageReporter(self.store).run([task])["result"]
        self.assertIn(DIARY,report["exclusions"]["enforced_paths"])
        self.assertFalse(report["exclusions"]["disk_inventory_complete"])
        self.assertGreaterEqual(report["exclusions"]["known_catalog_excluded"],0)

    def test_old_verification_cannot_be_used_after_source_version_change(self):
        task=self.task(); response=self.analyze(task,Validator())
        with self.assertRaises(CatalogError):
            self.service.policy.require(DIARY)
        self.write(NOTES+"/a.md","修改。")
        with self.assertRaises(CatalogError): self.store.get_run(response["run_id"])
        report=CoverageReporter(self.store).run([task])["result"]
        self.assertFalse(report["analysis_complete"])
        self.assertEqual(report["documents"]["failed"],1)

    def test_tampered_python_count_does_not_become_supported_statistic(self):
        task=self.task(); er=EntityResolver(self.store).run([task]); reduced=EvidenceReducer(self.store).run(er["run_id"])
        payload=reduced["result"]; payload["statistics"][0]["value"]=999
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE synthesis_runs SET result_json=?,result_sha256=? WHERE run_id=?",(canonical(payload),fingerprint(payload),reduced["run_id"]))
        verification=EvidenceVerifier(self.store,Validator()).run([task],derived_run_ids=[er["run_id"],reduced["run_id"]])
        wrong=[item for item in verification["result"]["checks"] if "python_recalculation_mismatch" in item["errors"]]
        self.assertEqual(len(wrong),1)
        self.assertEqual(wrong[0]["position_status"],"valid")
        self.assertEqual(wrong[0]["semantic_status"],"unsupported")

    def test_full_map_coverage_cannot_hide_missing_cross_task_entity_and_reduce_stages(self):
        first=self.task()
        second=self.map_task({NOTES+"/c.md":("王五于2024-01-03测试。",{"people":["王五"],"event":"测试","time":"2024-01-03"})})
        er=EntityResolver(self.store).run([first]); reduced=EvidenceReducer(self.store).run(er["run_id"])
        runs=[er["run_id"],reduced["run_id"]]
        verification=EvidenceVerifier(self.store,Validator()).run([first,second],derived_run_ids=runs)
        report=CoverageReporter(self.store).run([first,second],verification_run_id=verification["run_id"],derived_run_ids=runs)["result"]
        self.assertTrue(report["document_processing_complete"])
        self.assertFalse(report["pipeline_complete"])
        self.assertFalse(report["analysis_complete"])
        self.assertIn("pipeline_stages_missing",report["uncertainties"])
        second_er=EntityResolver(self.store).run([second]); second_reduce=EvidenceReducer(self.store).run(second_er["run_id"])
        runs.extend([second_er["run_id"],second_reduce["run_id"]])
        verification=EvidenceVerifier(self.store,Validator()).run([first,second],derived_run_ids=runs)
        separate=CoverageReporter(self.store).run([first,second],verification_run_id=verification["run_id"],derived_run_ids=runs)["result"]
        self.assertFalse(separate["pipeline_complete"],"两个独立分析不能代替跨任务关联。")
        full_er=EntityResolver(self.store).run([first,second]); full_reduce=EvidenceReducer(self.store).run(full_er["run_id"])
        runs=[full_er["run_id"],full_reduce["run_id"]]
        verification=EvidenceVerifier(self.store,Validator()).run([first,second],derived_run_ids=runs)
        full=CoverageReporter(self.store).run([first,second],verification_run_id=verification["run_id"],derived_run_ids=runs)["result"]
        self.assertTrue(full["pipeline_complete"])

    def test_map_evidence_and_derived_conclusions_have_separate_verification_counts(self):
        report=self.analyze(self.task(),Validator())["result"]
        self.assertEqual(report["verification_by_kind"]["map_evidence"]["expected_targets"],2)
        self.assertEqual(report["verification_by_kind"]["claim"]["supported"],2)
        self.assertEqual(sum(value["expected_targets"] for value in report["verification_by_kind"].values()),report["verification"]["expected_targets"])

    def test_semantic_limitations_are_persisted_and_prevent_an_unqualified_complete_report(self):
        validator=Validator(); original=validator.verify
        validator.verify=lambda request:original(request).model_copy(update={"limitations":["身份依据仅为文档中的自述。"]})
        report=self.analyze(self.task(),validator)["result"]
        self.assertTrue(report["verification_complete"])
        self.assertFalse(report["analysis_complete"])
        self.assertIn("semantic_validation_limitations",report["uncertainties"])
        self.assertTrue(any("身份依据仅为文档中的自述。" in item["limitations"] for item in report["semantic_limitations"]))

    def test_a_completed_contradiction_check_is_distinct_from_supported_conclusions(self):
        report=self.analyze(self.task(),Validator("contradicted"))["result"]
        self.assertTrue(report["verification_complete"])
        self.assertFalse(report["all_targets_supported"])
        self.assertFalse(report["analysis_complete"])
        self.assertGreater(report["verification"]["contradicted"],0)


if __name__=="__main__":
    import unittest
    unittest.main()
