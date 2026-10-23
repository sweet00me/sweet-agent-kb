"""证据模板、可注入提取协议与严格的原文定位；不调用模型。"""
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.document_analysis.catalog import CatalogError, DocumentRecord, fingerprint
from app.document_analysis.security import checked_path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ExtractorIdentity(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    provider: Literal["offline", "deepseek"] = "offline"


class PersonEvent(StrictModel):
    people: list[str] = Field(max_length=64)
    event: str = Field(min_length=1, max_length=2048)
    time: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=256)


class ExperimentParameter(StrictModel):
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=512)
    unit: str | None = Field(default=None, max_length=128)


class ResearchParameters(StrictModel):
    experiment: str = Field(min_length=1, max_length=512)
    parameters: list[ExperimentParameter] = Field(min_length=1, max_length=64)


class GeneralFact(StrictModel):
    subject: str = Field(min_length=1, max_length=512)
    predicate: str = Field(min_length=1, max_length=512)
    object: str = Field(min_length=1, max_length=2048)


@dataclass(frozen=True)
class Template:
    template_id: str
    description: str
    details_model: type[StrictModel]
    version: str = "1"

    @property
    def details_schema(self):
        return self.details_model.model_json_schema()


TEMPLATES = MappingProxyType({
    "people_events": Template("people_events", "提取原文直接支持的人物与事件；未知时间、地点留空，不推断身份。", PersonEvent),
    "research_parameters": Template("research_parameters", "提取科研实验设置与参数；保留原文数值表达、单位和实验上下文。", ResearchParameters),
    "general_facts": Template("general_facts", "提取原文直接支持的主体、关系和对象；区分事实、观点及不确定性。", GeneralFact),
})


def get_template(template_id: str) -> Template:
    try:
        return TEMPLATES[template_id]
    except (KeyError, TypeError) as exc:
        raise CatalogError("unknown_evidence_template") from exc


@dataclass(frozen=True)
class MapConfig:
    batch_chars: int = 8192
    overlap_chars: int = 1024

    def __post_init__(self):
        if type(self.batch_chars) is not int or not 1 <= self.batch_chars <= 32768:
            raise CatalogError("invalid_batch_size")
        if type(self.overlap_chars) is not int or not 0 <= self.overlap_chars <= 32768:
            raise CatalogError("invalid_overlap_size")


@dataclass(frozen=True)
class ExtractionWindow:
    source: str
    document_id: str
    sha256: str
    index: int
    start_char: int
    new_start_char: int
    end_char: int
    total_chars: int
    text: str

    def __post_init__(self):
        checked_path(self.source)
        numbers = (self.index, self.start_char, self.new_start_char, self.end_char, self.total_chars)
        if (any(type(n) is not int or n < 0 for n in numbers)
                or not 0 <= self.start_char <= self.new_start_char < self.end_char <= self.total_chars
                or not isinstance(self.text, str) or len(self.text) != self.end_char - self.start_char
                or len(self.text) > 65536):
            raise CatalogError("invalid_extraction_window")

    @property
    def digest(self):
        return fingerprint(self.__dict__)


class CandidateEvidence(StrictModel):
    quote: str = Field(min_length=1, max_length=4096)
    conclusion: str = Field(min_length=1, max_length=2048)
    uncertainty: str = Field(min_length=1, max_length=1024)
    details: dict[str, Any]
    start_char: int | None = Field(default=None, ge=0)
    end_char: int | None = Field(default=None, ge=1)

    @field_validator("quote", "conclusion", "uncertainty")
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError("empty_evidence_text")
        value.encode("utf-8")
        return value

    @model_validator(mode="after")
    def paired_offsets(self):
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("paired_offsets_required")
        return self


class ExtractionResponse(StrictModel):
    candidates: list[CandidateEvidence] = Field(max_length=64)
    complete: bool


@dataclass(frozen=True)
class ExtractionRequest:
    window: ExtractionWindow
    template: Template


class EvidenceExtractor(Protocol):
    identity: ExtractorIdentity

    def extract(self, request: ExtractionRequest) -> ExtractionResponse:
        ...


class EvidenceLocation(StrictModel):
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=1)
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)
    basis: Literal["parsed_text_unicode_v1"] = "parsed_text_unicode_v1"


class EvidenceRecord(StrictModel):
    evidence_id: str
    task_id: str
    source: str
    document_id: str
    document_sha256: str
    parsed_content_sha256: str
    cache_sha256: str
    parser_version: str
    template_id: str
    template_version: str
    quote: str
    location: EvidenceLocation
    conclusion: str
    uncertainty: str
    details: dict[str, Any]


def bind_evidence(candidate: CandidateEvidence, window: ExtractionWindow, entry: DocumentRecord,
                  full_text: str, task_id: str, template_id: str) -> EvidenceRecord:
    """模型不能指定来源；窗口位置转换为完整解析正文的可验证 Unicode 位置。"""
    template = get_template(template_id)
    if (window.source != entry.source or window.sha256 != entry.sha256 or window.document_id != entry.document_id
            or window.total_chars != len(full_text) or window.text != full_text[window.start_char:window.end_char]
            or hashlib.sha256(full_text.encode("utf-8")).hexdigest() != entry.content_sha256):
        raise CatalogError("evidence_version_mismatch")
    if candidate.start_char is None:
        start = window.text.find(candidate.quote)
        if start < 0:
            raise CatalogError("quote_not_found")
        if window.text.find(candidate.quote, start + 1) >= 0:
            raise CatalogError("ambiguous_quote_position")
        end = start + len(candidate.quote)
    else:
        start, end = candidate.start_char, candidate.end_char
        if end <= start or end > len(window.text) or window.text[start:end] != candidate.quote:
            raise CatalogError("quote_position_mismatch")
    start, end = start + window.start_char, end + window.start_char
    if full_text[start:end] != candidate.quote:
        raise CatalogError("quote_position_mismatch")
    try:
        details = template.details_model.model_validate(candidate.details).model_dump(mode="json")
    except ValidationError as exc:
        raise CatalogError("invalid_template_details") from exc
    location = EvidenceLocation(start_char=start, end_char=end,
                                start_line=full_text.count("\n", 0, start) + 1,
                                start_column=start - full_text.rfind("\n", 0, start),
                                end_line=full_text.count("\n", 0, end) + 1,
                                end_column=end - full_text.rfind("\n", 0, end))
    # 只做结构化去重，不进行实体消歧或语义合并；相同出处和字段保留首次结论。
    identity = {"task_id": task_id, "source": entry.source, "sha256": entry.sha256,
                "template": template_id, "start": start, "end": end, "details": details}
    return EvidenceRecord(evidence_id=fingerprint(identity), task_id=task_id, source=entry.source,
                          document_id=entry.document_id, document_sha256=entry.sha256,
                          parsed_content_sha256=entry.content_sha256, cache_sha256=entry.cache_sha256,
                          parser_version=entry.parser_version, template_id=template_id, template_version=template.version,
                          quote=candidate.quote, location=location, conclusion=candidate.conclusion,
                          uncertainty=candidate.uncertainty, details=details)
