from app.document_analysis.catalog import CatalogError, CatalogPolicy
from app.document_analysis.evidence_input import load_input
from app.document_analysis.synthesis_schema import RelationSuggestion, SummaryResponse
from pydantic import ValidationError
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES


class SynthesisInputTests(SynthesisFixture):
    def make_task(self, **kwargs):
        return self.map_task({
            NOTES + "/a.md": ("张三进行测试。", {"people":["张三"], "event":"测试"}),
            NOTES + "/b.md": ("李四进行测试。", {"people":["李四"], "event":"测试"}),
        }, **kwargs)

    def test_input_is_stable_sorted_and_keeps_original_provenance_without_semantic_verification(self):
        task_id = self.make_task()
        value = load_input(self.evidence, [task_id, task_id])
        repeated = load_input(self.evidence, (task_id,))
        self.assertEqual(value.digest, repeated.digest)
        self.assertEqual(len(value.evidence), 2)
        self.assertFalse(value.upstream_incomplete)
        self.assertTrue(value.source_location_verified)
        self.assertFalse(value.semantic_verified)
        for record in value.evidence:
            self.assertEqual(self.evidence.get_origin(record.evidence_id)["version_status"], "verified")

    def test_partial_task_exposes_only_successful_document_evidence_and_preserves_uncertainty(self):
        task_id = self.make_task(fail_sources=(NOTES + "/b.md",))
        value = load_input(self.evidence, [task_id])
        self.assertTrue(value.upstream_incomplete)
        self.assertEqual(len(value.evidence), 1)
        self.assertEqual(value.tasks[0]["status"], "partial")

    def test_changed_zero_evidence_document_is_not_missed(self):
        task_id = self.map_task({NOTES + "/empty.md": ("没有可提取人物。", None)})
        self.assertEqual(len(load_input(self.evidence, [task_id]).evidence), 0)
        self.write(NOTES + "/empty.md", "零证据文档也改变了。")
        with self.assertRaises(CatalogError) as raised:
            load_input(self.evidence, [task_id])
        self.assertEqual(raised.exception.code, "source_changed")

    def test_permission_revocation_blocks_evidence_input(self):
        task_id = self.make_task()
        self.service.policy = CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError):
            load_input(self.evidence, [task_id])

    def test_assistant_cannot_set_verified_flags_or_omit_completion(self):
        with self.assertRaises(ValidationError):
            RelationSuggestion.model_validate({"relation":"same", "rationale":"猜测", "evidence_ids":["a"], "semantic_verified":True, "complete":True})
        with self.assertRaises(ValidationError):
            SummaryResponse.model_validate({"conclusions":[]})

    def test_invalid_input_tasks_are_rejected(self):
        for task_ids in ([], "not-a-list", [None]):
            with self.subTest(task_ids=task_ids), self.assertRaises(CatalogError):
                load_input(self.evidence, task_ids)


if __name__ == "__main__":
    import unittest
    unittest.main()
