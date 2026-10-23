from app.document_analysis.catalog import CatalogFilter
from app.document_analysis.evidence_schema import (
    CandidateEvidence, ExtractionResponse, ExtractionWindow, ExtractorIdentity, MapConfig,
)
from app.document_analysis.scope import ScopeResolver
from tests.document_analysis_support import AnalysisFixture, NOTES


class EvidenceFixture(AnalysisFixture):
    def setUp(self):
        super().setUp()
        self.source = NOTES + "/alpha.md"
        self.text = "# 测试😀\n张三在实验室测试。\n学习率为0.001。\n地球绕太阳运行。\n"
        self.write(self.source, self.text)
        self.sync()
        self.service = self.catalog()
        self.snapshot = self.service.create_snapshot(CatalogFilter(sources=(self.source,)))
        self.scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), self.snapshot.snapshot_id).scope
        self.identity = ExtractorIdentity(name="fixture", version="1")
        self.config = MapConfig(batch_chars=32768, overlap_chars=0)
        self.entry = self.snapshot.documents[0]
        self.window = ExtractionWindow(self.source, self.entry.document_id, self.entry.sha256,
                                       0, 0, 0, len(self.text), len(self.text), self.text)

    def response(self, **kwargs):
        candidate = CandidateEvidence(quote="地球绕太阳运行。", conclusion="地球绕太阳运行。", uncertainty="仅记录原文陈述。",
                                      details={"subject":"地球", "predicate":"绕", "object":"太阳"})
        return ExtractionResponse(candidates=[candidate], complete=True, **kwargs)
