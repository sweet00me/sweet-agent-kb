"""派生分析的真实本地 Map/Store 夹具；仅替换模型边界。"""
from app.document_analysis.catalog import CatalogFilter
from app.document_analysis.evidence_map import EvidenceMap
from app.document_analysis.evidence_schema import CandidateEvidence, ExtractionResponse, ExtractorIdentity
from app.document_analysis.evidence_store import EvidenceStore
from app.document_analysis.scope import ScopeResolver
from tests.evidence_support import EvidenceFixture


class SynthesisFixture(EvidenceFixture):
    def setUp(self):
        super().setUp()
        self.evidence = EvidenceStore(self.service)

    def map_task(self, rows, template="people_events", *, fail_sources=(), version="1"):
        for source, (text, _) in rows.items():
            self.write(source, text)
        self.sync()
        snapshot = self.service.create_snapshot(CatalogFilter(sources=tuple(rows)))
        scope = ScopeResolver(self.service).resolve_filter(CatalogFilter(), snapshot.snapshot_id).scope

        class Extractor:
            identity = ExtractorIdentity(name="synthesis-fixture", version=version)

            def extract(_, request):
                source = request.window.source
                if source in fail_sources:
                    raise RuntimeError("mock extraction failed")
                text, details = rows[source]
                candidates = [] if details is None else [CandidateEvidence(
                    quote=text, conclusion="记录原文陈述。", uncertainty="未做语义核验。", details=details,
                )]
                return ExtractionResponse(candidates=candidates, complete=True)

        result = EvidenceMap(self.service, self.evidence, Extractor()).run(scope, template, self.config)
        return result["task_id"]
