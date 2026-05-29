"""Local-only tool registry for the exploration sandbox."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Callable, Iterable

from .policy_guard import SafetyPolicyViolation


class ToolRegistryViolation(SafetyPolicyViolation):
    """Raised when an agent attempts to register or call an unapproved tool."""


class ToolRegistry:
    """Whitelist-based registry for sandbox tools.

    MVP policy allows only deterministic local tools.  The registry rejects
    network, shell, and write-oriented tools by name before the agent can call
    them.
    """

    DEFAULT_ALLOWED_TOOLS = frozenset({"local_db_lookup", "slang_similarity_search"})

    def __init__(self, allowed_tools: Iterable[str] | None = None, *, install_defaults: bool = True) -> None:
        self.allowed_tools = frozenset(allowed_tools or self.DEFAULT_ALLOWED_TOOLS)
        self._tools: dict[str, Callable[..., Any]] = {}
        if install_defaults:
            self._register_default("local_db_lookup", self._local_db_lookup)
            self._register_default("slang_similarity_search", self._slang_similarity_search)

    def register(self, name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator for registering a whitelisted local tool."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or func.__name__
            self._ensure_allowed(tool_name)
            self._tools[tool_name] = func
            return func

        return decorator

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a registered whitelisted tool."""

        self._ensure_allowed(name)
        if name not in self._tools:
            raise ToolRegistryViolation(f"Tool '{name}' is allowed but has not been registered.", rule="tool_unregistered", action=name)
        return self._tools[name](*args, **kwargs)

    def list_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def is_allowed(self, name: str) -> bool:
        return name in self.allowed_tools

    def _register_default(self, name: str, func: Callable[..., Any]) -> None:
        self._ensure_allowed(name)
        self._tools[name] = func

    def _ensure_allowed(self, name: str) -> None:
        if name not in self.allowed_tools:
            raise ToolRegistryViolation(
                f"Tool '{name}' is not in the controlled exploration whitelist.",
                rule="tool_not_allowed",
                action=name,
            )

    @staticmethod
    def _local_db_lookup(query: str, *, corpus: Iterable[Any] | None = None, limit: int = 3) -> list[Any]:
        """Return local records with simple token overlap against the query."""

        records = list(corpus or [])
        query_text = str(query or "").lower()
        query_tokens = {token for token in _split_tokens(query_text) if token}
        if not query_tokens:
            return records[:limit]

        scored: list[tuple[int, Any]] = []
        for record in records:
            record_text = _record_text(record).lower()
            overlap = len(query_tokens.intersection(_split_tokens(record_text)))
            if overlap:
                scored.append((overlap, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]

    @staticmethod
    def _slang_similarity_search(term: str, *, slang_terms: Iterable[str] | None = None, limit: int = 3) -> list[dict[str, Any]]:
        """Find locally configured slang candidates using character similarity."""

        raw_candidates = list(slang_terms or ("音符", "抖", "dy", "接码", "跑分", "上车", "料子"))
        term_text = str(term or "").lower()
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in raw_candidates:
            if isinstance(candidate, dict):
                candidate_term = str(candidate.get("term") or candidate.get("raw") or "").strip()
                normalized_term = str(candidate.get("normalized_term") or candidate.get("target") or candidate_term).strip() or candidate_term
            else:
                candidate_term = str(candidate).strip()
                normalized_term = candidate_term
            if not candidate_term:
                continue
            dedupe_key = candidate_term.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidate_text = candidate_term.lower()
            score = SequenceMatcher(None, term_text, candidate_text).ratio()
            if candidate_text in term_text:
                score = max(score, 0.92)
            if score >= 0.25:
                results.append({"term": candidate_term, "normalized_term": normalized_term, "score": round(score, 4)})
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]


def _record_text(record: Any) -> str:
    if isinstance(record, dict):
        return " ".join(str(value) for value in record.values() if value is not None)
    return str(record)


def _split_tokens(text: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " " for ch in text)
    chunks = set(normalized.split())
    chinese_chars = {ch for ch in normalized if "\u4e00" <= ch <= "\u9fff"}
    return chunks.union(chinese_chars)
