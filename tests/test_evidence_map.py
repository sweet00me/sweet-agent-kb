import subprocess
import sys
from unittest.mock import patch

from app.document_analysis.catalog import CatalogFilter, CatalogPolicy
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, MapConfig
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.evidence_map import EvidenceMap
from app.document_analysis.scope import ScopeResolver
from tests.evidence_support import EvidenceFixture
from tests.document_analysis_support import DIARY, NOTES


class MockExtractor:
    def __init__(self, identity):
        self.identity, self.calls, self.fail_index, self.hook = identity, [], None, None

    def extract(self, request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        if request.window.index == self.fail_index:
            raise RuntimeError("PRIVATE_PROVIDER_DETAILS")
        targets = {
            "people_events": ("张三在实验室测试。", {"people":["张三"], "event":"测试"}),
            "research_parameters": ("学习率为0.001。", {"experiment":"测试", "parameters":[{"name":"学习率","value":"0.001","unit":None}]}),
            "general_facts": ("地球绕太阳运行。", {"subject":"地球","predicate":"绕","object":"太阳"}),
        }
        quote, details = targets[request.template.template_id]
        start = request.window.text.find(quote)
        candidates = [] if start < 0 else [CandidateEvidence(quote=quote, conclusion=quote, uncertainty="只记录原文，未补充背景。",
                                                             details=details, start_char=start, end_char=start+len(quote))]
        return ExtractionResponse(candidates=candidates, complete=True)


class EvidenceMapTests(EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.store = EvidenceStore(self.service)
        self.extractor = MockExtractor(self.identity)
        self.mapper = EvidenceMap(self.service, self.store, self.extractor)

    def run_map(self, template="general_facts", config=None, **kwargs):
        return self.mapper.run(self.scope, template, config or self.config, **kwargs)

    def test_all_templates_produce_traceable_verified_evidence(self):
        for template in ("people_events", "research_parameters", "general_facts"):
            with self.subTest(template=template):
                result = self.run_map(template)
                self.assertEqual(result["status"], "completed", result)
                records = self.store.list_evidence(result["task_id"])
                self.assertEqual(len(records), 1)
                self.assertEqual(self.store.get_origin(records[0]["evidence"]["evidence_id"])["version_status"], "verified")

    def test_overlap_handles_quote_crossing_reader_batch_boundary_and_deduplicates(self):
        self.text += "额外的尾部内容。" * 6
        self.write(self.source, self.text)
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source,)))
        self.scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        result = self.run_map(config=MapConfig(batch_chars=12, overlap_chars=20))
        self.assertEqual(result["status"], "completed", result)
        records = self.store.list_evidence(result["task_id"])
        self.assertEqual(len(records), 1)
        self.assertTrue(any(request.window.start_char < request.window.new_start_char for request in self.extractor.calls))
        self.assertGreater(result["run_statistics"]["duplicate_evidence"], 0)
        record = records[0]["evidence"]
        self.assertEqual(self.text[record["location"]["start_char"]:record["location"]["end_char"]], record["quote"])

    def test_repeated_task_skips_unchanged_completed_document_without_extractor_calls(self):
        first = self.run_map()
        count = len(self.extractor.calls)
        repeated = self.run_map()
        self.assertEqual(repeated["task_id"], first["task_id"])
        self.assertEqual(repeated["status"], "completed")
        self.assertEqual(len(self.extractor.calls), count)
        self.assertEqual(repeated["run_statistics"]["skipped_documents"], 1)

    def test_failure_recovery_reuses_successful_batches_and_never_publishes_partial_evidence(self):
        config = MapConfig(batch_chars=12, overlap_chars=20)
        self.extractor.fail_index = 1
        failed = self.run_map(config=config)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.store.list_evidence(failed["task_id"]), [])
        self.extractor.fail_index = None
        completed = self.run_map(config=config)
        self.assertEqual(completed["task_id"], failed["task_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(sum(request.window.index == 0 for request in self.extractor.calls), 1)
        self.assertEqual(completed["run_statistics"]["skipped_batches"], 1)

    def test_cancelled_run_preserves_checkpoints_and_is_resumable(self):
        config = MapConfig(batch_chars=12, overlap_chars=20)
        cancelled = self.run_map(config=config, cancel=lambda: len(self.extractor.calls) >= 2)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.list_evidence(cancelled["task_id"]), [])
        resumed = self.run_map(config=config)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["run_statistics"]["skipped_batches"], 1)

    def test_keyboard_interrupt_does_not_mark_task_or_document_successful(self):
        def interrupt(_):
            raise KeyboardInterrupt()
        self.extractor.hook = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_map()
        task = self.store.create_task(self.scope, "general_facts", self.identity, self.config)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(self.store.get_document(task["task_id"], self.source)["status"], "interrupted")
        self.extractor.hook = None
        self.assertEqual(self.run_map()["status"], "completed")

    def test_model_reports_incomplete_analysis_and_fabricated_quotes_fail_closed(self):
        for response in (ExtractionResponse(candidates=[], complete=False),
                         ExtractionResponse(candidates=[CandidateEvidence(quote="虚构引用",conclusion="结论",uncertainty="未知",
                             details={"subject":"甲","predicate":"是","object":"乙"})], complete=True)):
            with self.subTest(response=response):
                self.extractor.extract = lambda _: response
                result = self.run_map()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.store.list_evidence(result["task_id"]), [])

    def test_source_or_cache_change_during_extraction_prevents_promotion(self):
        self.extractor.hook = lambda _: self.write(self.source, "读取期间改变了文档。")
        result = self.run_map()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.get_document(result["task_id"], self.source)["status"], "version_changed")
        self.assertEqual(self.store.list_evidence(result["task_id"]), [])

    def test_completed_task_detects_new_source_version_and_new_snapshot_creates_new_task(self):
        first = self.run_map()
        count = len(self.extractor.calls)
        self.write(self.source, self.text + "新增内容。")
        stale = self.run_map()
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(len(self.extractor.calls), count)
        self.assertEqual(self.store.list_evidence(first["task_id"]), [])
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source,)))
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        refreshed = self.mapper.run(scope, "general_facts", self.config)
        self.assertNotEqual(refreshed["task_id"], first["task_id"])
        self.assertEqual(refreshed["status"], "completed")

    def test_scope_exclusions_are_preserved_across_multiple_documents(self):
        snapshot = self.service.create_snapshot()
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        result = self.mapper.run(scope, "general_facts", self.config)
        self.assertEqual(result["status"], "completed")
        self.assertNotIn(DIARY, [request.window.source for request in self.extractor.calls])
        self.assertEqual(result["counts"]["total_documents"], len(scope.sources))

    def test_permission_revocation_during_model_call_invalidates_output(self):
        self.extractor.hook = lambda _: setattr(self.service, "policy", CatalogPolicy(allowed_prefixes=("private",)))
        result = self.run_map()
        self.assertEqual(result["status"], "failed")
        self.service.policy = CatalogPolicy()
        self.assertEqual(self.store.list_evidence(result["task_id"]), [])

    def test_long_document_processes_all_batches_without_top_k_limit(self):
        self.write(self.source, self.text + "无关长段落。\n" * 500 + "地球绕太阳运行。")
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source,)))
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        result = self.mapper.run(scope, "general_facts", MapConfig(batch_chars=256, overlap_chars=32))
        self.assertEqual(result["status"], "completed")
        self.assertGreater(len(self.extractor.calls), 10)
        records = self.store.list_evidence(result["task_id"])
        self.assertEqual(len(records), 2)

    def test_task_finalization_rechecks_earlier_documents_and_cached_bodies(self):
        second_source = NOTES + "/nested/beta.txt"
        self.write(second_source, self.text)
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source, second_source)))
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        cache_path, _ = self.cached(self.source)
        original_cache = cache_path.read_bytes()
        for changed_part in ("source", "cache"):
            with self.subTest(changed_part=changed_part):
                self.write(self.source, self.text)
                cache_path.write_bytes(original_cache)
                def change_earlier_document(request):
                    if request.window.source == second_source:
                        if changed_part == "source":
                            self.write(self.source, "处理下一篇时，第一篇发生变化。")
                        else:
                            cache_path.write_text("{}", encoding="utf-8")
                self.extractor.hook = change_earlier_document
                result = self.mapper.run(scope, "general_facts", self.config,
                                         task_id="final-sweep-" + changed_part)
                self.assertEqual(result["status"], "partial", result)
                self.assertEqual(self.store.get_document(result["task_id"], self.source)["status"], "version_changed")
                records = self.store.list_evidence(result["task_id"])
                self.assertEqual([record["evidence"]["source"] for record in records], [second_source])

    def test_recovery_promotes_previously_saved_evidence_once_after_failure(self):
        config = MapConfig(batch_chars=24, overlap_chars=8)
        self.extractor.fail_index = 1
        failed = self.run_map("people_events", config=config)
        self.assertEqual(failed["status"], "failed")
        saved = self.store.list_evidence(failed["task_id"], include_unverified=True)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["state"], "invalidated")
        self.extractor.fail_index = None
        resumed = self.run_map("people_events", config=config)
        self.assertEqual(resumed["status"], "completed")
        records = self.store.list_evidence(resumed["task_id"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["evidence"]["evidence_id"], saved[0]["evidence"]["evidence_id"])
        self.assertEqual(sum(request.window.index == 0 for request in self.extractor.calls), 1)

    def test_reader_failure_after_last_committed_batch_does_not_verify_evidence(self):
        save = self.store.save_batch
        def save_then_change(*args):
            result = save(*args)
            self.write(self.source, "最后一批提交后原文件改变。")
            return result
        with patch.object(self.store, "save_batch", side_effect=save_then_change):
            result = self.run_map()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.list_evidence(result["task_id"]), [])
        self.assertEqual(self.store.list_evidence(result["task_id"], include_unverified=True)[0]["state"], "invalidated")

    def test_cancellation_after_last_batch_does_not_skip_document_finalization_on_resume(self):
        save = self.store.save_batch
        cancelled = False
        def save_then_cancel(*args):
            nonlocal cancelled
            result = save(*args)
            cancelled = True
            return result
        with patch.object(self.store, "save_batch", side_effect=save_then_cancel):
            result = self.run_map(cancel=lambda: cancelled)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.list_evidence(result["task_id"]), [])
        resumed = self.run_map()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["run_statistics"]["skipped_batches"], 1)
        self.assertEqual(len(self.extractor.calls), 1)
        self.assertEqual(len(self.store.list_evidence(result["task_id"])), 1)

    def test_abrupt_process_exit_releases_lock_and_resumes_committed_checkpoint(self):
        config = MapConfig(batch_chars=12, overlap_chars=20)
        task = self.store.create_task(self.scope, "general_facts", self.identity, config)
        script = """
import os, socket, sys
def deny_network(*args, **kwargs):
    raise AssertionError('offline child attempted network')
socket.socket.connect = deny_network
socket.getaddrinfo = deny_network
from app.document_analysis.catalog import DocumentCatalog
from app.document_analysis.evidence_schema import ExtractionResponse, ExtractionWindow
from app.document_analysis.evidence_store import EvidenceStore
catalog = DocumentCatalog(sys.argv[1])
store = EvidenceStore(catalog)
task_id, source = sys.argv[2:4]
_, _, _, entries = store._context(task_id)
entry = entries[source]
text = catalog.read_verified(entry)
with store.run_guard(task_id) as token:
    store.begin_document(task_id, source, token)
    window = ExtractionWindow(source, entry.document_id, entry.sha256, 0, 0, 0, 12, len(text), text[:12])
    store.save_batch(task_id, source, token, window, ExtractionResponse(candidates=[], complete=True))
    os._exit(71)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root), task["task_id"], self.source],
                                capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 71, result.stderr)
        self.assertEqual(self.store.get_task(task["task_id"])["status"], "running")
        resumed = self.run_map(config=config)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["run_statistics"]["skipped_batches"], 1)
        self.assertFalse(any(request.window.index == 0 for request in self.extractor.calls))
        self.assertEqual(self.store.get_document(task["task_id"], self.source)["attempts"], 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
