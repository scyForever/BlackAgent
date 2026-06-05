"""Minimal multi-turn conversation layer for investigation follow-ups."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from src.cleaner.text_filter import normalize_text


@dataclass(frozen=True)
class ConversationTurn:
    turn_id: str
    user_query: str
    intent_type: str
    resolved_payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConversationSession:
    session_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    active_clue_ids: list[str] = field(default_factory=list)
    active_entities: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    last_report: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def model_dump(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "turns": [turn.model_dump() for turn in self.turns],
        }

    def with_turn(self, turn: ConversationTurn) -> "ConversationSession":
        return ConversationSession(
            session_id=self.session_id,
            turns=[*self.turns, turn],
            active_clue_ids=list(self.active_clue_ids),
            active_entities=list(self.active_entities),
            filters=dict(self.filters),
            last_report=dict(self.last_report),
            created_at=self.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )


@dataclass(frozen=True)
class FollowupIntent:
    intent_type: str
    clue_index: int | None = None
    clue_id: str | None = None
    entity: str | None = None
    source_filter: str | None = None
    profile: str | None = None
    needs_rerun: bool = False
    needs_report: bool = False
    confidence: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class FollowupParser:
    """Parse common follow-up commands after an investigation result."""

    def parse(self, query: str) -> FollowupIntent:
        text = normalize_text(query)
        clue_index = _clue_index(text)
        entity = _entity_hint(text)
        source_filter = _source_filter(text)
        profile = _profile(text)
        if any(token in text for token in ("生成报告", "汇总报告", "输出报告")):
            return FollowupIntent("render_report", clue_index=clue_index, needs_report=True, confidence=0.88)
        if any(token in text for token in ("解释", "为什么", "依据")):
            return FollowupIntent("explain_clue", clue_index=clue_index, entity=entity, confidence=0.82)
        if any(token in text for token in ("展开", "查看", "详情")) and clue_index is not None:
            return FollowupIntent("expand_clue", clue_index=clue_index, confidence=0.86)
        if any(token in text for token in ("追踪", "继续查", "查这个", "关联")) and entity:
            return FollowupIntent("track_entity", entity=entity, confidence=0.84)
        if source_filter or profile or any(token in text for token in ("重跑", "再跑", "只看", "时间")):
            return FollowupIntent(
                "rerun_with_filters",
                clue_index=clue_index,
                source_filter=source_filter,
                profile=profile,
                needs_rerun=True,
                confidence=0.78,
            )
        return FollowupIntent("new_investigation", entity=entity, confidence=0.45)


class ConversationResolver:
    """Resolve follow-up intents against the current conversation state."""

    def resolve(self, session: ConversationSession, intent: FollowupIntent) -> dict[str, Any]:
        payload = intent.model_dump()
        if intent.clue_index is not None:
            index = intent.clue_index - 1
            if 0 <= index < len(session.active_clue_ids):
                payload["clue_id"] = session.active_clue_ids[index]
                payload["resolution_status"] = "resolved"
            else:
                payload["resolution_status"] = "missing_clue_index"
        elif intent.entity and intent.entity in session.active_entities:
            payload["resolution_status"] = "resolved"
        else:
            payload["resolution_status"] = "resolved_without_session_anchor" if intent.intent_type in {"render_report", "rerun_with_filters"} else "needs_new_investigation"
        return payload

    def append_turn(self, session: ConversationSession, query: str, intent: FollowupIntent) -> ConversationSession:
        resolved = self.resolve(session, intent)
        return session.with_turn(
            ConversationTurn(
                turn_id=f"turn_{uuid4().hex[:12]}",
                user_query=query,
                intent_type=intent.intent_type,
                resolved_payload=resolved,
            )
        )


class ConversationMemoryStore:
    """Small in-memory store; replaceable by SQL later."""

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}

    def create(self, *, session_id: str | None = None, **fields: Any) -> ConversationSession:
        session = ConversationSession(session_id=session_id or f"session_{uuid4().hex[:12]}", **fields)
        self._sessions[session.session_id] = session
        return session

    def save(self, session: ConversationSession) -> ConversationSession:
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ConversationSession | None:
        return self._sessions.get(session_id)

    def append_turn(self, session_id: str, query: str, intent: FollowupIntent) -> ConversationSession:
        session = self._sessions[session_id]
        updated = ConversationResolver().append_turn(session, query, intent)
        self._sessions[session_id] = updated
        return updated


def _clue_index(text: str) -> int | None:
    match = re.search(r"第\s*(\d+)\s*条", text)
    return int(match.group(1)) if match else None


def _entity_hint(text: str) -> str | None:
    match = re.search(r"(?:TG|telegram|@)[:：@]?\s*([A-Za-z][A-Za-z0-9_]{2,31})", text, flags=re.IGNORECASE)
    if match:
        return f"TG:{match.group(1)}"
    match = re.search(r"(?:实体|账号|域名|链接)\s*[:：]?\s*([A-Za-z0-9_.:/-]{3,80})", text)
    return match.group(1) if match else None


def _source_filter(text: str) -> str | None:
    lowered = text.lower()
    if "telegram" in lowered or "tg" in lowered or "电报" in text:
        return "telegram"
    if "论坛" in text or "forum" in lowered:
        return "forum"
    if "im" in lowered or "群聊" in text:
        return "im"
    return None


def _profile(text: str) -> str | None:
    if "高召回" in text or "high_recall" in text.lower():
        return "high_recall"
    if "高精度" in text or "high_precision" in text.lower():
        return "high_precision"
    if "快速" in text or "fast" in text.lower():
        return "fast"
    return None


__all__ = [
    "ConversationMemoryStore",
    "ConversationResolver",
    "ConversationSession",
    "ConversationTurn",
    "FollowupIntent",
    "FollowupParser",
]
