"""保守实体关联：同名仅候选，规则支持的同一性才参与合并。"""
from itertools import combinations
from collections import Counter, defaultdict
import re
import unicodedata
from uuid import UUID

from app.document_analysis.catalog import CatalogError, fingerprint
from app.document_analysis.evidence_input import load_input
from app.document_analysis.synthesis_runtime import execute_stage
from app.document_analysis.synthesis_schema import (
    EntityRelation, Mention, RelationSuggestion, ResolutionConfig, ResolutionRequest,
)

PRONOUNS = {"他", "她", "它", "其", "他们", "她们", "它们", "此人", "该人", "该实验", "本实验", "该对象",
            "我", "你", "您", "俺", "我们", "你们", "您们", "咱", "咱们", "本人", "自己",
            "he", "she", "it", "they", "him", "her", "i", "me", "we", "us", "you", "them",
            "myself", "ourselves", "yourself", "yourselves", "himself", "herself", "itself", "themselves",
            "this person", "that person", "this experiment", "the experiment"}


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def origin_identity(record):
    return {"source":record.source, "sha256":record.document_sha256,
            "parser_version":record.parser_version, "content_sha256":record.parsed_content_sha256,
            "start":record.location.start_char, "end":record.location.end_char}


def _term_start(label):
    # 不允许 Ann 匹配 JoAnn；仅额外识别括号标识后明确的中文列举连接词。
    return r"(?:(?<!\w)|(?<=[）)]和)|(?<=[）)]与)|(?<=[）)]及))" + re.escape(label)


def _slots(record):
    details = record.details
    if record.template_id == "people_events":
        for label in sorted(set(details["people"])):
            yield "person", "person", label
    elif record.template_id == "research_parameters":
        yield "experiment", "experiment", details["experiment"]
    elif record.template_id == "general_facts":
        for role in ("subject", "object"):
            label = details[role]
            is_number = bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?%?", label.strip()))
            if label.strip() and (role == "subject" or not is_number):
                yield "object", role, label


def derive_mentions(records):
    result = []
    for record in records:
        for kind, role, label in _slots(record):
            grounded = bool(label.strip()) and label in record.quote
            pronoun = normalized(label) in PRONOUNS
            candidates = {}
            if grounded and not pronoun:
                pattern = _term_start(label) + r"\s*[（(]\s*(ORCID|全局ID|UUID)\s*[:：]\s*([A-Za-z0-9-]+)\s*[)）]"
                for match in re.finditer(pattern, record.quote, flags=re.I):
                    namespace, value = match.group(1).casefold(), match.group(2)
                    if namespace == "orcid":
                        if kind != "person" or not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value, flags=re.I):
                            continue
                        value = value.upper()
                    else:
                        try:
                            value = str(UUID(value))
                        except ValueError:
                            continue
                        namespace = "global_uuid"
                    candidates.setdefault(namespace, set()).add(value)
            conflict = any(len(values) != 1 for values in candidates.values())
            identifiers = {key:next(iter(values)) for key,values in candidates.items() if len(values) == 1}
            identity = {"kind":kind, "role":role, "label":label}
            result.append(Mention(
                mention_id=fingerprint({"evidence_id":record.evidence_id, **identity}), kind=kind, role=role, label=label,
                evidence_ids=[record.evidence_id], origin_key=fingerprint({**origin_identity(record), **identity}),
                grounded=grounded, pronoun=pronoun, identifiers=identifiers, identifier_conflict=conflict,
            ))
    return sorted(result, key=lambda item:item.mention_id)


def _explicit_statements(left, right, records):
    alias = different = False
    # 同一证据内的明确陈述才能绑定这两个提及；不能借别名文字猜测另一文档中的同名者。
    for evidence_id in set(left.evidence_ids) & set(right.evidence_ids):
        quote = records[evidence_id].quote
        for first, second in ((left.label,right.label),(right.label,left.label)):
            a, b = _term_start(first), re.escape(second)
            ending = r"(?=$|[\s，。；;、!?！？:：)）])"
            alias |= bool(re.search(a + r"\s*(?:[（(]\s*(?:又名|别名|也称|aka)\s*" + b + r"\s*[)）]|(?:又名|也称)\s*" + b + ending + r")", quote, flags=re.I))
            different |= bool(re.search(a + r"\s*(?:与|和|、|and)\s*" + b + r"\s*(?:并)?(?:不是同一人|不是同一个实验|不是同一对象|不是同一个对象)", quote, flags=re.I))
    return alias, different


