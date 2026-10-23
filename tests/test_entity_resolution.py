import os
from unittest.mock import patch

from app.document_analysis.catalog import CatalogError
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_schema import ExtractorIdentity
from app.document_analysis.synthesis_schema import RelationSuggestion, ResolutionConfig
from app.document_analysis.synthesis_store import SynthesisStore
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import DIARY, NOTES

ORCID = "0000-0002-1825-0097"


class Assistant:
    identity = ExtractorIdentity(name="mock-entity", version="1")

    def __init__(self):
        self.calls, self.fail_call, self.hook = [], None, None

    def resolve(self, request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        if len(self.calls) == self.fail_call:
            raise RuntimeError("private model failure")
        return RelationSuggestion(complete=True, relation="same", rationale="模型认为有关联，但未核验。",
                                  evidence_ids=sorted({record.evidence_id for record in request.evidence}))


class EntityResolutionTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.store = SynthesisStore(self.evidence)

    def people(self, labels, texts=None, *, version="1", fail_sources=()):
        texts = texts or [label + "参与测试。" for label in labels]
        return self.map_task({NOTES + f"/entity{i}.md":(text, {"people":[label],"event":"测试"})
                              for i,(label,text) in enumerate(zip(labels,texts))},
                             version=version, fail_sources=fail_sources)

    def test_rules_support_all_four_states_without_merging_homonyms_or_pronouns(self):
        task = self.people(["张三","Zhang San","王五","王五","他","他","李四"], [
            f"张三（ORCID: {ORCID}）参与测试。", f"Zhang San（ORCID: {ORCID}）参与测试。",
            "王五参与测试。", "王五参与测试。", "他参与测试。", "他参与测试。",
            "李四（ORCID: 0000-0001-5109-3700）参与测试。",
        ])
        response = EntityResolver(self.store).run([task])
        self.assertEqual(response["status"], "completed", response)
        result = response["result"]
        self.assertEqual({row["relation"] for row in result["relations"]}, {"same","possible_same","unknown","different"})
        self.assertEqual(len(result["entities"]), 6)
        self.assertFalse(result["semantic_verified"])
        self.assertTrue(result["source_location_verified"])
        pronoun = next(item for item in result["mentions"] if item["label"] == "他")
        self.assertTrue(pronoun["pronoun"])
        self.assertFalse(any(row["automatic_merge"] for row in result["relations"] if pronoun["mention_id"] in (row["left"],row["right"])))

    def test_duplicate_original_mentions_across_map_tasks_keep_all_sources(self):
        first = self.people(["张三"])
        second = self.people(["张三"], version="2")
        result = EntityResolver(self.store).run([second, first])["result"]
        self.assertEqual(len(result["entities"]), 1)
        entity = result["entities"][0]
        self.assertEqual(len(entity["evidence_ids"]), 2)
        self.assertEqual(len(self.store.get_origin(EntityResolver(self.store).run([first,second])["run_id"], entity["entity_id"])), 2)

    def test_explicit_alias_can_link_mentions_in_one_quote(self):
        task = self.map_task({NOTES + "/alias.md":("张三（又名阿三）参与测试。", {"people":["张三","阿三"],"event":"测试"})})
        result = EntityResolver(self.store).run([task])["result"]
        self.assertEqual(len(result["entities"]), 1)
        self.assertIn("explicit_alias_in_quote", result["relations"][0]["rationale"])

    def test_transitive_same_component_with_explicit_difference_does_not_merge(self):
        quote = f"甲（ORCID: {ORCID}）、乙（ORCID: {ORCID}）和丙（ORCID: {ORCID}）；甲与丙不是同一人。"
        task = self.map_task({NOTES + "/conflict.md":(quote, {"people":["甲","乙","丙"],"event":"测试"})})
        result = EntityResolver(self.store).run([task])["result"]
        self.assertEqual(len(result["entities"]), 3)
        self.assertTrue(any(row["confidence_status"] == "conflicted" for row in result["relations"]))
        self.assertFalse(any(row["automatic_merge"] for row in result["relations"]))
        self.assertIn("identity_conflict", result["uncertainties"])

    def test_ungrounded_structured_name_is_not_treated_as_a_verified_person(self):
        task = self.people(["张三","张三"], ["有人参与测试。","有人参与测试。"])
        result = EntityResolver(self.store).run([task])["result"]
        self.assertFalse(any(row["grounded"] for row in result["mentions"]))
        self.assertEqual(result["relations"], [])
        self.assertTrue(all(group["identity_status"]=="unresolved" for group in result["entities"]))
        self.assertEqual(len(result["entities"]), 2)

    def test_model_same_is_only_a_suggestion_and_never_an_automatic_merge(self):
        task = self.people(["张三","张三"])
        result = EntityResolver(self.store, Assistant()).run([task])["result"]
        self.assertEqual(result["relations"][0]["relation"], "possible_same")
        self.assertEqual(result["relations"][0]["confidence_status"], "model_suggestion")
        self.assertFalse(result["relations"][0]["automatic_merge"])
        self.assertEqual(len(result["entities"]), 2)

    def test_invalid_model_sources_or_incomplete_response_fail_without_result(self):
        task = self.people(["张三","张三"])
        for response in (RelationSuggestion(complete=True, relation="same", rationale="无效来源", evidence_ids=["invented"]),
                         RelationSuggestion(complete=False, relation="unknown", rationale="未完成", evidence_ids=["invented"])):
            backend = Assistant()
            backend.resolve = lambda _: response
            result = EntityResolver(self.store, backend).run([task])
            self.assertEqual(result["status"], "failed", result)
            self.assertIsNone(result["result"])

    def test_failure_and_cancellation_resume_committed_pairs(self):
        task = self.people(["张三","张三","张三"])
        backend = Assistant()
        backend.fail_call = 2
        resolver = EntityResolver(self.store, backend)
        failed = resolver.run([task])
        self.assertEqual(failed["status"], "failed")
        backend.fail_call = None
        resumed = resolver.run([task])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["run_id"], failed["run_id"])
        self.assertGreater(resumed["run_statistics"]["skipped_units"], 0)
        self.assertEqual(len(backend.calls), 4)
        self.assertTrue(resolver.run([task])["run_statistics"]["reused_result"])
        other = Assistant()
        other.identity = ExtractorIdentity(name="cancel-mock", version="1")
        cancelled = EntityResolver(self.store, other).run([task], cancel=lambda: len(other.calls) >= 2)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(EntityResolver(self.store, other).run([task])["status"], "completed")

    def test_version_change_during_model_call_and_after_completion_is_detected(self):
        task = self.people(["张三","张三"])
        backend = Assistant()
        backend.hook = lambda _: self.write(NOTES + "/entity0.md", "已修改。")
        result = EntityResolver(self.store, backend).run([task])
        self.assertEqual(result["status"], "version_changed")
        self.assertIsNone(result["result"])
        with self.assertRaises(CatalogError):
            self.store.get_run(result["run_id"])

    def test_partial_upstream_and_forced_diary_exclusion_are_preserved(self):
        task = self.people(["张三","李四"], fail_sources=(NOTES + "/entity1.md",))
        result = EntityResolver(self.store).run([task])["result"]
        self.assertTrue(result["upstream_incomplete"])
        self.assertIn("upstream_incomplete", result["uncertainties"])
        for row in result["mentions"]:
            for evidence_id in row["evidence_ids"]:
                self.assertNotEqual(self.evidence.get_origin(evidence_id)["evidence"]["source"], DIARY)

    def test_cloud_assistant_requires_independent_environment_and_explicit_authorization(self):
        task = self.people(["张三","张三"])
        backend = Assistant()
        backend.identity = ExtractorIdentity(name="mock-cloud-entity", version="1", provider="deepseek")
        backend.authorized = True
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1", "DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM":"0"}):
            with self.assertRaises(CatalogError):
                EntityResolver(self.store, backend).run([task])
        self.assertFalse(self.store.path.exists())
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM":"1"}):
            backend.authorized = False
            with self.assertRaises(CatalogError):
                EntityResolver(self.store, backend).run([task])
            backend.authorized = True
            self.assertEqual(EntityResolver(self.store, backend).run([task])["status"], "completed")

    def test_entity_limit_fails_instead_of_silently_truncating(self):
        task = self.people(["张三","李四"])
        result = EntityResolver(self.store).run([task], ResolutionConfig(max_mentions=1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "entity_mention_limit")

    def test_recovered_upstream_task_requires_new_synthesis_input_version(self):
        first_task = self.people(["张三","李四"], fail_sources=(NOTES + "/entity1.md",))
        first = EntityResolver(self.store).run([first_task])
        recovered_task = self.people(["张三","李四"])
        self.assertEqual(first_task, recovered_task)
        with self.assertRaises(CatalogError) as raised:
            self.store.get_run(first["run_id"])
        self.assertEqual(raised.exception.code, "synthesis_input_changed")
        recovered = EntityResolver(self.store).run([recovered_task])
        self.assertNotEqual(first["run_id"], recovered["run_id"])
        self.assertFalse(recovered["result"]["upstream_incomplete"])

    def test_pair_limit_and_cloud_revocation_fail_closed(self):
        task = self.people(["张三","张三","张三"])
        capped = EntityResolver(self.store).run([task], ResolutionConfig(max_pairs=1))
        self.assertEqual(capped["status"], "failed")
        self.assertEqual(capped["error_code"], "entity_pair_limit")
        backend = Assistant()
        backend.identity = ExtractorIdentity(name="revoked-cloud",version="1",provider="deepseek")
        backend.authorized = True
        backend.hook = lambda _: os.environ.__setitem__("DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM", "0")
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM":"1"}):
            result = EntityResolver(self.store,backend).run([task])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["result"])
        self.assertEqual(len(backend.calls), 1)

    def test_alias_substring_does_not_create_a_false_automatic_merge(self):
        alias_task = self.map_task({NOTES + "/alias-boundary.md":(
            "张三又名李四海；李四参加会议。", {"people":["张三","李四"],"event":"参加会议"})})
        alias_result = EntityResolver(self.store).run([alias_task])["result"]
        self.assertEqual(len(alias_result["entities"]), 2)
        self.assertEqual(alias_result["relations"], [])

    def test_identifier_name_substring_does_not_create_a_false_automatic_merge(self):
        identifier_task = self.people(["Ann","Anne"], [
            f"Ann 与 JoAnn（ORCID: {ORCID}）参与测试。", f"Anne（ORCID: {ORCID}）参与测试。",
        ])
        identifier_result = EntityResolver(self.store).run([identifier_task])["result"]
        ann = next(item for item in identifier_result["mentions"] if item["label"] == "Ann")
        self.assertEqual(ann["identifiers"], {})
        self.assertEqual(identifier_result["relations"], [])

    def test_first_and_second_person_pronouns_remain_unresolved_across_repeated_origins(self):
        first = self.people(["我们","你","I"])
        second = self.people(["我们","你","I"], version="2")
        result = EntityResolver(self.store).run([first,second])["result"]
        self.assertTrue(all(item["pronoun"] for item in result["mentions"]))
        self.assertFalse(any(row["automatic_merge"] for row in result["relations"]))
        self.assertTrue(all(item["identity_status"] == "unresolved" for item in result["entities"]))

    def test_transitive_conflict_relations_include_the_third_document_basis(self):
        task = self.people(["甲","乙","丙"], [
            f"甲（ORCID: {ORCID}）。甲（全局ID: 12345678-1234-4234-8234-123456789abc）测试。",
            f"乙（ORCID: {ORCID}）测试。",
            f"丙（ORCID: {ORCID}）。丙（全局ID: 22345678-1234-4234-8234-123456789abc）测试。",
        ])
        response = EntityResolver(self.store).run([task])
        result = response["result"]
        adjusted = [row for row in result["relations"] if "inconsistent_same_component" in row["rationale"]]
        self.assertEqual(len(adjusted), 2)
        self.assertTrue(all(len(row["evidence_ids"]) == 3 for row in adjusted))
        for row in adjusted:
            self.assertEqual(len(self.store.get_origin(response["run_id"],row["relation_id"])), 3)


if __name__ == "__main__":
    import unittest
    unittest.main()
