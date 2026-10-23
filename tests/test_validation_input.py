import json
import sqlite3
from unittest.mock import patch
from app.document_analysis.catalog import CatalogError, CatalogPolicy, canonical, fingerprint
from app.document_analysis.validation_input import load_validation_input, load_workflow_input
from app.document_analysis.synthesis_store import SynthesisStore
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES,DIARY


class ValidationInputTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.task = self.map_task({NOTES + "/a.md":("张三测试。", {"people":["张三"],"event":"测试"})})
        self.store = SynthesisStore(self.evidence)

    def test_versions_are_observed_without_treating_stale_locations_as_valid(self):
        first = load_validation_input(self.evidence,[self.task])
        self.assertEqual(first.documents[0]["version"]["status"],"valid")
        self.write(NOTES + "/a.md","已变化。")
        second = load_validation_input(self.evidence,[self.task])
        self.assertEqual(second.documents[0]["version"]["code"],"source_changed")
        self.assertNotEqual(first.digest,second.digest)
        self.write(NOTES + "/a.md","再次变化。")
        self.assertNotEqual(second.digest,load_validation_input(self.evidence,[self.task]).digest)

    def test_current_cache_digest_and_batches_are_part_of_validation_version(self):
        first = load_validation_input(self.evidence,[self.task])
        self.assertEqual(len(first.documents[0]["batches"]),1)
        cache,_ = self.cached(NOTES + "/a.md")
        cache.write_text("{}",encoding="utf-8")
        second = load_validation_input(self.evidence,[self.task])
        self.assertEqual(second.documents[0]["version"]["code"],"cache_changed")
        self.assertNotEqual(first.digest,second.digest)

    def test_missing_source_is_recorded_but_permission_revocation_denies_input(self):
        (self.root / (NOTES + "/a.md")).unlink()
        data=load_validation_input(self.evidence,[self.task])
        self.assertEqual(data.documents[0]["version"]["code"],"source_missing")
        self.service.policy=CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError):
            load_validation_input(self.evidence,[self.task])

    def test_verification_and_coverage_share_existing_transaction_store(self):
        data=load_validation_input(self.evidence,[self.task])
        for kind in ("evidence_verification","coverage_report"):
            run=self.store.create(kind,data,{"test":"v1"})
            with self.store.run_guard(run["run_id"]) as token:
                self.store.finish(run["run_id"],token,result={"evidence_ids":[data.evidence[0].evidence_id]})
            self.assertEqual(self.store.get_run(run["run_id"])["status"],"completed")

    def test_historical_read_checks_permissions_and_does_not_weaken_fresh_queries(self):
        data=load_validation_input(self.evidence,[self.task])
        run=self.store.create("evidence_verification",data,{"test":"v1"})
        with self.store.run_guard(run["run_id"]) as token:
            self.store.finish(run["run_id"],token,result={"evidence_ids":[data.evidence[0].evidence_id]})
        self.write(NOTES + "/a.md","变化。")
        self.assertTrue(self.store.read_historical(run["run_id"])["historical"])
        with self.assertRaises(CatalogError):
            self.store.get_run(run["run_id"])
        self.service.policy=CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError):
            self.store.read_historical(run["run_id"])

    def test_nested_stage_does_not_interrupt_or_deadlock_the_workflow_lease(self):
        data=load_workflow_input(self.evidence,[self.task])
        workflow=self.store.create("analysis_workflow",data,{"test":"v1"})["run_id"]
        stage=self.store.create("evidence_verification",load_validation_input(self.evidence,[self.task]),{})["run_id"]
        with self.store.run_guard(workflow) as outer:
            with self.store.run_guard(stage) as inner:
                self.store.finish(stage,inner,result={"evidence_ids":[]})
            self.store.finish(workflow,outer,result={"evidence_ids":[]})
        self.assertEqual(self.store.get_run(workflow)["status"],"completed")

    def test_changing_earlier_document_during_later_version_check_rejects_mixed_input(self):
        task=self.map_task({
            NOTES+"/first.md":("张三测试。",{"people":["张三"],"event":"测试"}),
            NOTES+"/last.md":("李四测试。",{"people":["李四"],"event":"测试"}),
        })
        original=self.service.read_verified; changed=False
        def read(entry):
            nonlocal changed
            body=original(entry)
            if entry.source==NOTES+"/last.md" and not changed:
                changed=True; self.write(NOTES+"/first.md","读取后续文件时前文变化。")
            return body
        with patch.object(self.service,"read_verified",side_effect=read),self.assertRaises(CatalogError):
            load_validation_input(self.evidence,[task])

    def test_forged_historical_diary_row_cannot_bypass_scope_through_evidence_listing(self):
        with sqlite3.connect(self.evidence.path) as db:
            value=json.loads(db.execute("SELECT payload_json FROM analysis_evidence WHERE task_id=?",(self.task,)).fetchone()[0])
            value["source"]=DIARY
            db.execute("UPDATE analysis_documents SET source=? WHERE task_id=?",(DIARY,self.task))
            db.execute("UPDATE analysis_evidence SET source=?,payload_json=?,payload_sha256=? WHERE task_id=?",
                       (DIARY,canonical(value),fingerprint(value),self.task))
        with self.assertRaises(CatalogError): self.evidence.list_evidence(self.task,include_unverified=True)
        with self.assertRaises(CatalogError): load_validation_input(self.evidence,[self.task])


if __name__ == "__main__":
    import unittest
    unittest.main()
