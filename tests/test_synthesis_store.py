import json
import sqlite3
import subprocess
import sys

from app.document_analysis.catalog import CatalogError
from app.document_analysis.evidence_input import load_input
from app.document_analysis.synthesis_store import SynthesisStore
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES


class SynthesisStoreTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.task_id = self.map_task({NOTES + "/a.md": ("张三测试。", {"people":["张三"], "event":"测试"})})
        self.data = load_input(self.evidence, [self.task_id])
        self.store = SynthesisStore(self.evidence)

    def create(self):
        return self.store.create("entity_resolution", self.data, {"algorithm":"test-v1"})

    def payload(self):
        return {"entity_id":"entity-test", "evidence_ids":[self.data.evidence[0].evidence_id],
                "source_location_verified":True, "semantic_verified":False}

    def test_dedicated_store_is_idempotent_and_does_not_add_tables_to_map_store(self):
        first = self.create()
        self.assertEqual(first["run_id"], self.create()["run_id"])
        with sqlite3.connect(self.evidence.path) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='synthesis_runs'").fetchone())

    def test_checkpoint_is_idempotent_and_completed_result_traces_to_original_evidence(self):
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id) as token:
            self.assertIsNone(self.store.item(run_id, "unit", token))
            self.store.save_item(run_id, "unit", token, self.payload())
            self.store.save_item(run_id, "unit", token, self.payload())
            self.assertEqual(self.store.item(run_id, "unit", token), self.payload())
            self.store.finish(run_id, token, result={"entities":[self.payload()]})
        result = self.store.get_run(run_id)
        self.assertEqual(result["status"], "completed")
        origin = self.store.get_origin(run_id, "entity-test")
        self.assertEqual(len(origin), 1)
        self.assertEqual(origin[0]["version_status"], "verified")

    def test_unknown_source_and_false_semantic_verification_cannot_be_saved(self):
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id) as token:
            for payload in ({"evidence_ids":["invented"]}, {**self.payload(), "semantic_verified":True}):
                with self.subTest(payload=payload), self.assertRaises(CatalogError):
                    self.store.save_item(run_id, "bad", token, payload)
            self.assertIsNone(self.store.item(run_id, "bad", token))

    def test_unfinished_run_becomes_interrupted_and_keeps_checkpoint_for_recovery(self):
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id) as token:
            self.store.save_item(run_id, "unit", token, self.payload())
        self.assertEqual(self.store.get_run(run_id)["status"], "interrupted")
        with self.store.run_guard(run_id) as token:
            self.assertEqual(self.store.item(run_id, "unit", token), self.payload())
            self.store.finish(run_id, token, status="failed", error="test_failure")
        self.assertIsNone(self.store.get_run(run_id)["result"])

    def test_second_runner_is_rejected_and_wrong_lease_cannot_write(self):
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id):
            with self.assertRaises(CatalogError):
                with SynthesisStore(self.evidence).run_guard(run_id):
                    self.fail("second runner acquired lock")
            with self.assertRaises(CatalogError):
                self.store.save_item(run_id, "unit", "wrong", self.payload())

    def test_source_update_blocks_reuse_and_provenance_query(self):
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id) as token:
            self.store.finish(run_id, token, result={"entities":[self.payload()]})
        self.write(NOTES + "/a.md", "原文已改变。")
        with self.assertRaises(CatalogError):
            self.store.get_run(run_id)
        with self.assertRaises(CatalogError):
            self.store.get_origin(run_id, "entity-test")

    def test_other_databases_and_payload_tampering_are_rejected(self):
        for path in (self.evidence.path, self.db, self.snapshots):
            with self.subTest(path=path), self.assertRaises(CatalogError):
                SynthesisStore(self.evidence, path=path).create("entity_resolution", self.data, {})
        run_id = self.create()["run_id"]
        with self.store.run_guard(run_id) as token:
            self.store.save_item(run_id, "unit", token, self.payload())
            with sqlite3.connect(self.store.path) as db:
                db.execute("UPDATE synthesis_items SET payload_json='{}' WHERE run_id=?", (run_id,))
            with self.assertRaises(CatalogError):
                self.store.item(run_id, "unit", token)

    def test_process_death_releases_lock_and_recovers_incremental_items(self):
        run_id = self.create()["run_id"]
        script = """
import json, os, socket, sys
def deny(*args, **kwargs):
    raise AssertionError('offline subprocess attempted network')
socket.socket.connect = deny
socket.getaddrinfo = deny
from app.document_analysis.catalog import DocumentCatalog
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.synthesis_store import SynthesisStore
store = SynthesisStore(EvidenceStore(DocumentCatalog(sys.argv[1])))
with store.run_guard(sys.argv[2]) as token:
    store.save_item(sys.argv[2], 'unit', token, json.loads(sys.argv[3]))
    os._exit(71)
"""
        process = subprocess.run([sys.executable,"-c",script,str(self.root),run_id,json.dumps(self.payload())],
                                 capture_output=True,text=True,timeout=15,check=False)
        self.assertEqual(process.returncode, 71, process.stderr)
        self.assertEqual(self.store.get_run(run_id)["status"], "running")
        with self.store.run_guard(run_id) as token:
            self.assertEqual(self.store.item(run_id,"unit",token), self.payload())
            self.store.finish(run_id,token,result={"entities":[self.payload()]})
        self.assertEqual(self.store.get_run(run_id)["status"], "completed")


if __name__ == "__main__":
    import unittest
    unittest.main()
