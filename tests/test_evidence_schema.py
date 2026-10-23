from dataclasses import replace
import unittest
from pydantic import ValidationError

from app.document_analysis.evidence_schema import (
    CandidateEvidence, ExtractionResponse, ExtractionWindow, ExtractorIdentity,
    MapConfig, TEMPLATES, bind_evidence,
)
from app.document_analysis.catalog import CatalogError
from tests.document_analysis_support import AnalysisFixture, NOTES


class EvidenceSchemaTests(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.text = "标题😀\n张三在实验室进行测试。\n学习率为0.001，批次大小为32。\n"
        self.source = NOTES + "/alpha.md"
        self.write(self.source, self.text)
        self.sync()
        self.entry = self.catalog().list_documents().documents
        self.entry = next(item for item in self.entry if item.source == self.source)
        self.window = ExtractionWindow(self.source, self.entry.document_id, self.entry.sha256,
                                       0, 0, 0, len(self.text), len(self.text), self.text)

    def candidate(self, **kwargs):
        return CandidateEvidence(quote="张三在实验室进行测试。", conclusion="张三进行测试。", uncertainty="原文明确，未推断日期。",
                                 details={"people": ["张三"], "event": "进行测试"}, **kwargs)

    def test_three_typed_templates_are_available(self):
        self.assertEqual(set(TEMPLATES), {"people_events", "research_parameters", "general_facts"})
        self.assertTrue(all(template.version and template.details_schema for template in TEMPLATES.values()))

    def test_evidence_has_exact_source_version_unicode_position_and_original_quote(self):
        record = bind_evidence(self.candidate(), self.window, self.entry, self.text, "task-test", "people_events")
        self.assertEqual(record.source, self.source)
        self.assertEqual(record.document_sha256, self.entry.sha256)
        self.assertEqual(self.text[record.location.start_char:record.location.end_char], record.quote)
        self.assertEqual(record.location.start_line, 2)
        self.assertEqual(record.location.start_column, 1)
        self.assertEqual(record.location.basis, "parsed_text_unicode_v1")
        self.assertEqual(record.parsed_content_sha256, self.entry.content_sha256)
        self.assertTrue(record.uncertainty)

    def test_false_quote_wrong_offsets_and_ambiguous_quote_are_rejected(self):
        for candidate in (self.candidate(start_char=0, end_char=12),
                          CandidateEvidence(quote="原文不存在", conclusion="结论", uncertainty="未知", details={"subject":"甲","predicate":"是","object":"乙"})):
            with self.subTest(candidate=candidate), self.assertRaises(CatalogError):
                bind_evidence(candidate, self.window, self.entry, self.text, "task-test", "people_events")
        text = "事实。事实。"
        self.write(self.source, text)
        self.sync()
        entry = next(item for item in self.catalog().list_documents().documents if item.source == self.source)
        window = ExtractionWindow(self.source, entry.document_id, entry.sha256, 0, 0, 0, len(text), len(text), text)
        candidate = CandidateEvidence(quote="事实。", conclusion="事实", uncertainty="明确", details={"subject":"事实","predicate":"存在","object":"事实"})
        with self.assertRaises(CatalogError) as raised:
            bind_evidence(candidate, window, entry, text, "task-test", "general_facts")
        self.assertEqual(raised.exception.code, "ambiguous_quote_position")

    def test_duplicate_evidence_in_overlapping_windows_has_same_identity(self):
        first = bind_evidence(self.candidate(), self.window, self.entry, self.text, "task-test", "people_events")
        offset = self.text.index("张三")
        window = replace(self.window, index=1, start_char=offset, new_start_char=offset, text=self.text[offset:])
        second = bind_evidence(self.candidate(), window, self.entry, self.text, "task-test", "people_events")
        self.assertEqual(first.evidence_id, second.evidence_id)

    def test_experiment_and_generic_fields_are_checked_against_template(self):
        experiment = CandidateEvidence(quote="学习率为0.001，批次大小为32。", conclusion="采用学习率0.001。", uncertainty="未提供优化器。",
                                       details={"experiment":"测试", "parameters":[{"name":"学习率","value":"0.001","unit":None}]})
        record = bind_evidence(experiment, self.window, self.entry, self.text, "task-test", "research_parameters")
        self.assertEqual(record.details["parameters"][0]["value"], "0.001")
        with self.assertRaises(CatalogError):
            bind_evidence(experiment, self.window, self.entry, self.text, "task-test", "general_facts")

    def test_model_cannot_override_source_or_omit_uncertainty(self):
        for value in ({"quote":"原文","conclusion":"结论","details":{}},
                      {"quote":"原文","conclusion":"结论","uncertainty":"未知","details":{},"source":"../secret"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CandidateEvidence.model_validate(value)
        with self.assertRaises(ValidationError):
            ExtractionResponse.model_validate({"candidates":[]})

    def test_window_and_config_reject_invalid_boundaries_and_boolean_numbers(self):
        with self.assertRaises(CatalogError):
            replace(self.window, end_char=1000000)
        for kwargs in ({"batch_chars":0}, {"batch_chars":True}, {"overlap_chars":-1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CatalogError):
                MapConfig(**kwargs)
        with self.assertRaises(ValidationError):
            ExtractorIdentity(name="test", version="1", provider="unknown")


if __name__ == "__main__":
    unittest.main()
