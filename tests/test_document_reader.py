from dataclasses import replace
import json

from app.document_analysis.catalog import CatalogError, CatalogFilter, CatalogPolicy
from app.document_analysis.reader import DocumentReader
from app.document_analysis.scope import ScopeResolver
from tests.document_analysis_support import AnalysisFixture, DIARY, NOTES


class DocumentReaderTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.service = self.catalog()
        self.reader = DocumentReader(self.service)

    def scope(self, filters=None):
        snapshot = self.service.create_snapshot()
        result = ScopeResolver(self.service).resolve_filter(filters or CatalogFilter(directories=(NOTES,)), snapshot.snapshot_id)
        self.assertEqual(result.status, "resolved")
        return result.scope

    def test_reads_full_markdown_txt_docx_without_vector_top_k(self):
        scope = self.scope()
        session = self.reader.open(scope, batch_chars=7)
        batches = list(session)
        for source in scope.sources:
            pieces = [batch for batch in batches if batch.source == source]
            _, parsed = self.cached(source)
            self.assertEqual("".join(batch.text for batch in pieces), parsed["content"])
            self.assertEqual([batch.index for batch in pieces], list(range(len(pieces))))
            self.assertEqual(pieces[0].start_char, 0)
            self.assertEqual(pieces[-1].end_char, len(parsed["content"]))
            self.assertTrue(pieces[-1].is_last)
            self.assertTrue(all(a.end_char == b.start_char for a, b in zip(pieces, pieces[1:])))
        report = session.coverage
        self.assertTrue(report["complete"])
        self.assertEqual(report["completed_documents"], 3)
        self.assertEqual(report["pending_documents"], 0)
        self.assertEqual(report["emitted_characters"], report["verified_characters"])
        self.assertEqual(report["expected_characters"], report["verified_characters"])

    def test_long_unicode_document_has_exact_no_overlap_coverage(self):
        content = "# 长文\n" + "中文 😀 é Python\n" * 10000 + "最终段不能遗漏。"
        self.write(NOTES + "/long.md", content)
        self.sync()
        scope = self.scope(CatalogFilter(sources=(NOTES + "/long.md",)))
        with self.reader.open(scope, batch_chars=4096) as session:
            batches = list(session)
        self.assertGreater(len(batches), 10)
        self.assertEqual("".join(batch.text for batch in batches), content)
        self.assertEqual(session.coverage["verified_characters"], len(content))
        self.assertEqual(session.coverage["coverage_ratio"], 1.0)

    def test_changed_source_is_not_read_and_other_documents_continue(self):
        scope = self.scope()
        self.write(NOTES + "/alpha.md", "快照后改变了内容。")
        session = self.reader.open(scope)
        batches = list(session)
        self.assertNotIn(NOTES + "/alpha.md", [batch.source for batch in batches])
        self.assertEqual(session.coverage["completed_documents"], 2)
        self.assertEqual(session.coverage["failed_documents"], 1)
        self.assertEqual(session.coverage["errors"][0]["code"], "source_changed")
        self.assertFalse(session.coverage["complete"])

    def test_same_metadata_but_tampered_cache_body_is_rejected(self):
        scope = self.scope()
        cache, record = self.cached(NOTES + "/alpha.md")
        record["content"] = "被替换的缓存正文"
        record["content_chars"] = len(record["content"])
        cache.write_text(json.dumps(record, ensure_ascii=False))
        session = self.reader.open(scope)
        batches = list(session)
        self.assertNotIn(NOTES + "/alpha.md", [batch.source for batch in batches])
        self.assertEqual(session.coverage["errors"][0]["code"], "cache_changed")

    def test_missing_source_and_cache_and_broken_json_are_counted(self):
        scope = self.scope()
        (self.root / (NOTES + "/alpha.md")).unlink()
        cache, _ = self.cached(NOTES + "/nested/beta.txt")
        cache.unlink()
        cache, _ = self.cached(NOTES + "/report.docx")
        cache.write_text("{broken")
        session = self.reader.open(scope)
        self.assertEqual(list(session), [])
        self.assertEqual(session.coverage["failed_documents"], 3)
        self.assertEqual({item["code"] for item in session.coverage["errors"]}, {"source_missing", "cache_missing", "invalid_cache"})

    def test_symlink_replacement_after_snapshot_is_rejected(self):
        scope = self.scope()
        source = self.root / (NOTES + "/alpha.md")
        source.unlink()
        source.symlink_to(self.root / "private/secret.md")
        session = self.reader.open(scope)
        self.assertNotIn("PRIVATE_BODY", "".join(batch.text for batch in session))
        self.assertEqual(session.coverage["errors"][0]["code"], "access_denied")

    def test_invalid_scope_cannot_add_documents_or_change_filters(self):
        scope = self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",)))
        for sources in ((DIARY,), ("private/secret.md",), scope.sources * 2, (), ("../secret.md",)):
            with self.subTest(sources=sources), self.assertRaises(CatalogError):
                self.reader.open(replace(scope, sources=sources))
        with self.assertRaises(CatalogError):
            self.reader.open(replace(scope, filters=CatalogFilter(file_types=("txt",))))

    def test_permission_change_before_or_during_iteration_is_enforced(self):
        scope = self.scope()
        self.service.policy = CatalogPolicy(allowed_prefixes=("PC_Files/Uploads",))
        with self.assertRaises(CatalogError):
            self.reader.open(scope)
        self.service.policy = CatalogPolicy()
        session = self.reader.open(scope)
        self.service.policy = CatalogPolicy(allowed_prefixes=("PC_Files/Uploads",))
        self.assertEqual(list(session), [])
        self.assertEqual(session.coverage["failed_documents"], 3)

    def test_early_close_preserves_incomplete_coverage_and_can_release_session(self):
        session = self.reader.open(self.scope(), batch_chars=7)
        self.assertEqual(session.coverage["status"], "not_started")
        first = next(session)
        self.assertGreater(len(first.text), 0)
        session.close()
        session.close()
        self.assertFalse(session.coverage["complete"])
        self.assertEqual(session.coverage["status"], "cancelled")
        self.assertGreater(session.coverage["pending_documents"], 0)
        self.assertEqual(list(session), [])

    def test_source_change_between_batches_never_counts_as_verified_document(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))), batch_chars=7)
        first = next(session)
        self.write(first.source, "读取过程中文档发生变化。")
        list(session)
        self.assertEqual(session.coverage["completed_documents"], 0)
        self.assertEqual(session.coverage["verified_characters"], 0)
        self.assertEqual(session.coverage["failed_documents"], 1)
        self.assertEqual(session.coverage["errors"][0]["code"], "source_changed")
        self.assertFalse(session.coverage["complete"])

    def test_unready_catalog_entries_are_not_silently_omitted(self):
        self.write(NOTES + "/alpha.md", "未同步的新正文。")
        scope = self.scope()
        session = self.reader.open(scope)
        list(session)
        self.assertEqual(session.coverage["selected_documents"], 3)
        self.assertEqual(session.coverage["failed_documents"], 1)
        self.assertIsNone(session.coverage["expected_characters"])

    def test_batch_size_validation(self):
        scope = self.scope()
        for size in (0, -1, True, 1.5, 1000001):
            with self.subTest(size=size), self.assertRaises(CatalogError):
                self.reader.open(scope, batch_chars=size)

    def test_permission_revocation_stops_the_very_next_intermediate_batch(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))), batch_chars=3)
        next(session)
        self.service.policy = CatalogPolicy(allowed_prefixes=("private",))
        self.assertEqual(list(session), [])
        self.assertEqual(session.coverage["completed_documents"], 0)
        self.assertEqual(session.coverage["errors"][0]["code"], "access_denied")

    def test_cache_change_during_iteration_is_rejected_at_final_verification(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))), batch_chars=3)
        next(session)
        cache, _ = self.cached(NOTES + "/alpha.md")
        cache.write_text("{}")
        list(session)
        self.assertFalse(session.coverage["complete"])
        self.assertEqual(session.coverage["completed_documents"], 0)
        self.assertEqual(session.coverage["errors"][0]["code"], "cache_changed")

    def test_final_batch_does_not_credit_completion_until_iteration_finishes(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))))
        last = next(session)
        self.assertTrue(last.is_last)
        self.assertEqual(session.coverage["completed_documents"], 0)
        self.write(last.source, "最后一批之后发生变化。")
        list(session)
        self.assertFalse(session.coverage["complete"])
        self.assertEqual(session.coverage["failed_documents"], 1)
        self.assertEqual(session.coverage["errors"][0]["code"], "source_changed")

    def test_closing_after_final_yield_does_not_skip_verification(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))))
        next(session)
        session.close()
        self.assertFalse(session.coverage["complete"])
        self.assertEqual(session.coverage["completed_documents"], 0)
        self.assertEqual(session.coverage["status"], "cancelled")

    def test_policy_change_also_invalidates_inflight_snapshot_when_source_remains_allowed(self):
        session = self.reader.open(self.scope(CatalogFilter(sources=(NOTES + "/alpha.md",))), batch_chars=3)
        next(session)
        self.service.policy = CatalogPolicy(allowed_prefixes=(NOTES,))
        self.assertEqual(list(session), [])
        self.assertFalse(session.coverage["complete"])
        self.assertEqual(session.coverage["errors"][0]["code"], "snapshot_policy_changed")


if __name__ == "__main__":
    import unittest
    unittest.main()
