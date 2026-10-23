from dataclasses import replace
import os
import sqlite3

from app.document_analysis.catalog import CatalogError, CatalogPolicy
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, ExtractorIdentity, MapConfig
from app.document_analysis.evidence_store import EvidenceStore
from tests.evidence_support import EvidenceFixture


class EvidenceStoreTests(EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.store = EvidenceStore(self.service)

    def task(self, **kwargs):
        return self.store.create_task(self.scope, "general_facts", self.identity, self.config, **kwargs)

    def test_schema_is_separate_and_task_creation_is_idempotent(self):
        self.assertFalse(self.store.path.exists())
        first, second = self.task(), self.task()
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["counts"]["total_documents"], 1)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"analysis_tasks", "analysis_documents", "analysis_batches", "analysis_evidence"})
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_explicit_task_id_cannot_be_reused_for_another_definition(self):
        task = self.task(task_id="explicit-task")
        with self.assertRaises(CatalogError):
            self.store.create_task(self.scope, "people_events", self.identity, self.config, task_id=task["task_id"])
        other = self.store.create_task(self.scope, "general_facts", ExtractorIdentity(name="fixture", version="2"), self.config)
        self.assertNotEqual(other["task_id"], task["task_id"])

    def test_incremental_evidence_hidden_until_whole_document_is_verified(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            saved = self.store.save_batch(task, self.source, token, self.window, self.response())
            self.assertEqual(saved["inserted"], 1)
            self.assertEqual(self.store.list_evidence(task), [])
            provisional = self.store.list_evidence(task, include_unverified=True)
            self.assertEqual(provisional[0]["state"], "provisional")
            self.store.complete_document(task, self.source, token)
            self.store.finish_task(task, token)
        evidence = self.store.list_evidence(task)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["state"], "verified")
        self.assertEqual(self.store.get_task(task)["status"], "completed")

    def test_duplicate_batch_and_evidence_are_skipped_without_extra_records(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            self.store.save_batch(task, self.source, token, self.window, self.response())
            duplicate = self.store.save_batch(task, self.source, token, self.window, self.response())
            self.assertTrue(duplicate["skipped"])
            self.assertEqual(len(self.store.list_evidence(task, include_unverified=True)), 1)
            self.store.complete_document(task, self.source, token)
            self.store.finish_task(task, token)

    def test_origin_lookup_by_task_document_and_evidence_id(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            self.store.save_batch(task, self.source, token, self.window, self.response())
            self.store.complete_document(task, self.source, token)
            self.store.finish_task(task, token)
        document = self.store.get_document(task, self.entry.document_id)
        self.assertEqual(document["source"], self.source)
        records = self.store.list_evidence(task, document_id=self.entry.document_id)
        origin = self.store.get_origin(records[0]["evidence"]["evidence_id"])
        self.assertEqual(origin["version_status"], "verified")
        record = origin["evidence"]
        self.assertEqual(record["source"], self.source)
        self.assertEqual(self.text[record["location"]["start_char"]:record["location"]["end_char"]], record["quote"])

    def test_missing_batches_or_false_quotes_cannot_complete_document(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            with self.assertRaises(CatalogError):
                self.store.complete_document(task, self.source, token)
            invalid = CandidateEvidence(quote="伪造引用", conclusion="错误", uncertainty="未知", details={"subject":"甲","predicate":"是","object":"乙"})
            with self.assertRaises(CatalogError):
                self.store.save_batch(task, self.source, token, self.window, ExtractionResponse(candidates=[invalid], complete=True))
            self.assertEqual(self.store.list_evidence(task, include_unverified=True), [])
        self.assertEqual(self.store.get_task(task)["status"], "interrupted")

    def test_atomic_batch_failure_rolls_back_both_evidence_and_checkpoint(self):
        task = self.task()["task_id"]
        with sqlite3.connect(self.store.path) as db:
            db.execute("CREATE TRIGGER reject_batch BEFORE INSERT ON analysis_batches BEGIN SELECT RAISE(ABORT,'reject'); END")
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            with self.assertRaises(CatalogError):
                self.store.save_batch(task, self.source, token, self.window, self.response())
            self.assertFalse(self.store.check_batch(task, self.source, self.window))
            self.assertEqual(self.store.list_evidence(task, include_unverified=True), [])

    def test_version_change_detected_before_save_and_on_historical_origin_lookup(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            self.store.save_batch(task, self.source, token, self.window, self.response())
            self.store.complete_document(task, self.source, token)
            self.store.finish_task(task, token)
        evidence_id = self.store.list_evidence(task)[0]["evidence"]["evidence_id"]
        self.write(self.source, "已更新的文档。")
        self.assertEqual(self.store.get_origin(evidence_id)["version_status"], "source_changed")
        with self.assertRaises(CatalogError):
            self.store.check_batch(task, self.source, self.window)

    def test_permission_revocation_blocks_queries_and_forged_scope(self):
        task = self.task()["task_id"]
        with self.assertRaises(CatalogError):
            self.store.create_task(replace(self.scope, sources=("private/secret.md",)), "general_facts", self.identity, self.config)
        self.service.policy = CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError):
            self.store.list_evidence(task, include_unverified=True)

    def test_store_rejects_other_databases_and_aliases(self):
        with self.assertRaises(CatalogError):
            EvidenceStore(self.service, path=self.service.parsed_db).create_task(self.scope, "general_facts", self.identity, self.config)
        with self.assertRaises(CatalogError):
            EvidenceStore(self.service, path=self.service.snapshot_db).create_task(self.scope, "general_facts", self.identity, self.config)
        alias = self.db.parent / "evidence-alias.sqlite3"
        os.link(self.db, alias)
        with self.assertRaises(CatalogError):
            EvidenceStore(self.service, path=alias).create_task(self.scope, "general_facts", self.identity, self.config)

    def test_concurrent_run_is_rejected_and_unfinished_run_is_recoverable(self):
        task = self.task()["task_id"]
        with self.store.run_guard(task) as token:
            self.store.begin_document(task, self.source, token)
            with self.assertRaises(CatalogError) as error:
                with self.store.run_guard(task):
                    pass
            self.assertEqual(error.exception.code, "analysis_busy")
        self.assertEqual(self.store.get_task(task)["status"], "interrupted")
        reopened = EvidenceStore(self.service)
        with reopened.run_guard(task) as token:
            reopened.begin_document(task, self.source, token)
            reopened.save_batch(task, self.source, token, self.window, self.response())
            reopened.complete_document(task, self.source, token)
            reopened.finish_task(task, token)
        self.assertEqual(reopened.get_document(task, self.source)["attempts"], 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
