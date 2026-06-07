"""Continuously collect Telegram group/channel data with Telethon.

This script is intentionally scoped to authorized Telegram accounts and groups.
It discovers candidate public chats by keyword, joins configured targets, then
backfills and tails messages into the BlackAgent raw_records SQL table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_yaml_file, resolve_project_path
from src.collector.relevance import DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS, decide_text_relevance
from storage.sql_backend import connect


T_ME_INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class TelegramTarget:
    title: str
    entity_ref: str
    source_url: str
    chat_id: int | None = None


@dataclass(frozen=True)
class CollectorCliOverrides:
    db: str | None = None
    jsonl_path: str | None = None
    username_limit: int = 0
    search_limit: int = 0
    history_limit: int = 0


@dataclass(frozen=True)
class CollectionOptions:
    api_id: str
    api_hash: str
    phone: str | None
    proxy: tuple[Any, str, int, bool, str | None, str | None] | None
    session_path: Path
    db_path: Path
    state_path: Path
    jsonl_path: Path | None
    source_name_prefix: str
    legal_basis: str
    keywords: list[str]
    usernames: list[str]
    invite_links: list[str]
    search_limit: int
    history_limit: int
    download_media: bool
    include_keywords: list[str]
    exclude_keywords: list[str]
    include_themes: list[str]
    exclude_themes: list[str]
    min_keyword_hits: int


@dataclass
class TargetRunStats:
    chat_id: int | None
    title: str
    username: str | None
    source_url: str
    status: str = "pending"
    resolved: bool = False
    joined: bool = False
    backfilled_count: int = 0
    saved_count: int = 0
    skipped_irrelevant_count: int = 0
    skipped_duplicate_count: int = 0
    skipped_empty_count: int = 0
    error_stage: str | None = None
    error: str | None = None

    def mark_resolved(self) -> None:
        self.resolved = True
        if self.status == "pending":
            self.status = "resolved"

    def mark_joined(self) -> None:
        self.joined = True
        self.status = "joined"

    def record_backfilled(self, *, saved: bool, skip_reason: str | None = None) -> None:
        self.backfilled_count += 1
        if saved:
            self.saved_count += 1
            self.status = "collected"
            return
        if skip_reason == "duplicate":
            self.skipped_duplicate_count += 1
        elif skip_reason == "empty":
            self.skipped_empty_count += 1
        else:
            self.skipped_irrelevant_count += 1

    def record_failure(self, stage: str, error: Any) -> None:
        self.status = "failed"
        self.error_stage = stage
        self.error = str(error)

    def model_dump(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "username": self.username,
            "source_url": self.source_url,
            "status": self.status,
            "resolved": self.resolved,
            "joined": self.joined,
            "backfilled_count": self.backfilled_count,
            "saved_count": self.saved_count,
            "skipped_irrelevant_count": self.skipped_irrelevant_count,
            "skipped_duplicate_count": self.skipped_duplicate_count,
            "skipped_empty_count": self.skipped_empty_count,
            "error_stage": self.error_stage,
            "error": self.error,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Telegram messages into BlackAgent raw_records.")
    parser.add_argument(
        "--config",
        default="config/telegram_watch.example.yaml",
        help="Project-relative YAML config path (default: config/telegram_watch.example.yaml)",
    )
    parser.add_argument("--db", default=None, help="Optional DB path override")
    parser.add_argument("--jsonl-path", default=None, help="Optional JSONL output path override")
    parser.add_argument("--once", action="store_true", help="Backfill once and exit without tailing new messages")
    parser.add_argument("--fresh-state", action="store_true", help="Delete saved state before collecting")
    parser.add_argument("--username-limit", type=int, default=0, help="Limit configured usernames for smoke runs")
    parser.add_argument("--search-limit", type=int, default=0, help="Override keyword search limit per keyword")
    parser.add_argument("--history-limit", type=int, default=0, help="Override backfill history limit per chat")
    return parser.parse_args()


def build_proxy(proxy_cfg: dict[str, Any] | None) -> tuple[Any, str, int, bool, str | None, str | None] | None:
    cfg = dict(proxy_cfg or {})
    if not cfg or not bool(cfg.get("enabled", True)):
        return None
    host = str(cfg.get("host") or "").strip()
    port = int(cfg.get("port") or 0)
    if not host or port <= 0:
        return None

    try:
        import socks
    except ImportError as exc:
        raise SystemExit("Telegram proxy support requires PySocks. Install with: pip install PySocks") from exc

    proxy_type_name = str(cfg.get("type") or "socks5").strip().lower()
    proxy_types = {
        "socks4": socks.SOCKS4,
        "socks5": socks.SOCKS5,
        "http": socks.HTTP,
        "https": socks.HTTP,
    }
    if proxy_type_name not in proxy_types:
        raise ValueError(f"unsupported telegram proxy type: {proxy_type_name}")
    username = cfg.get("username")
    password = cfg.get("password")
    return (
        proxy_types[proxy_type_name],
        host,
        port,
        bool(cfg.get("rdns", True)),
        str(username) if username else None,
        str(password) if password else None,
    )


def prepare_session_path(session_path: Path) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)


def dedupe_texts(values: Iterable[Any], *, strip_at: bool = False) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if strip_at:
            text = text.lstrip("@")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def limit_items(values: list[str], limit: int) -> list[str]:
    if limit and limit > 0:
        return values[:limit]
    return values


def build_collection_options(telegram_cfg: dict[str, Any], overrides: CollectorCliOverrides) -> CollectionOptions:
    api_id = telegram_cfg.get("api_id")
    api_hash = telegram_cfg.get("api_hash")
    if not api_id or not api_hash:
        raise SystemExit("telegram.api_id and telegram.api_hash are required")

    collection_cfg = telegram_cfg.get("collection") or {}
    watch_cfg = telegram_cfg.get("watch") or {}
    session_path = resolve_project_path(telegram_cfg.get("session") or "data/telethon/blackagent_telegram")
    db_path = resolve_project_path(overrides.db or telegram_cfg.get("db") or "data/blackagent_telegram.db")
    state_path = session_path.parent / (session_path.name + ".state.json")
    jsonl_value = overrides.jsonl_path or collection_cfg.get("save_jsonl_path")
    keywords = dedupe_texts(watch_cfg.get("keywords", []))
    usernames = limit_items(dedupe_texts(watch_cfg.get("usernames", []), strip_at=True), overrides.username_limit)
    include_keywords = dedupe_texts(collection_cfg.get("include_keywords") or keywords)

    return CollectionOptions(
        api_id=str(api_id),
        api_hash=str(api_hash),
        phone=str(telegram_cfg.get("phone")) if telegram_cfg.get("phone") else None,
        proxy=build_proxy(telegram_cfg.get("proxy")),
        session_path=session_path,
        db_path=db_path,
        state_path=state_path,
        jsonl_path=resolve_project_path(jsonl_value) if jsonl_value else None,
        source_name_prefix=str(telegram_cfg.get("source_name_prefix") or "telegram_watch"),
        legal_basis=str(telegram_cfg.get("legal_basis") or "AUTHORIZED_PARTNER"),
        keywords=keywords,
        usernames=usernames,
        invite_links=dedupe_texts(watch_cfg.get("invite_links", [])),
        search_limit=positive_int(overrides.search_limit or collection_cfg.get("search_limit_per_keyword"), default=20),
        history_limit=positive_int(overrides.history_limit or collection_cfg.get("history_limit_per_chat"), default=200),
        download_media=bool(collection_cfg.get("download_media", False)),
        include_keywords=include_keywords,
        exclude_keywords=dedupe_texts(collection_cfg.get("exclude_keywords") or list(DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS)),
        include_themes=dedupe_texts(collection_cfg.get("include_themes") or []),
        exclude_themes=dedupe_texts(collection_cfg.get("exclude_themes") or []),
        min_keyword_hits=positive_int(collection_cfg.get("min_keyword_hits"), default=1),
    )


async def start_telegram_client(client: Any, phone: str | None) -> Any:
    if phone:
        await client.start(phone=str(phone))
    else:
        await client.start()
    return client


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_raw_record(
    *,
    message_text: str,
    source_name: str,
    source_url: str,
    legal_basis: str,
    publish_time: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(message_text or "").strip()
    payload = {
        "hash_id": sha256(text.encode("utf-8")).hexdigest(),
        "trace_id": str(uuid4()),
        "source_type": "IM",
        "source_name": source_name,
        "source_url": source_url,
        "capture_snapshot_uri": "",
        "collector_version": "telethon_collector_v1",
        "raw_payload_uri": source_url,
        "legal_basis": legal_basis,
        "crawl_time": utc_now_iso(),
        "publish_time": publish_time or utc_now_iso(),
        "content_text": text,
    }
    if extra:
        payload.update(extra)
    return payload


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"last_message_ids": {}, "targets": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_last_message_id(state: dict[str, Any], *, chat_id: str, message_id: int) -> bool:
    if message_id <= 0:
        return False
    last_ids = state.setdefault("last_message_ids", {})
    previous = int(last_ids.get(str(chat_id), 0) or 0)
    if message_id <= previous:
        return False
    last_ids[str(chat_id)] = message_id
    return True


def append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(payload, ensure_ascii=False) + "\n")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def entity_title(entity: Any) -> str:
    return normalize_text(
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or getattr(entity, "username", None)
        or getattr(entity, "id", "telegram_entity")
    )


def entity_username(entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    return str(username) if username else None


def entity_source_url(entity: Any) -> str:
    username = entity_username(entity)
    if username:
        return f"https://t.me/{username}"
    return f"telegram://chat?id={getattr(entity, 'id', 'unknown')}"


def message_text(message: Any) -> str:
    return normalize_text(
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or ""
    )


async def run() -> int:
    args = parse_args()
    cfg = load_yaml_file(resolve_project_path(args.config))
    telegram_cfg = cfg.get("telegram") or {}
    options = build_collection_options(
        telegram_cfg,
        CollectorCliOverrides(
            db=args.db,
            jsonl_path=args.jsonl_path,
            username_limit=args.username_limit,
            search_limit=args.search_limit,
            history_limit=args.history_limit,
        ),
    )

    try:
        from telethon import TelegramClient, events, functions
        from telethon.errors import (
            ChannelPrivateError,
            FloodWaitError,
            InviteHashExpiredError,
            InviteHashInvalidError,
            UserAlreadyParticipantError,
        )
        from telethon.tl.types import Channel, Chat
    except ImportError as exc:
        raise SystemExit("Telethon is not installed. Install with: pip install telethon") from exc

    prepare_session_path(options.session_path)
    if args.fresh_state and options.state_path.exists():
        options.state_path.unlink()
    state = load_state(options.state_path)

    options.db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = connect(f"sqlite:///{options.db_path.as_posix()}")
    backend.create_schema()
    persisted_count = 0

    client = TelegramClient(str(options.session_path), int(options.api_id), options.api_hash, proxy=options.proxy)
    try:
        await start_telegram_client(client, options.phone)

        async def resolve_target_entities() -> list[Any]:
            found: dict[int, Any] = {}

            for username in options.usernames:
                entity = await client.get_entity(username)
                found[getattr(entity, "id")] = entity

            for keyword in options.keywords:
                try:
                    result = await client(functions.contacts.SearchRequest(q=keyword, limit=options.search_limit))
                except Exception:
                    continue
                for chat in getattr(result, "chats", []):
                    chat_id = getattr(chat, "id", None)
                    if chat_id is None:
                        continue
                    # Prefer public supergroups / channels discoverable by username/title.
                    if getattr(chat, "deactivated", False):
                        continue
                    if not (getattr(chat, "megagroup", False) or getattr(chat, "broadcast", False) or getattr(chat, "username", None)):
                        continue
                    found[chat_id] = chat

            for invite in options.invite_links:
                entity = await join_invite(invite)
                if entity is not None:
                    found[getattr(entity, "id")] = entity

            return list(found.values())

        async def join_invite(invite: str) -> Any | None:
            match = T_ME_INVITE_RE.search(invite)
            if not match:
                return None
            invite_hash = match.group(1)
            try:
                updates = await client(functions.messages.ImportChatInviteRequest(invite_hash))
            except UserAlreadyParticipantError:
                try:
                    return await client.get_entity(invite)
                except Exception:
                    return None
            except (InviteHashInvalidError, InviteHashExpiredError):
                return None
            except FloodWaitError:
                raise
            chats = list(getattr(updates, "chats", []) or [])
            return chats[0] if chats else None

        async def join_public(entity: Any) -> Any:
            try:
                await client(functions.channels.JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                return entity
            except ChannelPrivateError:
                return entity
            except FloodWaitError:
                raise
            except Exception:
                return entity
            return entity

        targets = await resolve_target_entities()
        joined_targets: list[Any] = []
        for entity in targets:
            if isinstance(entity, (Channel, Chat)):
                joined_targets.append(await join_public(entity))
            else:
                joined_targets.append(entity)

        tracked_chat_ids: list[int] = []
        for entity in joined_targets:
            chat_id = int(getattr(entity, "id"))
            tracked_chat_ids.append(chat_id)
            state.setdefault("targets", {})[str(chat_id)] = {
                "title": entity_title(entity),
                "username": entity_username(entity),
                "source_url": entity_source_url(entity),
            }
        save_state(options.state_path, state)

        async def persist_message(entity: Any, message: Any) -> None:
            nonlocal persisted_count
            text = message_text(message)
            if not text:
                return
            decision = decide_text_relevance(
                text,
                include_keywords=options.include_keywords,
                exclude_keywords=options.exclude_keywords,
                include_themes=options.include_themes,
                exclude_themes=options.exclude_themes,
                min_keyword_hits=options.min_keyword_hits,
            )
            if not decision.relevant:
                return
            chat_id = str(getattr(entity, "id"))
            last_id = int(state.setdefault("last_message_ids", {}).get(chat_id, 0) or 0)
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id and message_id <= last_id:
                return
            sender = await message.get_sender() if hasattr(message, "get_sender") else None
            payload = build_raw_record(
                message_text=text,
                source_name=f"{options.source_name_prefix}:{entity_title(entity)}",
                source_url=entity_source_url(entity),
                legal_basis=options.legal_basis,
                publish_time=(getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None),
                extra={
                    "chat_id": getattr(entity, "id", None),
                    "chat_title": entity_title(entity),
                    "chat_username": entity_username(entity),
                    "message_id": message_id,
                    "sender_id": getattr(sender, "id", None) if sender is not None else None,
                    "sender_username": getattr(sender, "username", None) if sender is not None else None,
                    "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
                    "has_media": bool(getattr(message, "media", None)),
                    "matched_keywords": list(decision.matched_keywords),
                    "excluded_keywords": list(decision.excluded_keywords),
                    "matched_themes": list(decision.matched_themes),
                    "excluded_themes": list(decision.excluded_themes),
                    "keyword_hit_count": decision.hit_count,
                    "relevance_version": decision.policy_version,
                },
            )
            backend.save_raw(payload)
            append_jsonl(options.jsonl_path, payload)
            persisted_count += 1
            if update_last_message_id(state, chat_id=chat_id, message_id=message_id):
                save_state(options.state_path, state)

        # Initial backfill
        for entity in joined_targets:
            async for msg in client.iter_messages(entity, limit=options.history_limit, reverse=True):
                await persist_message(entity, msg)
                if options.download_media and getattr(msg, "media", None):
                    media_dir = options.db_path.parent / "telegram_media" / str(getattr(entity, "id"))
                    media_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        await msg.download_media(file=str(media_dir))
                    except Exception:
                        pass

        if args.once:
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "mode": "backfill_once",
                        "db_path": str(options.db_path),
                        "tracked_chat_count": len(tracked_chat_ids),
                        "persisted_count": persisted_count,
                        "tracked_chats": list(state.get("targets", {}).values()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            @client.on(events.NewMessage(chats=tracked_chat_ids))
            async def on_new_message(event: Any) -> None:
                try:
                    chat = await event.get_chat()
                    await persist_message(chat, event.message)
                except FloodWaitError as exc:
                    await asyncio.sleep(int(getattr(exc, "seconds", 5) or 5))

            print(
                json.dumps(
                    {
                        "status": "running",
                        "mode": "tail",
                        "db_path": str(options.db_path),
                        "tracked_chat_count": len(tracked_chat_ids),
                        "persisted_count": persisted_count,
                        "tracked_chats": list(state.get("targets", {}).values()),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            await client.run_until_disconnected()

    finally:
        backend.close()
        if client.is_connected():
            await client.disconnect()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