def rule_relation(left, right, records):
    common = {"relation_id":fingerprint(sorted((left.mention_id,right.mention_id))),
              "left":left.mention_id, "right":right.mention_id,
              "evidence_ids":sorted(set(left.evidence_ids + right.evidence_ids))}
    def decision(relation, confidence, reasons, merge=False):
        return EntityRelation(**common, relation=relation, confidence_status=confidence, rationale=reasons, automatic_merge=merge)
    if not left.grounded or not right.grounded:
        return decision("unknown", "uncertain", ["name_not_in_original_quote"])
    if left.pronoun or right.pronoun:
        return decision("unknown", "uncertain", ["unresolved_pronoun"])
    if left.identifier_conflict or right.identifier_conflict:
        return decision("unknown", "conflicted", ["conflicting_identifiers_in_quote"])
    alias, denial = _explicit_statements(left, right, records)
    namespaces = set(left.identifiers) & set(right.identifiers)
    same_ids = any(left.identifiers[key] == right.identifiers[key] for key in namespaces)
    different_ids = any(left.identifiers[key] != right.identifiers[key] for key in namespaces)
    identical_origin = left.origin_key == right.origin_key
    positive = identical_origin or same_ids or alias
    negative = different_ids or denial
    reasons = (["identical_original_mention"] if identical_origin else []) + (["shared_explicit_identifier"] if same_ids else [])
    reasons += (["explicit_alias_in_quote"] if alias else []) + (["different_explicit_identifiers"] if different_ids else [])
    reasons += ["explicit_difference_in_quote"] if denial else []
    if positive and negative:
        return decision("unknown", "conflicted", reasons)
    if negative:
        return decision("different", "rule_supported", reasons)
    if positive:
        return decision("same", "rule_supported", reasons, True)
    if normalized(left.label) == normalized(right.label):
        return decision("possible_same", "uncertain", ["same_name_is_not_identity"])
    return decision("unknown", "uncertain", ["insufficient_identity_evidence"])


def candidate_pairs(mentions, records, max_pairs=1_000_000):
    """按同名、同出处、标识符及明确陈述分桶；未入候选不等于明确不同。"""
    blocks = defaultdict(list)
    cooccurrences = defaultdict(list)
    lookup = {item.mention_id: item for item in mentions}
    for item in mentions:
        if not item.grounded:
            # 保留未定位提及及其 unresolved 分组，不用模型生成的名称推测关联。
            continue
        blocks[(item.kind, "name", normalized(item.label))].append(item.mention_id)
        blocks[(item.kind, "origin", item.origin_key)].append(item.mention_id)
        for namespace in item.identifiers:
            # 同一命名空间允许规则直接判断相同或不同，不需要模型猜身份。
            blocks[(item.kind, "identifier", namespace)].append(item.mention_id)
        for key in item.evidence_ids:
            cooccurrences[(item.kind, key)].append(item.mention_id)
    pairs = set()
    def add(left, right):
        pairs.add(tuple(sorted((left, right))))
        if len(pairs) > max_pairs:
            raise CatalogError("entity_pair_limit")
    for values in blocks.values():
        for left, right in combinations(sorted(set(values)), 2):
            add(left, right)
    for values in cooccurrences.values():
        for left, right in combinations(sorted(set(values)), 2):
            if (left, right) not in pairs and any(_explicit_statements(lookup[left], lookup[right], records)):
                add(left, right)
    return [(lookup[left], lookup[right]) for left, right in sorted(pairs)]


