"""按明确范围读取完整解析正文；内存中至多保留一份文档正文。"""
from dataclasses import dataclass

from app.document_analysis.catalog import CatalogError, CatalogFilter, DocumentCatalog
from app.document_analysis.scope import ResolvedScope, ScopeResolver


@dataclass(frozen=True)
class DocumentBatch:
    source: str
    document_id: str
    sha256: str
    parser_version: str
    index: int
    start_char: int
    end_char: int
    total_chars: int
    text: str
    is_last: bool
    warnings: tuple[str, ...]


class ReadSession:
    """单次顺序迭代；应耗尽或通过 with/close 结束，以明确最终覆盖情况。"""
    def __init__(self, catalog: DocumentCatalog, entries: tuple, scope: ResolvedScope, batch_chars: int, policy_digest: str):
        self.catalog, self.entries, self.scope = catalog, entries, scope
        self.batch_chars = batch_chars
        self._policy_digest = policy_digest
        self._status = "not_started"
        self._completed = self._failed = self._emitted = self._verified = self._batches = 0
        self._errors = []
        self._closed = False
        self._iterator = self._run()

    @property
    def coverage(self):
        selected = len(self.entries)
        known = all(entry.content_chars is not None for entry in self.entries)
        return {"snapshot_id": self.scope.snapshot_id, "scope_id": self.scope.scope_id,
                "status": self._status, "complete": self._status == "completed",
                "selected_documents": selected, "completed_documents": self._completed,
                "failed_documents": self._failed, "pending_documents": selected - self._completed - self._failed,
                "emitted_characters": self._emitted, "verified_characters": self._verified,
                "expected_characters": sum(entry.content_chars for entry in self.entries) if known else None,
                "batch_count": self._batches, "coverage_ratio": self._completed / selected if selected else 0.0,
                "errors": [dict(error) for error in self._errors]}

    def _finish(self):
        if self._completed + self._failed == len(self.entries):
            self._status = "partial" if self._failed else "completed"
        else:
            self._status = "cancelled"

    def _check_permissions(self, source):
        policy = self.catalog.policy
        policy.require(source)
        if policy.digest != self._policy_digest:
            raise CatalogError("snapshot_policy_changed")

    def _run(self):
        self._status = "running"
        try:
            for entry in self.entries:
                emitted = 0
                try:
                    self._check_permissions(entry.source)
                    content = self.catalog.read_verified(entry)
                    for index, start in enumerate(range(0, len(content), self.batch_chars)):
                        self._check_permissions(entry.source)
                        end = min(start + self.batch_chars, len(content))
                        is_last = end == len(content)
                        # 最后一批输出前复查当前路径，捕捉原位修改和原子替换。
                        # 先前已发批次需以最终覆盖报告判断是否仍有效。
                        if is_last:
                            if self.catalog.source_state(entry.source)[0] != entry.sha256:
                                raise CatalogError("source_changed")
                        emitted += end - start
                        self._emitted += end - start
                        self._batches += 1
                        yield DocumentBatch(entry.source, entry.document_id, entry.sha256, entry.parser_version,
                                            index, start, end, len(content), content[start:end], is_last, entry.warnings)
                    # 最后一批被消费且迭代继续后才验证并计完成；close 不跳过此检查。
                    self._check_permissions(entry.source)
                    self.catalog.read_verified(entry)
                    self._completed += 1
                    self._verified += len(content)
                except CatalogError as exc:
                    self._failed += 1
                    self._errors.append({"source": entry.source, "code": exc.code, "emitted_characters": emitted})
        finally:
            self._finish()

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            return next(self._iterator)
        except StopIteration:
            self._closed = True
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            self._iterator.close()
            self._finish()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DocumentReader:
    def __init__(self, catalog: DocumentCatalog):
        self.catalog = catalog

    def open(self, scope: ResolvedScope, *, batch_chars=32768) -> ReadSession:
        if isinstance(batch_chars, bool) or not isinstance(batch_chars, int) or not 1 <= batch_chars <= 1_000_000:
            raise CatalogError("invalid_batch_size")
        if not isinstance(scope, ResolvedScope) or not isinstance(scope.filters, CatalogFilter):
            raise CatalogError("invalid_scope")
        # 不将 ResolvedScope 当作权限令牌；重新从持久化清单核对选择与当前策略。
        resolution = ScopeResolver(self.catalog).resolve_filter(scope.filters, scope.snapshot_id)
        if resolution.status != "resolved" or resolution.scope != scope:
            raise CatalogError("invalid_scope")
        policy_digest = self.catalog.policy.digest
        snapshot = self.catalog.get_snapshot(scope.snapshot_id)
        selected = set(scope.sources)
        entries = tuple(entry for entry in snapshot.documents if entry.source in selected)
        return ReadSession(self.catalog, entries, scope, batch_chars, policy_digest)
