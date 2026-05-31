"""LLM-backed user intent parsing and investigation planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from src.backend import LLMGateway
from src.safety import PromptGuard


DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS = 180


@dataclass(frozen=True)
class UserIntent:
    goal: str
    risk_types: list[str]
    source_preferences: list[str]
    include_keywords: list[str]
    exclude_keywords: list[str]
    time_range_hours: int
    quality_profile: str
    output_type: str
    require_cross_source: bool
    require_evidence_chain: bool
    raw_query: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InvestigationPlan:
    goal: str
    agent_steps: list[dict[str, str]]
    source_selection_strategy: dict[str, Any]
    execution_notes: list[str]
    quality_gate: dict[str, Any]
    budget: dict[str, Any] = field(default_factory=dict)
    selected_source_names: list[str] = field(default_factory=list)
    llm_ok: bool = False
    llm_reason: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LLMDecisionTrace:
    stage: str
    llm_ok: bool
    used_fallback: bool
    parsed_json: dict[str, Any] | None
    error: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class LLMUserRequestParser:
    """Parse user natural-language investigation requests via an external LLM."""

    def __init__(self, llm_gateway: LLMGateway) -> None:
        self.llm_gateway = llm_gateway

    def parse(
        self,
        query: str,
        *,
        runtime_context: Mapping[str, Any] | None = None,
        budget: Any | None = None,
        deadline_ms: int | None = None,
    ) -> tuple[UserIntent, LLMDecisionTrace]:
        runtime_context = dict(runtime_context or {})
        guarded_query = PromptGuard().wrap_untrusted_text(query)
        response = self.llm_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are BlackAgent's intent parser. Extract a structured JSON object "
                        "for a cyber-fraud intelligence request. "
                        "Return only JSON with fields: goal, risk_types, source_preferences, "
                        "include_keywords, exclude_keywords, time_range_hours, quality_profile, "
                        "output_type, require_cross_source, require_evidence_chain. "
                        "Use runtime approved slang terms and few-shot review examples when they help disambiguate the request."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"query={guarded_query}\n"
                        f"runtime_slang_terms={runtime_context.get('slang_terms', [])[:12]}\n"
                        f"runtime_few_shot_examples={runtime_context.get('few_shot_examples', [])[:4]}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=400,
            response_format={"type": "json_object"},
            stage="intent_parse",
            budget=budget,
            cache_policy="read_write",
            deadline_ms=deadline_ms,
        )
        parsed = response.parsed_json or {}
        intent = self._intent_from_payload(parsed, query=query, runtime_context=runtime_context)
        used_fallback = not _intent_payload_usable(parsed)
        if used_fallback:
            intent = _fallback_intent(query, runtime_context=runtime_context)
        trace = LLMDecisionTrace(
            stage="intent_parse",
            llm_ok=response.ok,
            used_fallback=used_fallback,
            parsed_json=(
                {
                    **parsed,
                    "runtime_slang_term_count": len(runtime_context.get("slang_terms", []) or []),
                    "runtime_few_shot_count": len(runtime_context.get("few_shot_examples", []) or []),
                }
                if parsed
                else {
                    "runtime_slang_term_count": len(runtime_context.get("slang_terms", []) or []),
                    "runtime_few_shot_count": len(runtime_context.get("few_shot_examples", []) or []),
                }
            ),
            error=response.error,
        )
        return intent, trace

    def _intent_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        query: str,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> UserIntent:
        fallback = _fallback_intent(query, runtime_context=runtime_context)
        return UserIntent(
            goal=_normalize_goal(payload.get("goal")),
            risk_types=_string_list(payload.get("risk_types")) or fallback.risk_types,
            source_preferences=_string_list(payload.get("source_preferences")),
            include_keywords=_string_list(payload.get("include_keywords")) or fallback.include_keywords,
            exclude_keywords=_string_list(payload.get("exclude_keywords")),
            time_range_hours=_coerce_positive_int(payload.get("time_range_hours"), default=24),
            quality_profile=_normalize_quality_profile(payload.get("quality_profile")),
            output_type=str(payload.get("output_type") or "clue_cards"),
            require_cross_source=_coerce_bool(payload.get("require_cross_source"), default=True),
            require_evidence_chain=_coerce_bool(payload.get("require_evidence_chain"), default=True),
            raw_query=query,
        )


class LLMInvestigationPlanner:
    """Generate a structured multi-agent execution plan via an external LLM."""

    def __init__(self, llm_gateway: LLMGateway) -> None:
        self.llm_gateway = llm_gateway

    def plan(
        self,
        query: str,
        intent: UserIntent,
        *,
        available_sources: Iterable[Mapping[str, Any]] = (),
        runtime_context: Mapping[str, Any] | None = None,
        budget: Any | None = None,
        deadline_ms: int | None = None,
    ) -> tuple[InvestigationPlan, LLMDecisionTrace]:
        runtime_context = dict(runtime_context or {})
        guarded_query = PromptGuard().wrap_untrusted_text(query)
        source_brief = [
            {
                "source_name": str(source.get("source_name") or source.get("name") or ""),
                "source_type": str(source.get("source_type") or source.get("type") or ""),
                "query_theme": str(source.get("query_theme") or ""),
            }
            for source in list(available_sources)[:20]
        ]
        response = self.llm_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                    "You are BlackAgent's investigation planner. "
                    "Return only JSON with fields: goal, agent_steps, selected_source_names, "
                    "source_selection_strategy, execution_notes, quality_gate, budget. "
                    "agent_steps must be an array of {agent, action}. "
                    "Plan a multi-agent workflow but keep execution reviewable and compliant. "
                    "When useful, source_selection_strategy may include collection_mode "
                    "(adaptive|pool_only|live_only|hybrid) and query_rewrite_policy (auto|off). "
                    "execution_notes may include refine_policy=budgeted or refine_policy=off."
                ),
                },
                {
                    "role": "user",
                    "content": (
                        f"user_query={guarded_query}\n"
                        f"intent={intent.model_dump()}\n"
                        f"available_sources={source_brief}\n"
                        f"runtime_slang_terms={runtime_context.get('slang_terms', [])[:12]}\n"
                        f"runtime_few_shot_examples={runtime_context.get('few_shot_examples', [])[:4]}\n"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=700,
            response_format={"type": "json_object"},
            stage="investigation_plan",
            budget=budget,
            cache_policy="read_write",
            deadline_ms=deadline_ms,
        )
        parsed = response.parsed_json or {}
        plan = self._plan_from_payload(parsed, intent=intent, runtime_context=runtime_context)
        used_fallback = not _plan_payload_usable(parsed)
        if used_fallback:
            plan = _fallback_plan(intent, runtime_context=runtime_context)
        trace = LLMDecisionTrace(
            stage="investigation_plan",
            llm_ok=response.ok,
            used_fallback=used_fallback,
            parsed_json=(
                {
                    **parsed,
                    "runtime_slang_term_count": len(runtime_context.get("slang_terms", []) or []),
                    "runtime_few_shot_count": len(runtime_context.get("few_shot_examples", []) or []),
                }
                if parsed
                else {
                    "runtime_slang_term_count": len(runtime_context.get("slang_terms", []) or []),
                    "runtime_few_shot_count": len(runtime_context.get("few_shot_examples", []) or []),
                }
            ),
            error=response.error,
        )
        return plan, trace

    def _plan_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        intent: UserIntent,
        runtime_context: Mapping[str, Any] | None = None,
    ) -> InvestigationPlan:
        steps_raw = payload.get("agent_steps")
        agent_steps: list[dict[str, str]] = []
        fallback_plan = _fallback_plan(intent, runtime_context=runtime_context)
        if isinstance(steps_raw, list):
            for item in steps_raw:
                if isinstance(item, Mapping):
                    agent = str(item.get("agent") or "").strip()
                    action = str(item.get("action") or "").strip()
                    if agent and action:
                        agent_steps.append({"agent": agent, "action": action})
        # Real LLMs often paraphrase agent names. Keep the API contract stable:
        # the displayed high-level plan always starts with the canonical
        # deterministic pipeline stages while the raw LLM plan remains available
        # in llm_traces[*].parsed_json for inspection.
        if not agent_steps or agent_steps[0].get("agent") != "intent_planner":
            agent_steps = fallback_plan.agent_steps

        source_selection_strategy = payload.get("source_selection_strategy")
        if not isinstance(source_selection_strategy, Mapping):
            source_selection_strategy = fallback_plan.source_selection_strategy
        quality_gate = payload.get("quality_gate")
        if not isinstance(quality_gate, Mapping):
            quality_gate = fallback_plan.quality_gate
        budget = payload.get("budget")
        if not isinstance(budget, Mapping):
            budget = fallback_plan.budget

        return InvestigationPlan(
            goal=str(payload.get("goal") or intent.goal),
            agent_steps=agent_steps,
            selected_source_names=_string_list(payload.get("selected_source_names")),
            source_selection_strategy=dict(source_selection_strategy),
            execution_notes=_string_list(payload.get("execution_notes")) or fallback_plan.execution_notes,
            quality_gate=dict(quality_gate),
            budget=_normalize_budget(dict(budget)),
            llm_ok=True,
            llm_reason=None,
        )


def _fallback_intent(query: str, runtime_context: Mapping[str, Any] | None = None) -> UserIntent:
    normalized = query.lower()
    runtime_context = dict(runtime_context or {})
    risk_types: list[str] = []
    if "诈骗" in query or "引流" in query:
        risk_types.append("诈骗引流")
    if "接码" in query:
        risk_types.append("接码")
    if "跑分" in query or "代付" in query:
        risk_types.append("跑分代付")
    if "账号" in query or "卖号" in query:
        risk_types.append("账号交易")
    if not risk_types:
        risk_types = ["黑灰产情报"]

    runtime_labels: list[str] = []
    runtime_keywords: list[str] = []
    slang_terms = runtime_context.get("slang_terms") if isinstance(runtime_context.get("slang_terms"), list) else []
    for item in slang_terms:
        if not isinstance(item, Mapping):
            continue
        raw = str(item.get("term") or item.get("raw") or "").strip()
        target = str(item.get("normalized_term") or item.get("target") or "").strip()
        if raw and raw in query:
            runtime_keywords.append(raw)
            if target:
                runtime_keywords.append(target)
        elif target and target.lower() in normalized:
            runtime_keywords.append(target)
            if raw:
                runtime_keywords.append(raw)

    few_shot_examples = runtime_context.get("few_shot_examples") if isinstance(runtime_context.get("few_shot_examples"), list) else []
    for item in few_shot_examples:
        if not isinstance(item, Mapping):
            continue
        term = str(item.get("term") or "").strip()
        label = str(item.get("label") or "").strip()
        if term and term in query:
            runtime_keywords.append(term)
            if label:
                runtime_labels.append(label)

    if runtime_labels:
        risk_types = _dedupe_list([*risk_types, *runtime_labels])

    sources: list[str] = []
    for token in ("telegram", "tg", "电报"):
        if token in normalized or token in query:
            sources.append("telegram")
            break
    for token in ("forum", "论坛", "贴吧"):
        if token in normalized or token in query:
            sources.append("forum")
            break
    for token in ("im", "群", "私聊", "聊天"):
        if token in normalized or token in query:
            sources.append("im")
            break
    if not sources:
        sources = ["telegram", "forum", "im"]

    time_range_hours = 24
    if "48小时" in query or "48h" in normalized:
        time_range_hours = 48
    elif "72小时" in query or "72h" in normalized:
        time_range_hours = 72
    elif "当天" in query or "今日" in query or "今天" in query:
        time_range_hours = 24

    quality_profile = "high_precision" if any(term in query for term in ("高质量", "高置信", "可复核")) else "balanced"
    include_keywords = _dedupe_list([*risk_types, *runtime_keywords])
    return UserIntent(
        goal="collect_high_quality_risk_clues",
        risk_types=risk_types,
        source_preferences=_dedupe_list(sources),
        include_keywords=include_keywords,
        exclude_keywords=["曝光", "辟谣", "警方通报", "安全研究", "反诈提醒"],
        time_range_hours=time_range_hours,
        quality_profile=quality_profile,
        output_type="clue_cards",
        require_cross_source=True if quality_profile == "high_precision" else False,
        require_evidence_chain=True,
        raw_query=query,
    )


def _fallback_plan(intent: UserIntent, runtime_context: Mapping[str, Any] | None = None) -> InvestigationPlan:
    threshold = 0.78 if intent.quality_profile == "high_precision" else 0.65
    agent_steps = [
        {"agent": "intent_planner", "action": "parse_request_to_structured_intent"},
        {"agent": "source_planner", "action": "select_authorized_sources_and_query_variants"},
    ]
    agent_steps.extend(
        [
            {"agent": "collection_agent", "action": "collect_authorized_records"},
            {"agent": "cleaning_agent", "action": "clean_dedup_and_preserve_signal"},
            {"agent": "classification_agent", "action": "classify_risk_intent"},
            {"agent": "extraction_agent", "action": "extract_key_entities"},
            {"agent": "clue_aggregation_agent", "action": "aggregate_risk_clues"},
            {"agent": "quality_review_agent", "action": "score_and_gate_high_quality_clues"},
            {"agent": "report_agent", "action": "render_investigation_output"},
        ]
    )
    source_selection_strategy = {
        "preferred_source_types": intent.source_preferences,
        "require_authorized_legal_basis": True,
        "match_query_keywords": intent.include_keywords,
        "collection_mode": "adaptive",
        "query_rewrite_policy": "auto",
    }
    execution_notes = [
        "Use external LLM for request understanding and plan generation.",
        "Keep collection inside authorized configured sources only.",
        "Use deterministic local execution for collection, cleaning, classification, extraction, and clue aggregation.",
        "refine_policy=budgeted",
    ]
    return InvestigationPlan(
        goal=intent.goal,
        agent_steps=agent_steps,
        selected_source_names=[],
        source_selection_strategy=source_selection_strategy,
        execution_notes=execution_notes,
        quality_gate={
            "quality_profile": intent.quality_profile,
            "minimum_quality_score": threshold,
            "require_cross_source": intent.require_cross_source,
            "require_evidence_chain": intent.require_evidence_chain,
        },
        budget={
            "max_sources": 6,
            "max_raw_records": 5000,
            "max_candidate_clues": 100,
            "max_llm_refine_clues": 20,
            "max_elapsed_seconds": DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS,
        },
        llm_ok=False,
        llm_reason="fallback_plan",
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, Iterable) or isinstance(value, (bytes, bytearray, str)):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            items.append(text)
    return _dedupe_list(items)


def _dedupe_list(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(item)
    return output


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_quality_profile(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"high_precision", "precision", "strict"}:
        return "high_precision"
    if text in {"high_recall", "recall"}:
        return "high_recall"
    return "balanced"


def _normalize_goal(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "collect_high_quality_risk_clues"
    # Keep the public API stable even when a real LLM paraphrases the goal in
    # Chinese or free text. Downstream orchestration currently has one supported
    # investigation goal: collect and gate risk clues.
    if text == "collect_high_quality_risk_clues":
        return "collect_high_quality_risk_clues"
    return "collect_high_quality_risk_clues"


def _intent_payload_usable(payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("goal") or payload.get("risk_types") or payload.get("source_preferences"))


def _plan_payload_usable(payload: Mapping[str, Any]) -> bool:
    return isinstance(payload.get("agent_steps"), list) or isinstance(payload.get("quality_gate"), Mapping)


def _normalize_budget(budget: Mapping[str, Any]) -> dict[str, int]:
    defaults = {
        "max_sources": 0,
        "max_raw_records": 5000,
        "max_candidate_clues": 100,
        "max_llm_refine_clues": 20,
        "max_elapsed_seconds": DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS,
    }
    normalized = dict(defaults)
    for key, default in defaults.items():
        if key == "max_sources":
            try:
                parsed = int(budget.get(key))
            except (TypeError, ValueError):
                parsed = default
            normalized[key] = parsed if parsed >= 0 else default
            continue
        normalized[key] = _coerce_positive_int(budget.get(key), default=default)
    return normalized


__all__ = [
    "InvestigationPlan",
    "DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS",
    "LLMDecisionTrace",
    "LLMInvestigationPlanner",
    "LLMUserRequestParser",
    "UserIntent",
]
