"""将有限、明确的自然语言条件解析为固定清单子集；不调用模型。"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
import re

from app.document_analysis.catalog import (
    CatalogError, CatalogFilter, DocumentCatalog, date_value, fingerprint, within,
)
from app.document_analysis.security import checked_path


@dataclass(frozen=True)
class ResolvedScope:
    snapshot_id: str
    sources: tuple[str, ...]
    filters: CatalogFilter

    @property
    def scope_id(self):
        return fingerprint({"snapshot": self.snapshot_id, "sources": self.sources,
                            "filters": self.filters.__dict__})


@dataclass(frozen=True)
class ScopeResolution:
    status: str
    code: str
    scope: ResolvedScope | None = None


class ScopeResolver:
    def __init__(self, catalog: DocumentCatalog):
        self.catalog = catalog

    @staticmethod
    def _failure(error: CatalogError):
        denied = {"access_denied", "invalid_path", "snapshot_policy_changed"}
        return ScopeResolution("denied" if error.code in denied else "needs_clarification", error.code)

    def resolve_filter(self, filters: CatalogFilter, snapshot_id: str) -> ScopeResolution:
        """精确入口；所有目录/文件必须属于指定快照，不能访问快照之外。"""
        try:
            snapshot = self.catalog.get_snapshot(snapshot_id)
            self.catalog.check_filter(filters)
            sources = {entry.source for entry in snapshot.documents}
            if snapshot.undated_count:
                raise CatalogError("catalog_dates_unavailable")
            if filters.date_from or filters.date_to:
                without_dates = CatalogFilter(filters.directories, filters.sources, filters.file_types, exclude=filters.exclude)
                if any(entry.modified_date is None and without_dates.matches(entry) for entry in snapshot.documents):
                    raise CatalogError("catalog_dates_unavailable")
            if any(source not in sources for source in filters.sources):
                raise CatalogError("unknown_path")
            for directory in filters.directories:
                if directory and not any(within(source, directory) for source in sources):
                    raise CatalogError("unknown_path")
            for excluded in filters.exclude:
                if excluded not in self.catalog.policy.excluded_paths and not any(within(source, excluded) for source in sources):
                    raise CatalogError("unknown_path")
            selected = tuple(entry.source for entry in snapshot.documents if filters.matches(entry))
            if not selected:
                return ScopeResolution("no_matches", "empty_selection")
            return ScopeResolution("resolved", "scope_resolved", ResolvedScope(snapshot_id, selected, filters))
        except CatalogError as exc:
            return self._failure(exc)

    def _paths(self, text: str, snapshot, *, exclusion=False):
        """完整路径优先，唯一文件名/目录名其次；剩余路径不作模糊匹配。"""
        sources = {record.source for record in snapshot.documents}
        directories = {"/".join(PurePosixPath(source).parts[:i])
                       for source in sources for i in range(1, len(PurePosixPath(source).parts))}
        targets = sources | directories
        selected_sources, selected_directories = set(), set()

        def select(path):
            checked_path(path)
            if not exclusion:
                self.catalog.policy.require(path)
            if path not in targets:
                if exclusion and path in self.catalog.policy.excluded_paths:
                    selected_sources.add(path)
                    return
                raise CatalogError("unknown_path")
            (selected_sources if path in sources else selected_directories).add(path)

        def quoted(match):
            select(next(group for group in match.groups() if group is not None))
            return " "

        text = re.sub(r'`([^`]+)`|"([^"\n]+)"|“([^”\n]+)”', quoted, text)
        boundary_start, boundary_end = r"(?<![A-Za-z0-9_./-])", r"(?![A-Za-z0-9_./-])"
        for target in sorted(targets, key=len, reverse=True):
            pattern = boundary_start + re.escape(target) + boundary_end
            if re.search(pattern, text):
                select(target)
                text = re.sub(pattern, " ", text)

        def unknown_path(match):
            select(match.group())
            return " "

        text = re.sub(r"(?<![A-Za-z0-9_./-])/?(?:[\w.%+-]+/)+[\w.%+-]+", unknown_path, text)
        aliases = {}
        for target in targets:
            aliases.setdefault(PurePosixPath(target).name.casefold(), set()).add(target)
        for alias in sorted(aliases, key=len, reverse=True):
            pattern = boundary_start + re.escape(alias) + boundary_end
            if re.search(pattern, text, flags=re.IGNORECASE):
                paths = aliases[alias]
                if len(paths) != 1:
                    raise CatalogError("ambiguous_path")
                select(next(iter(paths)))
                text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
        return text, selected_directories, selected_sources

    @staticmethod
    def _dates(text: str, today: date):
        ranges = []

        def consume(pattern, make):
            nonlocal text
            def replace(match):
                ranges.append(make(match))
                return " "
            text = re.sub(pattern, replace, text)

        consume(r"(?:从|自)?(\d{4}-\d{2}-\d{2})\s*(?:到|至|~|～)\s*(\d{4}-\d{2}-\d{2})",
                lambda m: (date_value(m[1]), date_value(m[2])))
        consume(r"\d{4}-\d{2}-\d{2}", lambda m: (date_value(m[0]), date_value(m[0])))

        def month(year, number):
            return date(year, number, 1), date(year, number, monthrange(year, number)[1])

        consume(r"(\d{4})年(\d{1,2})月(\d{1,2})日", lambda m: (date(*map(int, m.groups())),) * 2)
        consume(r"(\d{4})年(\d{1,2})月", lambda m: month(int(m[1]), int(m[2])))
        consume(r"(?<!\d)(\d{4})-(\d{2})(?![-\d])", lambda m: month(int(m[1]), int(m[2])))

        def relative(match):
            value = match[0]
            if value == "今天":
                return today, today
            if value == "昨天":
                return today - timedelta(days=1), today - timedelta(days=1)
            if value == "本月":
                return month(today.year, today.month)
            if value == "上个月":
                previous = today.replace(day=1) - timedelta(days=1)
                return month(previous.year, previous.month)
            if value in {"今年", "去年"}:
                year = today.year - (value == "去年")
                return date(year, 1, 1), date(year, 12, 31)
            days = 7 if value == "最近一周" else int(match[1])
            if not 1 <= days <= 3660:
                raise CatalogError("invalid_date_range")
            return today - timedelta(days=days - 1), today

        consume(r"今天|昨天|本月|上个月|今年|去年|最近一周|最近\s*(\d{1,5})\s*天", relative)
        if len(ranges) > 1:
            raise CatalogError("ambiguous_date")
        if not ranges:
            return text, None, None
        first, last = ranges[0]
        if first > last:
            raise CatalogError("invalid_date_range")
        return text, first.isoformat(), last.isoformat()

    @staticmethod
    def _types(text):
        types = set()
        mapping = {"markdown": "md", "md": "md", "txt": "txt", "纯文本": "txt", "docx": "docx", "word": "docx"}
        def replace(match):
            types.add(mapping[match[0].lower().lstrip(".")])
            return " "
        text = re.sub(r"(?<![A-Za-z0-9_.])\.?(?:markdown|docx|word|txt|md|纯文本)(?![A-Za-z0-9_.])", replace, text, flags=re.IGNORECASE)
        return text, tuple(sorted(types))

    @staticmethod
    def _remaining(text: str):
        fillers = ("请帮我", "文件夹", "只包含", "仅包含", "全部", "所有", "文档", "文件", "资料", "目录", "修改", "更新",
                   "读取", "阅读", "分析", "汇总", "总结", "统计", "比较", "以及", "请", "的", "下", "中", "和", "与", "及", "或")
        text = re.sub("|".join(map(re.escape, sorted(fillers, key=len, reverse=True))), " ", text)
        return re.sub(r"[\s，,。；;、：:！!？?]+", "", text)

    def resolve(self, text: str, snapshot_id: str, *, today: date | None = None) -> ScopeResolution:
        try:
            if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                raise CatalogError("invalid_request")
            if "\\" in text or any(ord(char) < 32 and char not in "\n\t\r" for char in text):
                raise CatalogError("invalid_path")
            snapshot = self.catalog.get_snapshot(snapshot_id)
            pieces = re.split(r"(?:排除|不含|不要|除了)", text)
            if len(pieces) > 2:
                raise CatalogError("ambiguous_exclusion")
            included, directories, sources = self._paths(pieces[0], snapshot)
            if re.search(r"(?i)(?:\.?(?:md|txt|docx|markdown|word)|\d{4}(?:-\d{2}){0,2})\s*(?:目录|文件夹)", included):
                raise CatalogError("unknown_path")
            contexts = re.findall(r"(?:目录|文件夹)\s*(?:下|中)", pieces[0])
            if len(directories) > 1 and contexts:
                coordinated = bool(re.search(r"和|与|以及|、", included))
                if len(contexts) > 1 and not coordinated:
                    nested = [directory for directory in directories if all(within(directory, parent) for parent in directories)]
                    if len(nested) != 1:
                        raise CatalogError("conflicting_paths")
                    directories = set(nested)
                elif len(contexts) != 1 or not coordinated:
                    raise CatalogError("ambiguous_directory_nesting")
            if directories and sources:
                if not re.search(r"(?:目录|文件夹)\s*(?:下|中)", pieces[0]):
                    raise CatalogError("ambiguous_path_combination")
                if any(not any(within(source, directory) for directory in directories) for source in sources):
                    raise CatalogError("conflicting_paths")
                directories = set()
            included, first, last = self._dates(included, today or datetime.now(self.catalog.timezone).date())
            included, types = self._types(included)
            explicit_all = bool(re.search("所有|全部", included))
            if self._remaining(included):
                raise CatalogError("unrecognized_constraint")
            exclusions = set()
            if len(pieces) == 2:
                remainder, excluded_dirs, excluded_files = self._paths(pieces[1], snapshot, exclusion=True)
                exclusions = excluded_dirs | excluded_files
                if not exclusions or self._remaining(remainder):
                    raise CatalogError("ambiguous_exclusion")
            if not (directories or sources or types or first or last or explicit_all):
                raise CatalogError("scope_required")
            return self.resolve_filter(CatalogFilter(tuple(directories), tuple(sources), types, first, last, tuple(exclusions)), snapshot_id)
        except CatalogError as exc:
            return self._failure(exc)
        except (ValueError, OverflowError):
            return ScopeResolution("needs_clarification", "invalid_date")
