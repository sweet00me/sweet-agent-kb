import os
from unittest.mock import patch

from app.document_analysis.catalog import CatalogError, CatalogPolicy
from app.document_analysis.entity_resolution import EntityResolver
from app.document_analysis.evidence_reduce import EvidenceReducer
from app.document_analysis.evidence_schema import ExtractorIdentity
from app.document_analysis.synthesis_schema import ReduceConfig, SemanticConclusion, SummaryResponse
from app.document_analysis.synthesis_store import SynthesisStore
from tests.synthesis_support import SynthesisFixture
from tests.document_analysis_support import NOTES

UID = "12345678-1234-4234-8234-123456789abc"


class Summarizer:
    identity = ExtractorIdentity(name="mock-reduce", version="1")

    def __init__(self):
        self.calls, self.fail_call, self.hook = [], None, None

    def summarize(self, request):
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        if len(self.calls) == self.fail_call:
            raise RuntimeError("private reduce failure")
        return SummaryResponse(complete=True, conclusions=[SemanticConclusion(
            text="原文包含测试记录。", evidence_ids=[request.evidence[0].evidence_id], uncertainty="仅概括当前证据批次。",
        )])


class EvidenceReduceTests(SynthesisFixture):
    def setUp(self):
        super().setUp()
        self.store = SynthesisStore(self.evidence)

    def resolve(self, tasks):
        result = EntityResolver(self.store).run(tasks)
        self.assertEqual(result["status"], "completed", result)
        return result["run_id"]

    def experiments(self, values, units=None, *, identified=True, fail_sources=(), version="1"):
        units = units or [None] * len(values)
        rows = {}
        for i,(value,unit) in enumerate(zip(values,units)):
            label = "实验A"
            marker = f"（全局ID: {UID}）" if identified else ""
            quote = f"{label}{marker}的学习率为{value}{unit or ''}。"
            rows[NOTES + f"/experiment{i}.md"] = (quote, {"experiment":label,"parameters":[{"name":"学习率","value":value,"unit":unit}]})
        return self.map_task(rows, "research_parameters", fail_sources=fail_sources, version=version)

    @staticmethod
    def metrics(result):
        return {row["metric"]:row["value"] for row in result["statistics"]}

    def test_same_original_evidence_from_two_tasks_is_counted_once_with_both_ids(self):
        first = self.experiments(["0.001"])
        second = self.experiments(["0.001"], version="2")
        result = EvidenceReducer(self.store).run(self.resolve([first,second]))
        self.assertEqual(result["status"], "completed", result)
        metrics = self.metrics(result["result"])
        self.assertEqual(metrics["input_records"], 2)
        self.assertEqual(metrics["unique_observations"], 1)
        self.assertEqual(metrics["duplicate_origin_records"], 1)
        claim = result["result"]["claims"][0]
        self.assertEqual(len(claim["evidence_ids"]), 2)
        self.assertEqual(len(self.store.get_origin(result["run_id"], claim["claim_id"])), 2)

    def test_cross_document_statement_dedup_uses_certain_entities_and_decimal_values(self):
        task = self.experiments(["0.001","1e-3"])
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertEqual(self.metrics(result)["unique_observations"], 2)
        self.assertEqual(self.metrics(result)["statement_groups"], 1)
        self.assertEqual(len(result["claims"][0]["evidence_ids"]), 2)
        self.assertEqual(result["comparisons"][0]["numeric_min"], "0.001")
        self.assertEqual(result["comparisons"][0]["numeric_max"], "0.001")
        self.assertFalse(result["comparisons"][0]["potential_conflict"])

    def test_parameter_disagreement_is_retained_and_numeric_statistics_are_python_computed(self):
        task = self.experiments(["0.001","0.002"])
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        comparison = result["comparisons"][0]
        self.assertTrue(comparison["potential_conflict"])
        self.assertEqual(comparison["numeric_min"], "0.001")
        self.assertEqual(comparison["numeric_max"], "0.002")
        self.assertEqual(len(comparison["evidence_ids"]), 2)
        self.assertIn("potential_evidence_conflict", result["uncertainties"])

    def test_different_units_are_not_silently_converted_or_compared(self):
        task = self.experiments(["1","1000"], ["s","ms"])
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        comparison = result["comparisons"][0]
        self.assertIsNone(comparison["numeric_min"])
        self.assertIsNone(comparison["numeric_max"])
        self.assertIn("incomparable_units", comparison["uncertainties"])

    def test_ambiguous_same_name_experiments_are_not_deduplicated_across_documents(self):
        task = self.experiments(["0.001","0.001"], identified=False)
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertEqual(self.metrics(result)["statement_groups"], 2)
        self.assertIn("unresolved_identity", result["uncertainties"])
        self.assertFalse(result["exhaustive"])

    def test_generic_fact_alternatives_keep_conflict_sources(self):
        task = self.map_task({
            NOTES + "/v1.md":(f"系统A（全局ID: {UID}）版本为1。", {"subject":"系统A","predicate":"版本","object":"1"}),
            NOTES + "/v2.md":(f"系统A（全局ID: {UID}）版本为2。", {"subject":"系统A","predicate":"版本","object":"2"}),
        }, "general_facts")
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertTrue(result["comparisons"][0]["potential_conflict"])
        self.assertEqual(len(result["comparisons"][0]["evidence_ids"]), 2)

    def test_timeline_uses_original_dates_and_retains_unknown_and_ungrounded_times(self):
        rows = {
            NOTES + "/late.md":("张三于2024-02-01测试。", {"people":["张三"],"event":"测试","time":"2024-02-01"}),
            NOTES + "/early.md":("李四于2024-01-01测试。", {"people":["李四"],"event":"测试","time":"2024-01-01"}),
            NOTES + "/unknown.md":("王五参与测试，日期不详。", {"people":["王五"],"event":"测试"}),
            NOTES + "/wrong.md":("赵六于2024-01-02测试。", {"people":["赵六"],"event":"测试","time":"2024-03-01"}),
        }
        task = self.map_task(rows)
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertEqual([item["date"] for item in result["timeline"]], ["2024-01-01","2024-02-01"])
        self.assertEqual(len(result["undated"]), 2)
        self.assertTrue(all(item["evidence_ids"] for item in result["timeline"] + result["undated"]))

    def test_partial_upstream_is_never_claimed_as_an_exhaustive_statistic(self):
        task = self.experiments(["0.001","0.002"], fail_sources=(NOTES + "/experiment1.md",))
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertTrue(result["upstream_incomplete"])
        self.assertFalse(result["exhaustive"])
        self.assertIn("upstream_incomplete", result["uncertainties"])
        self.assertTrue(all(not row["exhaustive"] and row["evidence_ids"] for row in result["statistics"]))

    def test_model_cannot_invent_evidence_ids_or_hide_incomplete_analysis(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        for complete in (True,False):
            backend = Summarizer()
            backend.summarize = lambda _: SummaryResponse(complete=complete, conclusions=[SemanticConclusion(
                text="陈述", evidence_ids=["invented"], uncertainty="未知",
            )])
            result = EvidenceReducer(self.store, backend).run(resolution)
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["result"])

    def test_model_may_not_claim_complete_statistics(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        backend = Summarizer()
        backend.summarize = lambda request: SummaryResponse(complete=True, conclusions=[SemanticConclusion(
            text="已经完整统计了所有文档，无遗漏。", evidence_ids=[request.evidence[0].evidence_id], uncertainty="未知",
        )])
        result = EvidenceReducer(self.store, backend).run(resolution)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "unsupported_completeness_claim")

    def test_semantic_failure_recovers_batches_and_does_not_change_exact_statistics(self):
        task = self.experiments(["0.001","0.002","0.003"])
        resolution = self.resolve([task])
        backend = Summarizer()
        backend.fail_call = 2
        reducer = EvidenceReducer(self.store, backend)
        config = ReduceConfig(semantic_batch_size=1)
        failed = reducer.run(resolution, config)
        self.assertEqual(failed["status"], "failed")
        backend.fail_call = None
        resumed = reducer.run(resolution, config)
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertGreater(resumed["run_statistics"]["skipped_units"], 0)
        self.assertEqual(len(backend.calls), 4)
        offline = EvidenceReducer(self.store).run(resolution)["result"]
        self.assertEqual(resumed["result"]["statistics"], offline["statistics"])
        self.assertFalse(any(row["semantic_verified"] for row in resumed["result"]["semantic_conclusions"]))
        self.assertTrue(reducer.run(resolution, config)["run_statistics"]["reused_result"])

    def test_source_change_during_summarization_cannot_publish_result(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        backend = Summarizer()
        backend.hook = lambda _: self.write(NOTES + "/experiment0.md", "文档已变化。")
        result = EvidenceReducer(self.store, backend).run(resolution)
        self.assertEqual(result["status"], "version_changed")
        self.assertIsNone(result["result"])

    def test_permission_revocation_blocks_reduce_and_provenance(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        self.service.policy = CatalogPolicy(allowed_prefixes=("private",))
        with self.assertRaises(CatalogError):
            EvidenceReducer(self.store).run(resolution)
        with self.assertRaises(CatalogError):
            self.store.get_run(resolution)

    def test_reduce_cloud_permission_is_independent_of_entity_and_map_switches(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        backend = Summarizer()
        backend.identity = ExtractorIdentity(name="mock-cloud-reduce", version="1", provider="deepseek")
        backend.authorized = True
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_ENTITY_LLM":"1", "DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1", "DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM":"0"}):
            with self.assertRaises(CatalogError):
                EvidenceReducer(self.store, backend).run(resolution)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_REDUCE_LLM":"1"}):
            self.assertEqual(EvidenceReducer(self.store, backend).run(resolution)["status"], "completed")

    def test_high_precision_decimal_values_are_not_rounded_into_one_statement(self):
        first = "0.123456789012345678901234567890"
        second = "0.123456789012345678901234567891"
        task = self.experiments([first, second])
        result = EvidenceReducer(self.store).run(self.resolve([task]))["result"]
        self.assertEqual(self.metrics(result)["statement_groups"], 2)
        comparison = result["comparisons"][0]
        self.assertTrue(comparison["potential_conflict"])
        self.assertEqual(comparison["numeric_min"], first.rstrip("0"))
        self.assertEqual(comparison["numeric_max"], second)

    def test_single_letter_subject_is_not_discarded_as_a_numeric_value(self):
        task = self.map_task({NOTES + "/symbol.md":("E的值为1。", {"subject":"E","predicate":"值","object":"1"})}, "general_facts")
        result = EvidenceReducer(self.store).run(self.resolve([task]))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.metrics(result["result"])["statement_groups"], 1)

    def test_reduce_cancel_and_keyboard_interrupt_do_not_publish_partial_results(self):
        task = self.experiments(["0.001","0.002","0.003"])
        resolution = self.resolve([task])
        backend = Summarizer()
        reducer = EvidenceReducer(self.store, backend)
        config = ReduceConfig(semantic_batch_size=1)
        cancelled = reducer.run(resolution,config,cancel=lambda:len(backend.calls)>=2)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["result"])
        resumed = reducer.run(resolution,config)
        self.assertEqual(resumed["status"], "completed")
        self.assertGreater(resumed["run_statistics"]["skipped_units"], 0)
        interrupted = Summarizer()
        interrupted.identity = ExtractorIdentity(name="interrupt-mock",version="1")
        def interrupt(_):
            raise KeyboardInterrupt()
        interrupted.hook = interrupt
        with self.assertRaises(KeyboardInterrupt):
            EvidenceReducer(self.store,interrupted).run(resolution,config)
        interrupted.hook = None
        self.assertEqual(EvidenceReducer(self.store,interrupted).run(resolution,config)["status"], "completed")

    def test_new_source_snapshot_produces_new_resolution_and_reduce_versions(self):
        task = self.experiments(["0.001"])
        resolution = self.resolve([task])
        first = EvidenceReducer(self.store).run(resolution)
        changed_task = self.experiments(["0.002"])
        with self.assertRaises(CatalogError):
            self.store.get_run(first["run_id"])
        second = EvidenceReducer(self.store).run(self.resolve([changed_task]))
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["result"]["comparisons"][0]["numeric_min"], "0.002")

    def test_no_evidence_does_not_produce_uncited_zero_count_conclusions(self):
        task = self.map_task({NOTES + "/nothing.md":("没有可提取证据。", None)})
        resolution = self.resolve([task])
        result = EvidenceReducer(self.store).run(resolution)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "no_available_evidence")
        self.assertIsNone(result["result"])


if __name__ == "__main__":
    import unittest
    unittest.main()
