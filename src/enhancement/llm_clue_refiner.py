"""LLM-backed refinement for top-N candidate clues."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from src.backend import LLMGateway
from src.safety import OutputValidator, PIIMasker, PromptGuard
from src.safety.prompt_sanitizer import sanitize_clue_for_llm, stable_clue_card_id, stable_clue_refine_cache_key, stable_json_dumps


@dataclass(frozen=True)
class RefinedClue:
    clue_id: str
    refined_summary: str
    confidence_delta: float
    final_confidence: float
    review_required: bool
    refinement_reasons: list[str]
    llm_ok: bool
    used_fallback: bool

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class LLMClueRefiner:
    """Refine high-value clue cards with an external LLM, fallback locally when needed."""

    def __init__(self, llm_gateway: LLMGateway) -> None:
        self.llm_gateway = llm_gateway

    def refine(
        self,
        clue: Mapping[str, Any],
        *,
        query: str,
        intent: Mapping[str, Any],
        runtime_context: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime_context = dict(runtime_context or {})
        few_shot_examples = runtime_context.get("few_shot_examples") if isinstance(runtime_context.get("few_shot_examples"), list) else []
        slang_terms = runtime_context.get("slang_terms") if isinstance(runtime_context.get("slang_terms"), list) else []
        prompt_guard = PromptGuard()
        clue_card = sanitize_clue_for_llm(clue, stable_id=False)
        guarded_query = prompt_guard.wrap_untrusted_text(query)
        guarded_clue = prompt_guard.wrap_untrusted_text(stable_json_dumps(clue_card))
        response = self.llm_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are BlackAgent's clue refiner. Return only JSON with fields: "
                        "refined_summary, confidence_delta, review_required, refinement_reasons. "
                        "Summarize why this clue matters for the current investigation request. "
                        "Use runtime slang normalization and approved review examples when they help explain the clue."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"query={guarded_query}\n"
                        f"intent={dict(intent)}\n"
                        f"clue_card={guarded_clue}\n"
                        f"runtime_slang_terms={slang_terms[:12]}\n"
                        f"runtime_few_shot_examples={few_shot_examples[:4]}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        parsed = response.parsed_json or {}
        usable = isinstance(parsed.get("refined_summary"), str)
        payload = _fallback_refinement(clue) if not usable else _normalize_payload(parsed, clue)
        payload["refined_summary"] = _safe_summary(payload["refined_summary"])
        final_confidence = round(max(0.0, min(float(clue.get("confidence") or 0.0) + float(payload["confidence_delta"]), 0.99)), 4)
        refined = RefinedClue(
            clue_id=str(clue.get("clue_id") or "unknown_clue"),
            refined_summary=str(payload["refined_summary"]),
            confidence_delta=float(payload["confidence_delta"]),
            final_confidence=final_confidence,
            review_required=bool(payload["review_required"]),
            refinement_reasons=[str(item) for item in payload.get("refinement_reasons", [])],
            llm_ok=response.ok,
            used_fallback=not usable,
        )
        enriched = dict(clue)
        enriched["refinement"] = refined.model_dump()
        enriched["confidence"] = final_confidence
        trace = {
            "stage": "clue_refine",
            "clue_id": refined.clue_id,
            "llm_ok": response.ok,
            "used_fallback": not usable,
            "runtime_slang_term_count": len(slang_terms),
            "runtime_few_shot_count": len(few_shot_examples),
            "parsed_json": parsed if parsed else None,
            "error": response.error,
        }
        return enriched, trace

    def refine_batch(
        self,
        clues: list[Mapping[str, Any]],
        *,
        query: str,
        intent: Mapping[str, Any],
        runtime_context: Mapping[str, Any] | None = None,
        max_tokens: int = 900,
        deadline_ms: int | None = None,
        budget: Any | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Refine multiple clue cards in one LLM call with per-clue fallback."""

        materialized = [dict(clue) for clue in clues]
        if not materialized:
            return [], []
        runtime_context = dict(runtime_context or {})
        few_shot_examples = runtime_context.get("few_shot_examples") if isinstance(runtime_context.get("few_shot_examples"), list) else []
        slang_terms = runtime_context.get("slang_terms") if isinstance(runtime_context.get("slang_terms"), list) else []
        prompt_guard = PromptGuard()
        clue_cards = [sanitize_clue_for_llm(clue, stable_id=True) for clue in materialized]
        card_id_by_clue_id = {
            str(clue.get("clue_id") or "unknown_clue"): str(card.get("clue_id") or stable_clue_card_id(clue))
            for clue, card in zip(materialized, clue_cards, strict=False)
        }
        guarded_query = prompt_guard.wrap_untrusted_text(query)
        guarded_clues = prompt_guard.wrap_untrusted_text(stable_json_dumps(clue_cards))
        response = self.llm_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are BlackAgent's batch clue refiner. Return only JSON with an items array. "
                        "Each item must contain clue_id, refined_summary, confidence_delta, "
                        "review_required, and refinement_reasons. Do not invent facts beyond the evidence."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"query={guarded_query}\n"
                        f"intent={dict(intent)}\n"
                        f"clue_cards={guarded_clues}\n"
                        f"runtime_slang_terms={slang_terms[:12]}\n"
                        f"runtime_few_shot_examples={few_shot_examples[:4]}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            stage="clue_refine",
            budget=budget,
            cache_policy="read_write",
            cache_key=stable_clue_refine_cache_key(materialized, query=query, intent=intent),
            deadline_ms=deadline_ms,
            extra_body={"budget_item_count": len(materialized)},
        )
        parsed = response.parsed_json or {}
        parsed_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
        payload_by_id = {
            str(item.get("clue_id") or ""): item
            for item in parsed_items
            if isinstance(item, Mapping) and str(item.get("clue_id") or "").strip()
        }

        enriched_items: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        for clue in materialized:
            clue_id = str(clue.get("clue_id") or "unknown_clue")
            card_id = card_id_by_clue_id.get(clue_id, clue_id)
            candidate_payload = payload_by_id.get(card_id) or payload_by_id.get(clue_id)
            usable = isinstance(candidate_payload, Mapping) and isinstance(candidate_payload.get("refined_summary"), str)
            payload = _fallback_refinement(clue) if not usable else _normalize_payload(candidate_payload, clue)
            payload["refined_summary"] = _safe_summary(payload["refined_summary"])
            final_confidence = round(max(0.0, min(float(clue.get("confidence") or 0.0) + float(payload["confidence_delta"]), 0.99)), 4)
            refined = RefinedClue(
                clue_id=clue_id,
                refined_summary=str(payload["refined_summary"]),
                confidence_delta=float(payload["confidence_delta"]),
                final_confidence=final_confidence,
                review_required=bool(payload["review_required"]),
                refinement_reasons=[str(item) for item in payload.get("refinement_reasons", [])],
                llm_ok=response.ok,
                used_fallback=not usable,
            )
            enriched = dict(clue)
            enriched["refinement"] = refined.model_dump()
            enriched["confidence"] = final_confidence
            enriched_items.append(enriched)
            traces.append(
                {
                    "stage": "clue_refine",
                    "mode": "batch",
                    "clue_id": clue_id,
                    "prompt_card_id": card_id,
                    "llm_ok": response.ok,
                    "used_fallback": not usable,
                    "runtime_slang_term_count": len(slang_terms),
                    "runtime_few_shot_count": len(few_shot_examples),
                    "parsed_json": candidate_payload if candidate_payload else None,
                    "error": response.error,
                }
            )
        return enriched_items, traces


