from datetime import date

from app.document_analysis.catalog import CatalogFilter, CatalogPolicy
from app.document_analysis.scope import ScopeResolver
from tests.document_analysis_support import AnalysisFixture, DIARY, NOTES


class ScopeResolverTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.service = self.catalog()
        self.snapshot = self.service.create_snapshot()
        self.resolver = ScopeResolver(self.service)

    def resolve(self, text, **kwargs):
        return self.resolver.resolve(text, self.snapshot.snapshot_id, today=kwargs.pop("today", date(2024, 1, 4)), **kwargs)

    def test_natural_directory_types_and_date_intersection(self):
        result = self.resolve("请分析 KnowledgeBase/documents/notes 目录下从2024-01-02到2024-01-03的 Markdown 和 TXT 文档")
        self.assertEqual(result.status, "resolved", result)
        self.assertEqual(result.scope.sources, (NOTES + "/alpha.md", NOTES + "/nested/beta.txt"))
        self.assertEqual(result.scope.filters.date_from, "2024-01-02")
        self.assertEqual(result.scope.filters.date_to, "2024-01-03")

    def test_known_directory_alias_and_explicit_filename(self):
        alias = self.resolve("分析 notes 目录下的所有 md 文件")
        exact = self.resolve("读取 alpha.md 文件")
        self.assertEqual(alias.status, "resolved", alias)
        self.assertEqual(alias.scope.sources, (NOTES + "/alpha.md",))
        self.assertEqual(exact.scope.sources, (NOTES + "/alpha.md",))

    def test_directory_context_and_filename_do_not_union_into_entire_directory(self):
        result = self.resolve("读取 notes 目录下的 alpha.md 文件")
        self.assertEqual(result.status, "resolved", result)
        self.assertEqual(result.scope.sources, (NOTES + "/alpha.md",))
        conflict = self.resolve("读取 private 目录下的 alpha.md 文件")
        self.assertEqual(conflict.status, "needs_clarification", conflict)

    def test_mixed_directory_and_file_without_clear_relationship_requires_clarification(self):
        result = self.resolve("分析 notes 目录和 private/secret.md 文件")
        self.assertEqual(result.status, "needs_clarification", result)

    def test_explicit_all_is_limited_by_snapshot_and_default_exclusions(self):
        result = self.resolve("分析所有文档")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(set(result.scope.sources), {d.source for d in self.snapshot.documents})
        self.assertNotIn(DIARY, result.scope.sources)
        limited = self.service.create_snapshot(CatalogFilter(directories=(NOTES,)))
        narrow = self.resolver.resolve("分析所有文档", limited.snapshot_id)
        self.assertTrue(all(source.startswith(NOTES + "/") for source in narrow.scope.sources))

    def test_relative_dates_are_deterministic_and_inclusive(self):
        for phrase, first, last in (("今天", "2024-01-04", "2024-01-04"), ("昨天", "2024-01-03", "2024-01-03"),
                                   ("最近3天", "2024-01-02", "2024-01-04"), ("本月", "2024-01-01", "2024-01-31")):
            with self.subTest(phrase=phrase):
                result = self.resolve("分析所有文档中" + phrase + "修改的文档")
                self.assertEqual(result.status, "resolved", result)
                self.assertEqual((result.scope.filters.date_from, result.scope.filters.date_to), (first, last))

    def test_chinese_month_and_leap_year_date_range(self):
        result = self.resolve("分析 notes 目录下2024年1月的全部文档")
        self.assertEqual(result.status, "resolved", result)
        self.assertEqual((result.scope.filters.date_from, result.scope.filters.date_to), ("2024-01-01", "2024-01-31"))
        no_matches = self.resolve("分析所有文档中2024年2月修改的文档")
        self.assertEqual(no_matches.status, "no_matches")
        self.assertIsNone(no_matches.scope)

    def test_exclusion_can_only_narrow_scope(self):
        result = self.resolve("分析所有文档，排除 private 目录")
        self.assertEqual(result.status, "resolved", result)
        self.assertFalse(any(source.startswith("private/") for source in result.scope.sources))
        without_docx = self.resolve("分析 notes 目录下的文档，排除 report.docx 文件")
        self.assertEqual(without_docx.status, "resolved", without_docx)
        self.assertNotIn(NOTES + "/report.docx", without_docx.scope.sources)

    def test_ambiguous_unknown_and_unrecognized_constraints_require_clarification(self):
        for text in ("分析一下", "分析重要文档", "分析所有文档但只包含关于Transformer的内容", "分析 notes 目录中大约去年修改的文件",
                     "分析 notes 目录下不是 Markdown 的文件", "分析所有PDF", "分析不存在的目录", "分析 notes 目录下最近修改的文件",
                     "分析 notes 目录下2024-01-01以后2024-01-04以前的文档", "分析所有文档，排除一些文件"):
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result.status, "needs_clarification", result)
                self.assertIsNone(result.scope)

    def test_traversal_absolute_hidden_and_explicit_excluded_paths_are_denied(self):
        for text in ("读取 ../secret.md", "读取 /etc/passwd", "读取 .ssh/id_rsa", "读取 " + DIARY,
                     "分析 KnowledgeBase/sqlite 目录", "读取 notes\\secret.md"):
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result.status, "denied", result)
                self.assertIsNone(result.scope)

    def test_same_basename_never_selects_multiple_documents_implicitly(self):
        self.write("PC_Files/Uploads/alpha.md", "同名文件")
        self.sync()
        self.snapshot = self.service.create_snapshot()
        result = self.resolve("读取 alpha.md 文件")
        self.assertEqual(result.status, "needs_clarification", result)
        self.assertEqual(result.code, "ambiguous_path")

    def test_structured_filter_and_permission_cannot_escape_snapshot(self):
        policy = CatalogPolicy(allowed_prefixes=(NOTES,))
        service = self.catalog(policy=policy)
        snapshot = service.create_snapshot()
        resolver = ScopeResolver(service)
        denied = resolver.resolve_filter(CatalogFilter(sources=("private/secret.md",)), snapshot.snapshot_id)
        self.assertEqual(denied.status, "denied")
        unknown = resolver.resolve_filter(CatalogFilter(sources=(NOTES + "/does-not-exist.md",)), snapshot.snapshot_id)
        self.assertEqual(unknown.status, "needs_clarification")
        allowed = resolver.resolve_filter(CatalogFilter(file_types=("txt",)), snapshot.snapshot_id)
        self.assertEqual(allowed.scope.sources, (NOTES + "/nested/beta.txt",))

    def test_conflicting_or_invalid_dates_are_not_ignored(self):
        for text in ("分析 notes 目录下2024-02-30的文档", "分析所有文档从2024-01-04到2024-01-01",
                     "分析所有文档今天和昨天修改的文件", "分析所有文档最近0天的文件"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text).status, "needs_clarification")

    def test_unhandled_negative_constraints_never_become_positive_selection(self):
        for text in ("分析 notes 目录以外的所有文档", "分析所有文档中2024年1月以外修改的文档",
                     "读取 alpha.md 外的所有文件", "分析 notes 目录下不是txt的文件"):
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result.status, "needs_clarification", result)

    def test_nested_directory_context_selects_intersection(self):
        result = self.resolve("分析 notes 目录下 nested 目录中的所有文档")
        self.assertEqual(result.status, "resolved", result)
        self.assertEqual(result.scope.sources, (NOTES + "/nested/beta.txt",))
        conflict = self.resolve("分析 notes 目录下 private 目录中的所有文档")
        self.assertEqual(conflict.status, "needs_clarification", conflict)

    def test_explicit_coordinated_directories_keep_union_instead_of_implying_nesting(self):
        result = self.resolve("分析 notes 和 nested 目录下的所有文档")
        self.assertEqual(result.status, "resolved", result)
        self.assertEqual(set(result.scope.sources), {NOTES + "/alpha.md", NOTES + "/nested/beta.txt", NOTES + "/report.docx"})

    def test_unknown_directory_names_cannot_become_file_type_or_date_constraints(self):
        for text in ("分析 md 目录下所有文件", "分析md目录下所有文件", "分析所有文档中2024-01-02目录的文件", "分析所有文档中 Markdown 目录的资料"):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text).status, "needs_clarification")

    def test_missing_source_date_requires_clarification_instead_of_silent_omission(self):
        (self.root / (NOTES + "/alpha.md")).unlink()
        self.snapshot = self.service.create_snapshot()
        result = self.resolve("分析 notes 目录下2024年1月修改的文档")
        self.assertEqual(result.status, "needs_clarification", result)
        self.assertEqual(result.code, "catalog_dates_unavailable")


if __name__ == "__main__":
    import unittest
    unittest.main()
