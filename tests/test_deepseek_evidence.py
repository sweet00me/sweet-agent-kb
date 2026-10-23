import json
import os
from unittest.mock import patch

from langchain_core.messages import AIMessage

from app.document_analysis.catalog import CatalogError, CatalogFilter
from app.document_analysis.deepseek_evidence import DeepSeekEvidenceExtractor
from app.document_analysis.evidence_schema import ExtractionRequest, ExtractionResponse, MapConfig, TEMPLATES, bind_evidence
from app.document_analysis.evidence_map import EvidenceMap
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.scope import ScopeResolver
from app.document_analysis.synthesis_store import SynthesisStore
from app.document_analysis.workflow import AnalysisWorkflow
from tests.evidence_support import EvidenceFixture
from tests.test_evidence_verification import Validator


class DeepSeekEvidenceTests(EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.finish_reason = "stop"
        fixture = self
        class Model:
            def invoke(self, messages):
                fixture.calls.append(messages)
                return AIMessage(content=json.dumps(fixture.response().model_dump(mode="json"), ensure_ascii=False),
                                 response_metadata={"finish_reason": fixture.finish_reason})
        self.model = Model()
        self.request = ExtractionRequest(self.window, TEMPLATES["general_facts"])

    def test_default_closed_even_when_chat_api_key_exists(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"unused-test", "DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"0"}):
            extractor = DeepSeekEvidenceExtractor(model=self.model)
            with self.assertRaises(CatalogError):
                extractor.extract(self.request)
        self.assertEqual(self.calls, [])

    def test_environment_switch_and_explicit_authorization_are_both_required(self):
        for enabled, authorized in (("0",False), ("0",True), ("1",False)):
            with self.subTest(enabled=enabled, authorized=authorized), patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":enabled}):
                with self.assertRaises(CatalogError):
                    DeepSeekEvidenceExtractor(model=self.model, authorized=authorized).extract(self.request)
        self.assertEqual(self.calls, [])

    def test_authorized_mock_model_gets_template_and_json_but_no_tool_execution(self):
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1"}):
            result = DeepSeekEvidenceExtractor(model=self.model, authorized=True).extract(self.request)
        self.assertTrue(result.complete)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("JSON", self.calls[0][0].content)
        self.assertIn("general_facts", self.calls[0][0].content)
        self.assertIn(self.text, json.loads(self.calls[0][1].content)["document_text"])

    def test_truncated_filtered_or_unfinished_responses_are_rejected(self):
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1"}):
            for reason in ("length", "content_filter", "tool_calls", None):
                with self.subTest(reason=reason):
                    self.finish_reason = reason
                    with self.assertRaises(CatalogError):
                        DeepSeekEvidenceExtractor(model=self.model, authorized=True).extract(self.request)

    def test_map_does_not_create_or_start_cloud_task_without_authorization(self):
        store = EvidenceStore(self.service)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"0"}):
            mapper = EvidenceMap(self.service, store, DeepSeekEvidenceExtractor(model=self.model, authorized=True))
            with self.assertRaises(CatalogError):
                mapper.run(self.scope, "general_facts")
        self.assertFalse(store.path.exists())
        self.assertEqual(self.calls, [])

    def test_switch_revocation_prevents_subsequent_requests(self):
        extractor = DeepSeekEvidenceExtractor(model=self.model, authorized=True)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK":"1"}):
            extractor.extract(self.request)
            os.environ["DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK"] = "0"
            with self.assertRaises(CatalogError):
                extractor.extract(self.request)
        self.assertEqual(len(self.calls), 1)

    def test_unique_verbatim_quote_corrects_inaccurate_model_offsets_without_another_request(self):
        original = self.response()
        candidate = original.candidates[0]
        start = self.text.index(candidate.quote)
        end = start + len(candidate.quote)
        extractor = DeepSeekEvidenceExtractor(model=self.model, authorized=True)
        for bad_start, bad_end in ((0, 1), (start + 1, end + 1), (start, end - 1),
                                   (start, len(self.text) + 100), (end, start)):
            with self.subTest(offsets=(bad_start, bad_end)):
                self.response = lambda: original.model_copy(update={"candidates": [
                    candidate.model_copy(update={"start_char": bad_start, "end_char": bad_end})]})
                before = len(self.calls)
                with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}):
                    result = extractor.extract(self.request)
                record = bind_evidence(result.candidates[0], self.window, self.entry,
                                       self.text, "test-task", "general_facts")
                self.assertEqual((record.location.start_char, record.location.end_char), (start, end))
                self.assertEqual(self.text[start:end], record.quote)
                self.assertEqual(record.document_sha256, self.entry.sha256)
                self.assertEqual(len(self.calls), before + 1)

    def test_inaccurate_offsets_do_not_allow_missing_or_ambiguous_quotes(self):
        original = self.response()
        for quote in ("这段引文不存在", "。"):
            with self.subTest(quote=quote):
                self.response = lambda: original.model_copy(update={"candidates": [
                    original.candidates[0].model_copy(update={"quote": quote, "start_char": 0, "end_char": 1})]})
                store = EvidenceStore(self.service)
                mapper = EvidenceMap(self.service, store,
                                     DeepSeekEvidenceExtractor(model=self.model, authorized=True))
                with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}):
                    result = mapper.run(self.scope, "general_facts", self.config)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(store.get_document(result["task_id"], self.source)["error_code"],
                                 "quote_position_mismatch")
                self.assertEqual(store.get_batches(result["task_id"], self.source), [])
                self.assertEqual(store.list_evidence(result["task_id"], include_unverified=True), [])

    def test_correct_explicit_offsets_still_disambiguate_repeated_quotes(self):
        original = self.response()
        start = self.text.index("。")
        self.response = lambda: original.model_copy(update={"candidates": [
            original.candidates[0].model_copy(update={"quote": "。", "start_char": start, "end_char": start + 1})]})
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}):
            result = DeepSeekEvidenceExtractor(model=self.model, authorized=True).extract(self.request)
        record = bind_evidence(result.candidates[0], self.window, self.entry,
                               self.text, "test-task", "general_facts")
        self.assertEqual((record.location.start_char, record.location.end_char), (start, start + 1))

    def test_invalid_json_schema_is_rejected_before_quote_rebinding(self):
        valid = self.response().model_dump(mode="json")
        missing_field = json.loads(json.dumps(valid))
        del missing_field["candidates"][0]["uncertainty"]
        unpaired_offsets = json.loads(json.dumps(valid))
        unpaired_offsets["candidates"][0]["start_char"] = 0
        for payload in (missing_field, unpaired_offsets, {"complete": True, "candidates": "invalid"}):
            with self.subTest(payload_keys=list(payload)), \
                    patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}), \
                    patch.object(self.model, "invoke", return_value=AIMessage(
                        content=json.dumps(payload), response_metadata={"finish_reason": "stop"})) as invoke:
                with self.assertRaises(CatalogError) as error:
                    DeepSeekEvidenceExtractor(model=self.model, authorized=True).extract(self.request)
                self.assertEqual(error.exception.code, "invalid_model_response")
                invoke.assert_called_once()

    def test_quote_rebinding_uses_unicode_window_offsets_across_overlapping_batches(self):
        self.text += "额外的尾部内容。" * 6
        self.write(self.source, self.text)
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source,)))
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope
        original = self.response()
        candidate = original.candidates[0]
        def respond(messages):
            self.calls.append(messages)
            payload = json.loads(messages[1].content)
            start = payload["document_text"].find(candidate.quote)
            # Simulate a model confusing document-global and window-relative positions.
            candidates = [] if start < 0 else [candidate.model_copy(update={
                "start_char": payload["window_start_char"] + start,
                "end_char": payload["window_start_char"] + start + len(candidate.quote)})]
            response = ExtractionResponse(complete=True, candidates=candidates)
            return AIMessage(content=response.model_dump_json(), response_metadata={"finish_reason": "stop"})
        store = EvidenceStore(self.service)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}), \
                patch.object(self.model, "invoke", side_effect=respond):
            result = EvidenceMap(self.service, store, DeepSeekEvidenceExtractor(
                model=self.model, authorized=True)).run(scope, "general_facts", MapConfig(batch_chars=10, overlap_chars=20))
        self.assertEqual(result["status"], "completed", result)
        self.assertGreater(result["run_statistics"]["duplicate_evidence"], 0)
        records = store.list_evidence(result["task_id"])
        self.assertEqual(len(records), 1)
        record = records[0]["evidence"]
        self.assertEqual(record["location"]["start_char"], self.text.index(candidate.quote))
        self.assertEqual(self.text[record["location"]["start_char"]:record["location"]["end_char"]], candidate.quote)
        self.assertEqual(store.get_origin(record["evidence_id"])["version_status"], "verified")

    def test_quote_rebinding_does_not_bypass_document_version_validation(self):
        original = self.response()
        self.response = lambda: original.model_copy(update={"candidates": [
            original.candidates[0].model_copy(update={"start_char": 0, "end_char": 1})]})
        invoke = self.model.invoke
        def change_source(messages):
            response = invoke(messages)
            self.write(self.source, self.text + "文件版本已变化。")
            return response
        store = EvidenceStore(self.service)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1"}), \
                patch.object(self.model, "invoke", side_effect=change_source):
            result = EvidenceMap(self.service, store, DeepSeekEvidenceExtractor(
                model=self.model, authorized=True)).run(self.scope, "general_facts", self.config)
        self.assertEqual(result["status"], "failed")
        document = store.get_document(result["task_id"], self.source)
        self.assertEqual(document["status"], "version_changed")
        self.assertEqual(document["error_code"], "source_changed")
        self.assertEqual(store.get_batches(result["task_id"], self.source), [])
        self.assertEqual(store.list_evidence(result["task_id"], include_unverified=True), [])

    def test_failed_two_document_workflow_resumes_with_verified_positions_and_no_duplicate_calls(self):
        second = self.source.replace("alpha.md", "second.md")
        self.write(second, "# 第二篇😀\n" + self.text)
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source, second)))
        original = self.response()
        bad_offsets = original.model_copy(update={"candidates": [
            original.candidates[0].model_copy(update={"start_char": 0, "end_char": 1})]})
        self.response = lambda: bad_offsets
        extractor = DeepSeekEvidenceExtractor(model=self.model, authorized=True)
        evidence = EvidenceStore(self.service)
        workflow = AnalysisWorkflow(self.service, extractor, store=SynthesisStore(evidence),
                                    validator=Validator(), cloud_authorized=True)
        with patch.dict(os.environ, {"DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK": "1",
                                     "DOCUMENT_ANALYSIS_ALLOW_WORKFLOW_LLM": "1"}):
            # Reproduce the previous adapter, which passed model offsets through unchanged.
            with patch.object(extractor, "extract", return_value=bad_offsets):
                failed = workflow.run("整理原文事实", "分析全部文档", snapshot_id=snapshot.snapshot_id)
            self.assertEqual(failed["status"], "partial")
            task_id = failed["result"]["task_ids"][0]
            self.assertEqual(failed["result"]["coverage"]["documents"]["failed"], 2)
            self.assertEqual(failed["result"]["coverage"]["body"]["processing_ratio"], 0.0)
            for source in (self.source, second):
                self.assertEqual(evidence.get_document(task_id, source)["error_code"], "quote_position_mismatch")
                self.assertEqual(evidence.get_batches(task_id, source), [])
            recovered = workflow.resume(failed["run_id"])
            # A complete pipeline must still retain uncertainty about cross-document identity.
            self.assertEqual(recovered["status"], "partial")
            self.assertTrue(recovered["result"]["coverage"]["pipeline_complete"])
            self.assertEqual(recovered["result"]["coverage"]["uncertainties"], ["unresolved_identity"])
            self.assertTrue(all(stage["status"] == "completed" for stage in recovered["result"]["stages"].values()))
            self.assertEqual(recovered["run_id"], failed["run_id"])
            self.assertEqual(recovered["result"]["task_ids"], [task_id])
            self.assertEqual(recovered["result"]["coverage"]["documents"]["analyzed"], 2)
            self.assertEqual(recovered["result"]["coverage"]["body"]["processing_ratio"], 1.0)
            self.assertFalse(recovered["result"]["coverage"]["facts_exhaustive"])
            self.assertEqual(len(self.calls), 2)
            records = evidence.list_evidence(task_id)
            self.assertEqual(len(records), 2)
            for item in records:
                record = item["evidence"]
                origin = evidence.get_origin(record["evidence_id"])
                self.assertEqual(origin["version_status"], "verified")
                text = self.service.read_verified(next(entry for entry in snapshot.documents
                                                       if entry.source == record["source"]))
                self.assertEqual(text[record["location"]["start_char"]:record["location"]["end_char"]],
                                 record["quote"])
                self.assertEqual(evidence.get_document(task_id, record["source"])["attempts"], 2)
            self.assertEqual(workflow.resume(failed["run_id"])["status"], "partial")
            self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