def _normalize_payload(payload: Mapping[str, Any], clue: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "refined_summary": str(payload.get("refined_summary") or _fallback_refinement(clue)["refined_summary"]),
        "confidence_delta": _coerce_delta(payload.get("confidence_delta")),
        "review_required": _coerce_bool(payload.get("review_required"), default=False),
        "refinement_reasons": _string_list(payload.get("refinement_reasons")) or _fallback_refinement(clue)["refinement_reasons"],
    }


def _fallback_refinement(clue: Mapping[str, Any]) -> dict[str, Any]:
    clue_type = str(clue.get("clue_type") or "risk_clue")
    key = str(clue.get("key") or "")
    sources = [str(item) for item in (clue.get("source_names") or [])]
    entities = [str(item) for item in (clue.get("entity_values") or [])[:3]]
    summary = f"候选线索 {clue_type} 命中 {key}，来源 {', '.join(sources[:3]) or 'unknown'}，关键实体 {', '.join(entities) or 'none'}。"
    return {
        "refined_summary": summary,
        "confidence_delta": 0.0,
        "review_required": bool(((clue.get('quality') or {}).get("review_required")) or clue.get("quality_level") != "high"),
        "refinement_reasons": ["deterministic_fallback_summary", "preserve_candidate_clue_contract"],
    }


def _safe_summary(text: Any) -> str:
    masked = PIIMasker().mask_text(str(text or ""))
    OutputValidator().reject_dangerous_text(masked)
    return masked


def _coerce_delta(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(-0.2, min(parsed, 0.2))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


__all__ = ["LLMClueRefiner", "RefinedClue"]
