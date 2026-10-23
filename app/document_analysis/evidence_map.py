"""按授权文档执行可恢复的 Map；不创建 Agent 子图，不执行 Reduce。"""
import os
import re

from app.document_analysis.catalog import CatalogError, CatalogFilter
from app.document_analysis.evidence_schema import (
    ExtractionRequest, ExtractionResponse, ExtractionWindow, ExtractorIdentity, MapConfig, get_template,
)
from app.document_analysis.reader import DocumentReader
from app.document_analysis.scope import ScopeResolver


class _Cancelled(Exception):
    pass


def require_deepseek_authorization(authorized=False):
    """API Key 的存在不是分析授权；环境开关与显式授权必须同时存在。"""
    if authorized is not True or os.getenv("DOCUMENT_ANALYSIS_ALLOW_DEEPSEEK", "0") != "1":
        raise CatalogError("deepseek_analysis_not_authorized")


class EvidenceMap:
    def __init__(self, catalog, store, extractor):
        if store.catalog is not catalog or not isinstance(getattr(extractor, "identity", None), ExtractorIdentity):
            raise CatalogError("invalid_extractor_configuration")
        self.catalog, self.store, self.extractor = catalog, store, extractor

    def _authorize(self):
        if self.extractor.identity.provider == "deepseek":
            require_deepseek_authorization(getattr(self.extractor, "authorized", False))

    def run(self, scope, template_id, config: MapConfig = MapConfig(), *, task_id=None, cancel=None):
        self._authorize()
        template = get_template(template_id)
        task = self.store.create_task(scope, template_id, self.extractor.identity, config, task_id=task_id)
        task_id = task["task_id"]
        statistics = {"extractor_calls": 0, "skipped_documents": 0, "skipped_batches": 0,
                      "inserted_evidence": 0, "duplicate_evidence": 0}
        errors = []

        def check_cancel():
            if cancel is not None and cancel():
                raise _Cancelled()

        with self.store.run_guard(task_id) as token:
            def finish(status=None):
                code=errors[0] if len(set(errors))==1 else "map_documents_failed" if errors else None
                return self.store.finish_task(task_id, token, status=status) | {"run_statistics": dict(statistics),"error_code":code}

            try:
                snapshot = self.catalog.get_snapshot(scope.snapshot_id)
                entries = {entry.source: entry for entry in snapshot.documents}
                for source in scope.sources:
                    check_cancel()
                    try:
                        document = self.store.get_document(task_id, source)
                        # 即使整个文档已经完成，复用前仍检查当前来源及解析缓存。
                        self.catalog.read_verified(entries[source])
                        if document["status"] == "completed":
                            statistics["skipped_documents"] += 1
                            continue
                        self.store.begin_document(task_id, source, token)
                        single = ScopeResolver(self.catalog).resolve_filter(CatalogFilter(sources=(source,)), scope.snapshot_id)
                        if single.status != "resolved":
                            raise CatalogError("analysis_scope_unavailable")
                        tail = ""
                        with DocumentReader(self.catalog).open(single.scope, batch_chars=config.batch_chars) as session:
                            for batch in session:
                                check_cancel()
                                self._authorize()
                                window = ExtractionWindow(batch.source, batch.document_id, batch.sha256, batch.index,
                                                          batch.start_char - len(tail), batch.start_char, batch.end_char,
                                                          batch.total_chars, tail + batch.text)
                                if self.store.check_batch(task_id, source, window):
                                    statistics["skipped_batches"] += 1
                                else:
                                    statistics["extractor_calls"] += 1
                                    response = self.extractor.extract(ExtractionRequest(window, template))
                                    check_cancel()
                                    if not isinstance(response, ExtractionResponse):
                                        response = ExtractionResponse.model_validate(response)
                                    saved = self.store.save_batch(task_id, source, token, window, response)
                                    statistics["inserted_evidence"] += saved["inserted"]
                                    statistics["duplicate_evidence"] += saved["duplicates"]
                                tail = (tail + batch.text)[-config.overlap_chars:] if config.overlap_chars else ""
                            report = session.coverage
                        if not report["complete"]:
                            raise CatalogError(report["errors"][0]["code"] if report["errors"] else "reader_incomplete")
                        check_cancel()
                        self.store.complete_document(task_id, source, token)
                    except _Cancelled:
                        raise
                    except Exception as exc:
                        code = exc.code if isinstance(exc, CatalogError) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", exc.code) else "extraction_failed"
                        errors.append(code)
                        self.store.fail_document(task_id, source, token, code)
                check_cancel()
                return finish()
            except _Cancelled:
                return finish("cancelled")
