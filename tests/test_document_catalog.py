import json
import os
import sqlite3
from unittest.mock import patch

from app.document_analysis.catalog import CatalogError, CatalogFilter, CatalogPolicy
from tests.document_analysis_support import AnalysisFixture, DIARY, NOTES


class DocumentCatalogTests(AnalysisFixture):
    def test_directory_type_and_inclusive_local_date_intersection(self):
        query = CatalogFilter(directories=(NOTES,), file_types=(".MD", "txt"),
                              date_from="2024-01-02", date_to="2024-01-03")
        result = self.catalog().list_documents(query)
        self.assertEqual([d.source for d in result.documents], [NOTES + "/alpha.md", NOTES + "/nested/beta.txt"])
        self.assertEqual(result.documents[0].modified_date, "2024-01-02", "日期使用上海时区，而非 UTC")
        self.assertTrue(all(d.status == "ready" and len(d.sha256) == 64 for d in result.documents))
        self.assertFalse(self.snapshots.exists(), "仅查询不得创建快照库")

    def test_catalog_uses_existing_full_parse_and_does_not_reparse(self):
        with patch("app.rag.document_loader.load_document", side_effect=AssertionError("must reuse cache")):
            result = self.catalog().list_documents(CatalogFilter(sources=(NOTES + "/report.docx",)))
        self.assertEqual(len(result.documents), 1)
        self.assertEqual(result.documents[0].file_type, "docx")
        self.assertGreater(result.documents[0].content_chars, 10)

    def test_excluded_diary_never_appears_even_with_empty_custom_exclusions(self):
        catalog = self.catalog(policy=CatalogPolicy(excluded_paths=()))
        result = catalog.list_documents()
        self.assertNotIn(DIARY, [d.source for d in result.documents])
        self.assertGreater(result.excluded_count, 0)
        with self.assertRaises(CatalogError) as error:
            catalog.list_documents(CatalogFilter(sources=(DIARY,)))
        self.assertEqual(error.exception.code, "access_denied")

    def test_trusted_allowlist_cannot_be_expanded_by_filter(self):
        catalog = self.catalog(policy=CatalogPolicy(allowed_prefixes=(NOTES,)))
        result = catalog.list_documents()
        self.assertTrue(all(d.source.startswith(NOTES + "/") for d in result.documents))
        with self.assertRaises(CatalogError) as error:
            catalog.list_documents(CatalogFilter(directories=("private",)))
        self.assertEqual(error.exception.code, "access_denied")

    def test_invalid_paths_dates_and_types_fail_closed(self):
        for path in ("../etc", "/etc/passwd", ".ssh/key", "notes//x", "notes\\x", "x\x00y"):
            with self.subTest(path=repr(path)), self.assertRaises(CatalogError):
                CatalogFilter(directories=(path,))
        for kwargs in ({"date_from": "2024-02-30"}, {"date_from": "2024-02-02", "date_to": "2024-01-01"},
                       {"file_types": ("pdf",)}, {"directories": NOTES}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CatalogError):
                CatalogFilter(**kwargs)

    def test_snapshot_is_deterministic_persistent_and_immutable_after_source_change(self):
        catalog = self.catalog()
        query = CatalogFilter(directories=(NOTES,))
        first = catalog.create_snapshot(query)
        second = catalog.create_snapshot(query)
        self.assertEqual(first, second)
        self.assertEqual(self.catalog().get_snapshot(first.snapshot_id), first)
        self.write(NOTES + "/alpha.md", "修改后的正文。")
        self.sync()
        self.assertEqual(catalog.get_snapshot(first.snapshot_id), first)
        self.assertNotEqual(catalog.create_snapshot(query).snapshot_id, first.snapshot_id)
        with sqlite3.connect(self.snapshots) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE catalog_snapshots SET manifest_json='{}' WHERE snapshot_id=?", (first.snapshot_id,))
        self.assertEqual(self.snapshots.stat().st_mode & 0o777, 0o600)

    def test_snapshot_integrity_and_policy_rechecked_on_load(self):
        snapshot = self.catalog().create_snapshot()
        with sqlite3.connect(self.snapshots) as connection:
            triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
            for (name,) in triggers:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute("UPDATE catalog_snapshots SET manifest_json='{}'")
            connection.commit()
        with self.assertRaises(CatalogError) as error:
            self.catalog().get_snapshot(snapshot.snapshot_id)
        self.assertEqual(error.exception.code, "snapshot_integrity_error")

    def test_policy_change_revokes_previously_created_snapshot(self):
        snapshot = self.catalog().create_snapshot()
        with self.assertRaises(CatalogError) as error:
            self.catalog(policy=CatalogPolicy(allowed_prefixes=(NOTES,))).get_snapshot(snapshot.snapshot_id)
        self.assertEqual(error.exception.code, "snapshot_policy_changed")

    def test_missing_stale_and_corrupt_documents_are_visible_as_failures(self):
        self.write(NOTES + "/alpha.md", "未同步的新版本。")
        (self.root / (NOTES + "/nested/beta.txt")).unlink()
        cache, _ = self.cached(NOTES + "/report.docx")
        cache.write_text("not JSON")
        result = self.catalog().list_documents(CatalogFilter(directories=(NOTES,)))
        self.assertEqual({d.source: d.status for d in result.documents}, {
            NOTES + "/alpha.md": "stale", NOTES + "/nested/beta.txt": "missing", NOTES + "/report.docx": "invalid_cache"})

    def test_source_and_cache_symlinks_cannot_escape_authorized_roots(self):
        source = self.root / (NOTES + "/alpha.md")
        source.unlink()
        source.symlink_to(self.root / "private/secret.md")
        cache, _ = self.cached(NOTES + "/nested/beta.txt")
        cache.unlink()
        cache.symlink_to(self.root / "private/secret.md")
        result = self.catalog().list_documents(CatalogFilter(directories=(NOTES,)))
        rows = {d.source: d for d in result.documents}
        self.assertEqual(rows[NOTES + "/alpha.md"].status, "access_denied")
        self.assertEqual(rows[NOTES + "/nested/beta.txt"].status, "invalid_cache")

    def test_sqlite_paths_are_validated_and_unregistered_files_are_not_silently_added(self):
        self.write(NOTES + "/new.md", "尚未同步")
        with sqlite3.connect(self.db) as connection:
            connection.execute("INSERT INTO parsed_documents VALUES (?,?,?,?,?)", ("../outside.md", "0" * 64, "v1", "/etc/passwd", "now"))
            connection.commit()
        view = self.catalog().list_documents()
        self.assertNotIn("../outside.md", [d.source for d in view.documents])
        self.assertNotIn(NOTES + "/new.md", [d.source for d in view.documents])
        self.assertEqual(view.invalid_count, 1)

    def test_malformed_unicode_cache_isolated_to_one_document(self):
        cache, record = self.cached(NOTES + "/alpha.md")
        record["content"], record["content_chars"] = "\ud800", 1
        cache.write_text(json.dumps(record, ensure_ascii=True))
        result = self.catalog().list_documents(CatalogFilter(directories=(NOTES,)))
        rows = {d.source: d for d in result.documents}
        self.assertEqual(rows[NOTES + "/alpha.md"].status, "invalid_cache")
        self.assertEqual(rows[NOTES + "/nested/beta.txt"].status, "ready")

    def test_database_symlinks_hardlinks_and_traversal_cannot_alias_existing_state(self):
        linked = self.db.parent / "linked.sqlite3"
        linked.symlink_to(self.db)
        with self.assertRaises(CatalogError):
            self.catalog(parsed_db=linked).list_documents()
        linked.unlink()
        os.link(self.db, linked)
        with self.assertRaises(CatalogError):
            self.catalog(snapshot_db=linked).create_snapshot()
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='catalog_snapshots'").fetchone())
        linked.unlink()
        with self.assertRaises(CatalogError):
            self.catalog(snapshot_db=self.db.parent / "other" / ".." / self.db.name).create_snapshot()

    def test_database_symlink_replacement_after_configuration_is_rejected(self):
        catalog = self.catalog()
        catalog.create_snapshot()
        self.snapshots.unlink()
        self.snapshots.symlink_to(self.db)
        with self.assertRaises(CatalogError):
            catalog.create_snapshot()
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='catalog_snapshots'").fetchone())

    def test_unknown_modification_dates_are_counted_in_date_filtered_catalog(self):
        (self.root / (NOTES + "/alpha.md")).unlink()
        result = self.catalog().list_documents(CatalogFilter(directories=(NOTES,), date_from="2024-01-01"))
        self.assertEqual(result.undated_count, 1)

    def test_invalid_policy_collections_cannot_silently_weaken_exclusions(self):
        for field in ("allowed_prefixes", "excluded_paths", "excluded_prefixes"):
            with self.subTest(field=field), self.assertRaises(CatalogError):
                CatalogPolicy(**{field: "private"})

    def test_snapshot_store_rejects_foreign_sqlite_tables(self):
        for table in ("unrelated_state", "sqlitex_other_state"):
            with self.subTest(table=table):
                foreign = self.db.parent / (table + ".sqlite3")
                with sqlite3.connect(foreign) as connection:
                    connection.execute('CREATE TABLE "' + table + '" (value TEXT)')
                with self.assertRaises(CatalogError) as error:
                    self.catalog(snapshot_db=foreign).create_snapshot()
                self.assertEqual(error.exception.code, "snapshot_database_not_dedicated")

    def test_database_replacement_during_sqlite_connect_is_detected_before_writing(self):
        catalog = self.catalog()
        catalog.create_snapshot()
        connect = sqlite3.connect
        changed = False
        def replace_on_connect(database, *args, **kwargs):
            nonlocal changed
            if "mode=rw" in str(database) and not changed:
                changed = True
                self.snapshots.rename(self.snapshots.with_suffix(".saved"))
                self.snapshots.symlink_to(self.db)
            return connect(database, *args, **kwargs)
        with patch("app.document_analysis.security.sqlite3.connect", side_effect=replace_on_connect):
            with self.assertRaises(CatalogError):
                catalog.create_snapshot()
        self.assertTrue(changed)
        with sqlite3.connect(self.db) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='catalog_snapshots'").fetchone())


if __name__ == "__main__":
    import unittest
    unittest.main()
