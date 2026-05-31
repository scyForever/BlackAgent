"""Runtime quality gate helpers for investigation runtime."""


from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Mapping

from src.config_loader import InvestigationConfig, InvestigationPolicyOverride
from src.domain import RunPolicyContext
from src.scheduling.layered_collection import (
    group_sources_by_collection_layer,
    prioritize_sources_for_investigation,
)
from src.safety import PIIMasker
from src.workflows import WorkflowContext

from .budget_controller import RuntimeBudget
from .investigation_contracts import (
    InvestigationRunResult,
    PlanExecutionControls,
    RuntimeQualityGate,
    SourceCollector,
    _FreshProcessingState,
    _LiveCollectionState,
    _RefinementState,
    _RetrievalState,
    _RunPlanningState,
    _SemanticLocalState,
)
from .user_request_parser import DEFAULT_INVESTIGATION_MAX_ELAPSED_SECONDS





class InvestigationQualityMixin:
    """Extracted helper group; state is supplied by InvestigationRuntime."""

    def _runtime_quality_gate(
            self,
            *,
            intent: Mapping[str, Any],
            plan: Mapping[str, Any],
            policy_override: InvestigationPolicyOverride | None,
        ) -> RuntimeQualityGate:
            plan_gate = plan.get("quality_gate") if isinstance(plan.get("quality_gate"), Mapping) else {}
            quality_profile = str(
                plan_gate.get("quality_profile")
                or intent.get("quality_profile")
                or "balanced"
            ).strip().lower()
            if quality_profile not in {"balanced", "high_precision", "high_recall"}:
                quality_profile = "balanced"
            minimum_quality_score = self._runtime_minimum_quality_score(
                quality_profile=quality_profile,
                plan_gate=plan_gate,
                policy_override=policy_override,
            )
            require_cross_source = self._runtime_gate_bool(
                override_value=getattr(policy_override, "require_cross_source", None) if policy_override else None,
                plan_value=plan_gate.get("require_cross_source"),
                intent_value=intent.get("require_cross_source"),
                default=False,
            )
            require_evidence_chain = self._runtime_gate_bool(
                override_value=getattr(policy_override, "require_evidence_chain", None) if policy_override else None,
                plan_value=plan_gate.get("require_evidence_chain"),
                intent_value=intent.get("require_evidence_chain"),
                default=True,
            )
            return RuntimeQualityGate(
                quality_profile=quality_profile,
                minimum_quality_score=minimum_quality_score,
                require_cross_source=require_cross_source,
                require_evidence_chain=require_evidence_chain,
            )


    def _runtime_minimum_quality_score(
            self,
            *,
            quality_profile: str,
            plan_gate: Mapping[str, Any],
            policy_override: InvestigationPolicyOverride | None,
        ) -> float:
            default_threshold = {
                "high_precision": 0.78,
                "high_recall": 0.52,
                "balanced": 0.65,
            }.get(quality_profile, 0.65)
            if policy_override and policy_override.minimum_quality_score is not None:
                return round(float(policy_override.minimum_quality_score), 4)
            plan_value = self._optional_float(plan_gate.get("minimum_quality_score"))
            if plan_value is not None:
                return round(plan_value, 4)
            return round(default_threshold, 4)

    @staticmethod

    def _runtime_gate_bool(
            *,
            override_value: bool | None,
            plan_value: Any,
            intent_value: Any,
            default: bool,
        ) -> bool:
            for value in (override_value, plan_value, intent_value):
                if isinstance(value, bool):
                    return value
                if value is None:
                    continue
                text = str(value).strip().lower()
                if text in {"1", "true", "yes", "y", "on"}:
                    return True
                if text in {"0", "false", "no", "n", "off"}:
                    return False
            return default


    def _passes_runtime_quality_gate(
            self,
            clue: Mapping[str, Any],
            *,
            quality_gate: RuntimeQualityGate,
        ) -> bool:
            quality = clue.get("quality") or {}
            quality_score = float(clue.get("quality_score") or 0.0)
            if quality_score < quality_gate.minimum_quality_score:
                return False
            cross_source_count = self._cross_source_count(clue)
            if quality_gate.require_cross_source and cross_source_count < 2:
                return False
            evidence_count = self._evidence_count(clue)
            if quality_gate.require_evidence_chain and evidence_count < 2:
                return False
            if "pass_threshold" in quality and bool(quality.get("pass_threshold")) is False and quality_score < quality_gate.minimum_quality_score:
                return False
            return True

    @staticmethod

    def _cross_source_count(clue: Mapping[str, Any]) -> int:
            return len({str(item) for item in (clue.get("source_names") or []) if str(item).strip()})

    @staticmethod

    def _evidence_count(clue: Mapping[str, Any]) -> int:
            return len({str(item) for item in (clue.get("evidence_trace_ids") or []) if str(item).strip()})


__all__ = ["InvestigationQualityMixin"]
