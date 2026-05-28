"""LLM-backed search-query rewrite before authorized source collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping
from urllib.parse import quote

from src.backend import LLMGateway


@dataclass(frozen=True)
class QueryRewriteTrace:
    stage: str
    source_name: str
    llm_ok: bool
    used_fallback: bool
    applied: bool
    parsed_json: dict[str, Any] | None
    error: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class LLMSourceQueryRewriter:
    """Rewrite one source-level search query from the user investigation intent."""

    def __init__(self, llm_gateway: LLMGateway) -> None:
        self.llm_gateway = llm_gateway

    def rewrite(
        self,
        source: Mapping[str, Any],
        *,
        query: str,
        intent: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], QueryRewriteTrace]:
        source_payload = dict(source)
        source_name = str(source_payload.get("source_name") or "unknown_source")
        query_url_template = str(source_payload.get("query_url_template") or "").strip()
        if not query_url_template:
            unchanged = dict(source_payload)
            unchanged.setdefault("query_rewrite_applied", False)
            unchanged.setdefault("query_rewrite_reason", "source_has_no_query_template")
            return unchanged, QueryRewriteTrace(
                stage="source_query_rewrite",
                source_name=source_name,
                llm_ok=False,
                used_fallback=False,
                applied=False,
                parsed_json=None,
                error="source_has_no_query_template",
            )

        response = self.llm_gateway.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are BlackAgent's source query rewriter. "
                        "Rewrite one compliant public search query for the given source. "
                        "Return only JSON with fields: search_query, query_theme, query_term, "
                        "query_term_stage, rewrite_reason. "
                        "Keep existing site/domain constraints already present in the source metadata. "
                        "Use short high-signal keywords. query_term_stage must be core or variant."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"user_query={query}\n"
                        f"intent={dict(intent)}\n"
                        f"plan={dict(plan)}\n"
                        f"source={source_payload}"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=250,
            response_format={"type": "json_object"},
        )

        parsed = response.parsed_json or {}
        usable = isinstance(parsed.get("search_query"), str) and bool(str(parsed.get("search_query")).strip())
        rewrite = (
            _normalize_rewrite_payload(parsed, source_payload=source_payload, query=query, intent=intent)
            if usable
            else _fallback_rewrite(source_payload=source_payload, query=query, intent=intent)
        )

        rewritten = dict(source_payload)
        original_source_url = str(source_payload.get("source_url") or "")
        original_search_query = str(source_payload.get("search_query") or "").strip()
        rewritten["source_url_before_rewrite"] = original_source_url
        if original_search_query:
            rewritten["search_query_before_rewrite"] = original_search_query
        rewritten["search_query"] = rewrite["search_query"]
        rewritten["query_theme"] = rewrite["query_theme"]
        rewritten["query_term"] = rewrite["query_term"]
        rewritten["query_term_stage"] = rewrite["query_term_stage"]
        rewritten["query_rewrite_reason"] = rewrite["rewrite_reason"]
        rewritten["query_rewrite_applied"] = True
        rewritten["query_rewrite_used_fallback"] = not usable
        rewritten["source_url"] = _render_query_url(
            query_url_template,
            rewrite["search_query"],
            fallback=original_source_url,
        )

        return rewritten, QueryRewriteTrace(
            stage="source_query_rewrite",
            source_name=source_name,
            llm_ok=response.ok,
            used_fallback=not usable,
            applied=True,
            parsed_json=parsed if parsed else None,
            error=response.error,
        )


def _normalize_rewrite_payload(
    payload: Mapping[str, Any],
    *,
    source_payload: Mapping[str, Any],
    query: str,
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    fallback = _fallback_rewrite(source_payload=source_payload, query=query, intent=intent)
    return {
        "search_query": str(payload.get("search_query") or fallback["search_query"]).strip(),
        "query_theme": str(
            payload.get("query_theme") or source_payload.get("query_theme") or fallback["query_theme"] or ""
        ).strip()
        or None,
        "query_term": str(
            payload.get("query_term") or source_payload.get("query_term") or fallback["query_term"] or ""
        ).strip()
        or None,
        "query_term_stage": _normalize_stage(
            payload.get("query_term_stage") or source_payload.get("query_term_stage") or fallback["query_term_stage"]
        ),
        "rewrite_reason": str(payload.get("rewrite_reason") or "llm_rewrite").strip() or "llm_rewrite",
    }


def _fallback_rewrite(
    *,
    source_payload: Mapping[str, Any],
    query: str,
    intent: Mapping[str, Any],
) -> dict[str, Any]:
    existing_search_query = str(source_payload.get("search_query") or "").strip()
    if existing_search_query:
        return {
            "search_query": existing_search_query,
            "query_theme": source_payload.get("query_theme"),
            "query_term": source_payload.get("query_term"),
            "query_term_stage": _normalize_stage(source_payload.get("query_term_stage")),
            "rewrite_reason": "fallback_existing_search_query",
        }

    seed_terms = [str(item).strip() for item in (source_payload.get("query_seed_terms") or []) if str(item).strip()]
    focus_terms = _dedupe_terms(
        [
            *(intent.get("include_keywords") or []),
            *(intent.get("risk_types") or []),
            source_payload.get("query_theme") or "",
            source_payload.get("query_term") or "",
        ]
    )
    composed_terms = [*seed_terms, *focus_terms[:2]]
    search_query = " ".join(term for term in composed_terms if term).strip() or query.strip()
    return {
        "search_query": search_query,
        "query_theme": source_payload.get("query_theme"),
        "query_term": source_payload.get("query_term") or (focus_terms[0] if focus_terms else None),
        "query_term_stage": _normalize_stage(source_payload.get("query_term_stage")),
        "rewrite_reason": "fallback_composed_from_source_and_intent",
    }


def _render_query_url(query_url_template: str, search_query: str, *, fallback: str) -> str:
    if "{query}" not in query_url_template:
        return fallback or query_url_template
    return query_url_template.replace("{query}", quote(str(search_query).strip(), safe=""))


def _normalize_stage(value: Any) -> str:
    return "variant" if str(value or "").strip().lower() == "variant" else "core"


def _dedupe_terms(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(text)
    return result


__all__ = ["LLMSourceQueryRewriter", "QueryRewriteTrace"]
