"""Phase II/III deterministic orchestration engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Iterable, Mapping

from storage.graph_repo import GraphRepo, InMemoryGraphRepo
from storage.vector_repo import InMemoryVectorRepo, VectorRepo

from .lifecycle import DynamicSlangLifecycleManager, PromptEvaluator
from .source_intake import AuthorizedSourcePolicy, ComplianceSourceDiscovery, MultimodalTextExtractor
from .strategy import CountermeasurePlanner, PlaybookBuilder, RiskClueAggregator
from .text_intelligence import (
    AdaptiveEntropyFilter,
    AdvancedEntityExtractor,
    FineGrainedIntentClassifier,
    SimilarityClusterer,
    SlangDictionary,
)


@dataclass
class PhaseRunResult:
    status: str
    mode: str
    input_count: int
    accepted_count: int
    dropped_count: int
    classification_count: int
    entity_count: int
    cluster_count: int
    risk_clue_count: int
    playbook_count: int
    strategy_count: int
    compliance_decisions: list[dict[str, Any]] = field(default_factory=list)
    entropy_decisions: list[dict[str, Any]] = field(default_factory=list)
    classifications: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    similarity_clusters: list[dict[str, Any]] = field(default_factory=list)
    risk_clues: list[dict[str, Any]] = field(default_factory=list)
    playbooks: list[dict[str, Any]] = field(default_factory=list)
    strategies: list[dict[str, Any]] = field(default_factory=list)
    graph_summary: dict[str, Any] = field(default_factory=dict)
    vector_summary: dict[str, Any] = field(default_factory=dict)
    prompt_eval: dict[str, Any] | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class PhaseTwoThreeEngine:
    """Local completion surface for PRD Phase II and Phase III.

    The engine deliberately keeps all high-impact outputs as review-only
    candidates. It does not execute enforcement, write production policy, or
    expand collection beyond authorized/compliance-gated source metadata.
    """

    def __init__(
        self,
        *,
        source_policy: AuthorizedSourcePolicy | None = None,
        multimodal_extractor: MultimodalTextExtractor | None = None,
        entropy_filter: AdaptiveEntropyFilter | None = None,
        classifier: FineGrainedIntentClassifier | None = None,
        entity_extractor: AdvancedEntityExtractor | None = None,
        clusterer: SimilarityClusterer | None = None,
        clue_aggregator: RiskClueAggregator | None = None,
        playbook_builder: PlaybookBuilder | None = None,
        strategy_planner: CountermeasurePlanner | None = None,
        vector_repo: InMemoryVectorRepo | None = None,
        graph_repo: InMemoryGraphRepo | None = None,
        lifecycle_manager: DynamicSlangLifecycleManager | None = None,
        prompt_evaluator: PromptEvaluator | None = None,
    ) -> None:
        self.source_policy = source_policy or AuthorizedSourcePolicy()
        self.multimodal_extractor = multimodal_extractor or MultimodalTextExtractor()
        self.entropy_filter = entropy_filter or AdaptiveEntropyFilter()
        self.classifier = classifier or FineGrainedIntentClassifier()
        self.entity_extractor = entity_extractor or AdvancedEntityExtractor()
        self.clusterer = clusterer or SimilarityClusterer()
        self.clue_aggregator = clue_aggregator or RiskClueAggregator()
        self.playbook_builder = playbook_builder or PlaybookBuilder()
        self.strategy_planner = strategy_planner or CountermeasurePlanner()
        self.vector_repo = vector_repo or VectorRepo()
        self.graph_repo = graph_repo or GraphRepo()
        self.lifecycle_manager = lifecycle_manager or DynamicSlangLifecycleManager()
        self.prompt_evaluator = prompt_evaluator or PromptEvaluator()
        self.compliance_discovery = ComplianceSourceDiscovery()
        self._record_cache: dict[str, dict[str, Any]] = {}
        self._last_run_payload: dict[str, Any] | None = None

    def run(
        self,
        records: Iterable[Mapping[str, Any] | Any],
        *,
        prompt_text: str | None = None,
        source_candidates: Iterable[Mapping[str, Any] | Any] | None = None,
    ) -> PhaseRunResult:
        raw_records = list(records)
        self._refresh_runtime_slang_dictionary()
        accepted, compliance_decisions = self.source_policy.filter_records(raw_records)
        materialized = [self.multimodal_extractor.materialize(record) for record in accepted]

        kept: list[dict[str, Any]] = []
        entropy_decisions = []
        for record in materialized:
            decision = self.entropy_filter.evaluate(record)
            entropy_decisions.append(_dump(decision))
            if decision.action == "KEEP":
                kept.append(record)

        classifications = [self.classifier.classify(record) for record in kept]
        entities = [entity for record in kept for entity in self.entity_extractor.extract(record)]
        clusters = self.clusterer.cluster(kept)

        for record in kept:
            trace_id = str(record.get("source_trace_id") or record.get("trace_id") or record.get("hash_id") or len(self.vector_repo.list()))
            cached_record = dict(record)
            cached_record.setdefault("source_trace_id", trace_id)
            self._record_cache[trace_id] = cached_record
            self.vector_repo.upsert(
                trace_id,
                str(record.get("content_text") or ""),
                {
                    "trace_id": trace_id,
                    "source_name": record.get("source_name"),
                    "source_type": record.get("source_type"),
                    "legal_basis": record.get("legal_basis"),
                    "publish_time": record.get("publish_time"),
                    "source_url": record.get("source_url"),
                },
            )
        risk_clues = self.clue_aggregator.aggregate(records=kept, classifications=classifications, entities=entities)
        playbooks = self.playbook_builder.build(risk_clues, kept)
        strategies = self.strategy_planner.plan(risk_clues, playbooks)
        self._index_graph(entities, risk_clues, playbooks)

        for entity in entities:
            if entity.entity_type == "slang_term":
                self.lifecycle_manager.nominate(entity.entity_value, entity.normalized_value, [entity.source_trace_id])

        prompt_eval = None
        if prompt_text is not None:
            prompt_eval = _dump(self.prompt_evaluator.evaluate("phase3_prompt", prompt_text, kept))

        if source_candidates:
            for candidate in source_candidates:
                compliance_decisions.append(self.compliance_discovery.evaluate(candidate))

        result = PhaseRunResult(
            status="completed",
            mode="phase2_phase3_enhancement",
            input_count=len(raw_records),
            accepted_count=len(accepted),
            dropped_count=len(raw_records) - len(kept),
            classification_count=len(classifications),
            entity_count=len(entities),
            cluster_count=len(clusters),
            risk_clue_count=len(risk_clues),
            playbook_count=len(playbooks),
            strategy_count=len(strategies),
            compliance_decisions=[_dump(item) for item in compliance_decisions],
            entropy_decisions=entropy_decisions,
            classifications=[_dump(item) for item in classifications],
            entities=[_dump(item) for item in entities],
            similarity_clusters=[_dump(item) for item in clusters],
            risk_clues=[_dump(item) for item in risk_clues],
            playbooks=[_dump(item) for item in playbooks],
            strategies=[_dump(item) for item in strategies],
            graph_summary={"node_count": len(self.graph_repo.nodes()), "edge_count": len(self.graph_repo.edges())},
            vector_summary={"record_count": len(self.vector_repo.list())},
            prompt_eval=prompt_eval,
        )
        self._last_run_payload = result.model_dump()
        return result

    def semantic_search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        return [_dump(item) for item in self.vector_repo.search(query, top_k=top_k)]

    def graph_neighbors(self, node_id: str) -> list[dict[str, Any]]:
        return [_dump(item) for item in self.graph_repo.neighbors(node_id)]

    def get_cached_record(self, trace_id: str) -> dict[str, Any] | None:
        record = self._record_cache.get(str(trace_id))
        return dict(record) if record is not None else None

    def get_cached_records(self, trace_ids: Iterable[str]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for trace_id in trace_ids:
            normalized = str(trace_id).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            record = self.get_cached_record(normalized)
            if record is not None:
                records.append(record)
        return records

    def expand_related_trace_ids(self, trace_ids: Iterable[str], *, limit: int = 6) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        def add_trace(trace_id: str | None) -> None:
            normalized = str(trace_id or "").strip()
            if not normalized or normalized in seen or len(ordered) >= max(1, int(limit or 1)):
                return
            if self.get_cached_record(normalized) is None:
                return
            seen.add(normalized)
            ordered.append(normalized)

        def extract_trace_id(node: Mapping[str, Any]) -> str | None:
            if str(node.get("node_type") or "").strip().lower() != "risk_sample":
                return None
            properties = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
            return str(properties.get("source_trace_id") or "").strip() or None

        for trace_id in trace_ids:
            add_trace(str(trace_id))
        direct_trace_ids = list(ordered)
        for trace_id in direct_trace_ids:
            if len(ordered) >= max(1, int(limit or 1)):
                break
            for neighbor in self.graph_neighbors(f"sample:{trace_id}"):
                add_trace(extract_trace_id(neighbor))
                if len(ordered) >= max(1, int(limit or 1)):
                    break
                node_id = str(neighbor.get("node_id") or "").strip()
                if not node_id:
                    continue
                for second_hop in self.graph_neighbors(node_id):
                    add_trace(extract_trace_id(second_hop))
                    if len(ordered) >= max(1, int(limit or 1)):
                        break
                if len(ordered) >= max(1, int(limit or 1)):
                    break
        return ordered

    def runtime_slang_terms(self, *, include_candidates: bool = False, include_gray: bool = False) -> tuple[str, ...]:
        return tuple(self.lifecycle_manager.runtime_terms_mapping(include_candidates=include_candidates, include_gray=include_gray).keys())

    def runtime_slang_mapping(self, *, include_candidates: bool = False, include_gray: bool = False) -> dict[str, str]:
        return self.lifecycle_manager.runtime_terms_mapping(include_candidates=include_candidates, include_gray=include_gray)

    def runtime_prompt_context(
        self,
        *,
        label: str | None = None,
        include_candidates: bool = False,
        include_gray: bool = False,
    ) -> dict[str, Any]:
        prompt_context = self.lifecycle_manager.prompt_context(
            label=label,
            include_candidates=include_candidates,
            include_gray=include_gray,
        )
        prompt_context["slang_terms_mapping"] = self.runtime_slang_mapping(
            include_candidates=include_candidates,
            include_gray=include_gray,
        )
        prompt_context["slang_term_values"] = list(prompt_context.get("slang_terms_mapping", {}).keys())
        return prompt_context

    def last_run_payload(self) -> dict[str, Any] | None:
        if self._last_run_payload is None:
            return None
        return deepcopy(self._last_run_payload)

    def _index_graph(self, entities: Iterable[Any], clues: Iterable[Any], playbooks: Iterable[Any]) -> None:
        for entity in entities:
            entity_id = f"entity:{entity.entity_type}:{entity.normalized_value}"
            trace_id = f"sample:{entity.source_trace_id}"
            self.graph_repo.upsert_node(entity_id, entity.entity_type, _dump(entity))
            self.graph_repo.upsert_node(trace_id, "risk_sample", {"source_trace_id": entity.source_trace_id})
            self.graph_repo.add_edge(trace_id, entity_id, "HAS_ENTITY")
        for clue in clues:
            clue_id = f"clue:{clue.clue_id}"
            self.graph_repo.upsert_node(clue_id, "risk_clue", _dump(clue))
            for trace_id in clue.evidence_trace_ids:
                self.graph_repo.upsert_node(f"sample:{trace_id}", "risk_sample", {"source_trace_id": trace_id})
                self.graph_repo.add_edge(clue_id, f"sample:{trace_id}", "SUPPORTED_BY")
        for playbook in playbooks:
            playbook_id = f"playbook:{playbook.playbook_id}"
            self.graph_repo.upsert_node(playbook_id, "cheating_playbook", _dump(playbook))
            for clue_id in playbook.clue_ids:
                self.graph_repo.add_edge(playbook_id, f"clue:{clue_id}", "COMPOSED_OF")

    def _refresh_runtime_slang_dictionary(self) -> None:
        mapping = self.lifecycle_manager.runtime_terms_mapping(include_candidates=False, include_gray=False)
        if not mapping:
            if isinstance(getattr(self.entity_extractor, "slang_dictionary", None), SlangDictionary):
                return
            self.entity_extractor.slang_dictionary = SlangDictionary()
            return
        self.entity_extractor.slang_dictionary = SlangDictionary(mapping)


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": value}


__all__ = ["PhaseRunResult", "PhaseTwoThreeEngine"]
