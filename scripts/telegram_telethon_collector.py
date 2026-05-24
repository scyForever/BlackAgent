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
from storage.sql_backend import connect


T_ME_INVITE_RE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)")


@dataclass
class TelegramTarget:
    title: str
    entity_ref: str
    source_url: str
    chat_id: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Telegram messages into BlackAgent raw_records.")
    parser.add_argument(
        "--config",
        default="config/telegram_watch.example.yaml",
        help="Project-relative YAML config path (default: config/telegram_watch.example.yaml)",
    )
    parser.add_argument("--fresh-state", action="store_true", help="Delete saved state before collecting")
    return parser.parse_args()


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
    api_id = telegram_cfg.get("api_id")
    api_hash = telegram_cfg.get("api_hash")
    phone = telegram_cfg.get("phone")
    if not api_id or not api_hash:
        raise SystemExit("telegram.api_id and telegram.api_hash are required")

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

    session_path = resolve_project_path(telegram_cfg.get("session") or "data/telethon/blackagent_telegram")
    db_path = resolve_project_path(telegram_cfg.get("db") or "data/blackagent_telegram.db")
    state_path = session_path.parent / (session_path.name + ".state.json")
    jsonl_path = telegram_cfg.get("collection", {}).get("save_jsonl_path")
    jsonl_file = resolve_project_path(jsonl_path) if jsonl_path else None
    source_name_prefix = str(telegram_cfg.get("source_name_prefix") or "telegram_watch")
    legal_basis = str(telegram_cfg.get("legal_basis") or "AUTHORIZED_PARTNER")
    keywords = [str(item).strip() for item in telegram_cfg.get("watch", {}).get("keywords", []) if str(item).strip()]
    usernames = [str(item).strip().lstrip("@") for item in telegram_cfg.get("watch", {}).get("usernames", []) if str(item).strip()]
    invite_links = [str(item).strip() for item in telegram_cfg.get("watch", {}).get("invite_links", []) if str(item).strip()]
    search_limit = int(telegram_cfg.get("collection", {}).get("search_limit_per_keyword", 20) or 20)
    history_limit = int(telegram_cfg.get("collection", {}).get("history_limit_per_chat", 200) or 200)
    download_media = bool(telegram_cfg.get("collection", {}).get("download_media", False))

    if args.fresh_state and state_path.exists():
        state_path.unlink()
    state = load_state(state_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()

    async with TelegramClient(str(session_path), int(api_id), str(api_hash)) as client:
        if phone:
            await client.start(phone=str(phone))
        else:
            await client.start()

        async def resolve_target_entities() -> list[Any]:
            found: dict[int, Any] = {}

            for username in usernames:
                entity = await client.get_entity(username)
                found[getattr(entity, "id")] = entity

            for keyword in keywords:
                try:
                    result = await client(functions.contacts.SearchRequest(q=keyword, limit=search_limit))
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

            for invite in invite_links:
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
        save_state(state_path, state)

        async def persist_message(entity: Any, message: Any) -> None:
            text = message_text(message)
            if not text:
                return
            chat_id = str(getattr(entity, "id"))
            last_id = int(state.setdefault("last_message_ids", {}).get(chat_id, 0) or 0)
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id and message_id <= last_id:
                return
            sender = await message.get_sender() if hasattr(message, "get_sender") else None
            payload = build_raw_record(
                message_text=text,
                source_name=f"{source_name_prefix}:{entity_title(entity)}",
                source_url=entity_source_url(entity),
                legal_basis=legal_basis,
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
                },
            )
            backend.save_raw(payload)
            append_jsonl(jsonl_file, payload)
            if message_id:
                state["last_message_ids"][chat_id] = message_id
                save_state(state_path, state)

        # Initial backfill
        for entity in joined_targets:
            async for msg in client.iter_messages(entity, limit=history_limit, reverse=True):
                await persist_message(entity, msg)
                if download_media and getattr(msg, "media", None):
                    media_dir = db_path.parent / "telegram_media" / str(getattr(entity, "id"))
                    media_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        await msg.download_media(file=str(media_dir))
                    except Exception:
                        pass

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
                    "db_path": str(db_path),
                    "tracked_chat_count": len(tracked_chat_ids),
                    "tracked_chats": list(state.get("targets", {}).values()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        await client.run_until_disconnected()

    backend.close()
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
