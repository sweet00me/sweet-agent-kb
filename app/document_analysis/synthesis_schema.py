"""关联/归纳的严格接口；原文定位校验不代表事实语义已验证。"""
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field

from app.document_analysis.evidence_schema import EvidenceRecord, ExtractorIdentity, StrictModel

RelationKind = Literal["same", "possible_same", "unknown", "different"]


class ResolutionConfig(StrictModel):
    max_mentions: int = Field(default=256, ge=1, le=4096)
    max_pairs: int = Field(default=32768, ge=1, le=1000000)


class ReduceConfig(StrictModel):
    semantic_batch_size: int = Field(default=16, ge=1, le=64)
    semantic_batch_chars: int = Field(default=32768, ge=8192, le=65536)


class Mention(StrictModel):
    mention_id: str
    kind: Literal["person", "experiment", "object"]
    role: str
    label: str
    evidence_ids: list[str] = Field(min_length=1)
    origin_key: str
    grounded: bool
    pronoun: bool
    identifiers: dict[str, str]
    identifier_conflict: bool = False
    source_location_verified: Literal[True] = True
    semantic_verified: Literal[False] = False


class EntityRelation(StrictModel):
    relation_id: str
    left: str
    right: str
    relation: RelationKind
    confidence_status: Literal["rule_supported", "model_suggestion", "uncertain", "conflicted"]
    rationale: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    automatic_merge: bool = False
    source_location_verified: Literal[True] = True
    semantic_verified: Literal[False] = False


class RelationSuggestion(StrictModel):
    complete: bool
    relation: RelationKind
    rationale: str = Field(min_length=1, max_length=2048)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class ResolutionRequest:
    left: Mention
    right: Mention
    evidence: tuple[EvidenceRecord, ...]


class EntityAssistant(Protocol):
    identity: ExtractorIdentity

    def resolve(self, request: ResolutionRequest) -> RelationSuggestion:
        ...


class SemanticConclusion(StrictModel):
    text: str = Field(min_length=1, max_length=4096)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)
    uncertainty: str = Field(min_length=1, max_length=2048)


class SummaryResponse(StrictModel):
    complete: bool
    conclusions: list[SemanticConclusion] = Field(max_length=64)


@dataclass(frozen=True)
class ReduceRequest:
    evidence: tuple[EvidenceRecord, ...]
    claims: tuple[dict, ...]
    uncertainties: tuple[str, ...]


class EvidenceSummarizer(Protocol):
    identity: ExtractorIdentity

    def summarize(self, request: ReduceRequest) -> SummaryResponse:
        ...