def _group_entities(mentions, relations):
    lookup = {item.mention_id:item for item in mentions}
    graph = {item.mention_id:set() for item in mentions}
    for relation in relations:
        if relation.automatic_merge:
            graph[relation.left].add(relation.right)
            graph[relation.right].add(relation.left)
    components, seen = [], set()
    for key in graph:
        if key in seen:
            continue
        component, pending = set(), [key]
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(graph[node] - component)
        components.append(component)
        seen.update(component)
    conflicted, conflict_sources = set(), {}
    for component in components:
        if any(row.left in component and row.right in component and
               (row.relation == "different" and row.confidence_status == "rule_supported" or row.confidence_status == "conflicted")
               for row in relations):
            conflicted.update(component)
            evidence_ids = sorted({value for key in component for value in lookup[key].evidence_ids})
            conflict_sources.update({key:evidence_ids for key in component})
    adjusted = []
    for relation in relations:
        if relation.automatic_merge and relation.left in conflicted:
            relation = relation.model_copy(update={"relation":"unknown", "confidence_status":"conflicted", "automatic_merge":False,
                                                   "rationale":relation.rationale + ["inconsistent_same_component"],
                                                   "evidence_ids":sorted(set(relation.evidence_ids + conflict_sources[relation.left]))})
        adjusted.append(relation)
    groups = [component for component in components if not component & conflicted]
    groups.extend({key} for key in sorted(conflicted))
    entities = []
    for group in groups:
        members = [lookup[key] for key in sorted(group)]
        uncertain = (bool(group & conflicted) or any(not item.grounded or item.pronoun or item.identifier_conflict for item in members)
                     or len(group) == 1 and any(row.relation == "possible_same" and (row.left in group or row.right in group) for row in adjusted))
        entities.append({"entity_id":fingerprint(sorted(group)), "kind":members[0].kind,
                         "names":sorted({item.label for item in members}), "mention_ids":sorted(group),
                         "evidence_ids":sorted({value for item in members for value in item.evidence_ids}),
                         "identity_status":"unresolved" if uncertain else "linked_by_rule" if len(group)>1 else "isolated",
                         "source_location_verified":True, "semantic_verified":False})
    return sorted(entities, key=lambda item:item["entity_id"]), adjusted


class EntityResolver:
    def __init__(self, store, assistant=None):
        self.store, self.assistant = store, assistant

    def run(self, task_ids, config=ResolutionConfig(), *, cancel=None):
        if not isinstance(config, ResolutionConfig):
            raise CatalogError("invalid_resolution_config")
        data = load_input(self.store.evidence, task_ids)
        def build(session):
            mentions = derive_mentions(data.evidence)
            if len(mentions) > config.max_mentions:
                raise CatalogError("entity_mention_limit")
            records = {record.evidence_id:record for record in data.evidence}
            pairs = candidate_pairs(mentions, records, config.max_pairs)
            relations = []
            for left, right in pairs:
                session.check_cancel()
                def compute():
                    relation = rule_relation(left, right, records)
                    if (self.assistant is not None and relation.confidence_status == "uncertain"
                            and left.grounded and right.grounded and not left.pronoun and not right.pronoun):
                        session.before_model(self.assistant, "entity_resolution")
                        requested = tuple(records[key].model_copy(deep=True) for key in relation.evidence_ids)
                        suggestion = self.assistant.resolve(ResolutionRequest(left.model_copy(deep=True), right.model_copy(deep=True), requested))
                        suggestion = RelationSuggestion.model_validate(suggestion)
                        if not suggestion.complete:
                            raise CatalogError("incomplete_entity_response")
                        if set(suggestion.evidence_ids) != set(relation.evidence_ids):
                            raise CatalogError("unknown_or_missing_relation_evidence")
                        relation = relation.model_copy(update={
                            "relation":"possible_same" if suggestion.relation == "same" else suggestion.relation,
                            "confidence_status":"model_suggestion", "automatic_merge":False,
                            "rationale":relation.rationale + [suggestion.rationale, "model_identity_not_verified"],
                        })
                    return relation.model_dump(mode="json")
                key = "relation:" + fingerprint([left.mention_id,right.mention_id])
                relations.append(EntityRelation.model_validate(session.checkpoint(key, compute)))
            entities, relations = _group_entities(mentions, relations)
            uncertainties = set()
            if data.upstream_incomplete:
                uncertainties.add("upstream_incomplete")
            if (any(entity["identity_status"] == "unresolved" for entity in entities)
                    or any(row.relation in {"unknown","possible_same"} for row in relations)):
                uncertainties.add("unresolved_identity")
            if any(row.confidence_status == "conflicted" for row in relations):
                uncertainties.add("identity_conflict")
            if not data.evidence:
                uncertainties.add("no_available_evidence")
            return {"mentions":[item.model_dump(mode="json") for item in mentions],
                    "relations":[item.model_dump(mode="json") for item in relations], "entities":entities,
                    "evidence_ids":[item.evidence_id for item in data.evidence], "uncertainties":sorted(uncertainties),
                    "candidate_selection":{"algorithm":"identity-blocks-v1", "candidate_pairs":len(pairs),
                        "all_same_kind_pairs":sum(n*(n-1)//2 for n in Counter(item.kind for item in mentions).values()),
                        "omitted_pairs":"not_assessed_not_different"},
                    "upstream_incomplete":data.upstream_incomplete, "source_location_verified":True, "semantic_verified":False}
        return execute_stage(self.store, "entity_resolution", data,
                             {"algorithm":"entity-rules-v2", **config.model_dump(mode="json")}, self.assistant, build, cancel=cancel)
