"""Report formatting helpers for application outputs."""

from __future__ import annotations

from typing import Any, Mapping


class ReportService:
    """Render compact summaries without owning investigation logic."""

    def summarize_investigation(self, result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": result.get("status"),
            "query": result.get("query"),
            "high_quality_count": result.get("high_quality_count", 0),
            "candidate_count": result.get("candidate_count", 0),
            "selected_source_count": result.get("selected_source_count", 0),
            "execution_summary": result.get("execution_summary") or {},
        }


__all__ = ["ReportService"]
