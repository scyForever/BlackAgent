"""Message-level Telegram public-page collector for collection-phase delivery."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.relevance import DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS, decide_text_relevance
from src.config_loader import load_yaml_file, resolve_project_path
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


MESSAGE_WRAP_MARKER = '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
CAPTION_RE = re.compile(r'<div class="tgme_widget_message_caption[^"]*"[^>]*>(.*?)</div>', re.S)
POST_RE_TEMPLATE = r'data-post="{channel}/(\d+)"'
TIME_RE = re.compile(r'<time datetime="([^"]+)"')
TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]+)"')
PHOTO_WRAP_RE = re.compile(
    r'<a class="tgme_widget_message_photo_wrap[^"]*"[^>]*style="[^"]*background-image:url\(\'([^\']+)\'\)[^"]*"',
    re.S,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect message-level Telegram public pages into raw_records.")
    parser.add_argument(
        "--channels-config",
        default="config/telegram_public_delivery_channels.yaml",
        help="Project-relative YAML path listing channel/page budgets",
    )
    parser.add_argument("--db", default="data/collection_phase_delivery.db", help="SQLite DB path")
    parser.add_argument("--summary-path", default="data/collection_phase_delivery_telegram_summary.json", help="Summary JSON path")
    parser.add_argument("--fresh", action="store_true", help="Delete DB before collection")
    parser.add_argument("--min-records", type=int, default=4000, help="Stop after at least this many newly saved records")
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="Request timeout")
    parser.add_argument("--sleep-seconds", type=float, default=0.15, help="Inter-page sleep")
    return parser.parse_args()


def clean_html_text(raw_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def fetch_html(url: str, *, timeout_seconds: float) -> str:
    req = urllib_request.Request(
        url,
        headers={
            "User-Agent": "BlackAgent-TelegramPublicDelivery/0.1",
            "Accept": "text/html,*/*",
        },
        method="GET",
    )
    with urllib_request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - explicit public Telegram pages only
        return resp.read().decode("utf-8", errors="replace")


def parse_page(channel: str, page_url: str, html_text: str) -> tuple[str | None, list[dict[str, Any]]]:
    title_match = TITLE_RE.search(html_text)
    channel_title = html.unescape(title_match.group(1)) if title_match else None
    post_re = re.compile(POST_RE_TEMPLATE.format(channel=re.escape(channel)))
    items: list[dict[str, Any]] = []
    seen_post_ids: set[int] = set()

    for part in html_text.split(MESSAGE_WRAP_MARKER)[1:]:
        post_match = post_re.search(part)
        if not post_match:
            continue
        post_id = int(post_match.group(1))
        if post_id in seen_post_ids:
            continue
        seen_post_ids.add(post_id)

        text_match = TEXT_RE.search(part) or CAPTION_RE.search(part)
        if not text_match:
            continue
        content_text = clean_html_text(text_match.group(1))
        if not content_text:
            continue
        photo_urls = [html.unescape(item) for item in PHOTO_WRAP_RE.findall(part)]

        time_match = TIME_RE.search(part)
        publish_time = time_match.group(1) if time_match else None
        items.append(
            {
                "channel": channel,
                "channel_title": channel_title,
                "post_id": post_id,
                "publish_time": publish_time,
                "content_text": content_text,
                "page_url": page_url,
                "source_url": f"https://t.me/{channel}/{post_id}",
                "photo_urls": photo_urls,
                "has_media": bool(photo_urls),
                "message_text_source": "photo_caption" if photo_urls else "message_text",
            }
        )
    items.sort(key=lambda item: item["post_id"])
    return channel_title, items


def build_record(
    *,
    source_name: str,
    message: dict[str, Any],
    decision: Any,
) -> dict[str, Any]:
    unique_key = f'{message["channel"]}:{message["post_id"]}:{message["source_url"]}'
    crawl_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "hash_id": hashlib.sha256(unique_key.encode("utf-8")).hexdigest(),
        "trace_id": str(uuid4()),
        "source_type": "IM",
        "source_name": source_name,
        "source_url": message["source_url"],
        "capture_snapshot_uri": message["page_url"],
        "collector_version": "telegram_public_delivery_v1",
        "raw_payload_uri": message["page_url"],
        "legal_basis": "PUBLIC_COMPLIANT_DATA",
        "crawl_time": crawl_time,
        "publish_time": message.get("publish_time") or crawl_time,
        "content_text": message["content_text"],
        "channel": message["channel"],
        "channel_title": message.get("channel_title"),
        "post_id": message["post_id"],
        "has_media": bool(message.get("has_media")),
        "message_text_source": message.get("message_text_source") or "message_text",
        "matched_keywords": list(decision.matched_keywords),
        "excluded_keywords": list(decision.excluded_keywords),
        "matched_themes": list(decision.matched_themes),
        "excluded_themes": list(decision.excluded_themes),
        "keyword_hit_count": decision.hit_count,
        "relevance_version": decision.policy_version,
        "attachments": [
            {
                "type": "photo",
                "image_url": photo_url,
                "caption": message["content_text"],
            }
            for photo_url in (message.get("photo_urls") or [])
        ],
    }


def main() -> int:
    args = parse_args()
    config_path = resolve_project_path(args.channels_config)
    db_path = resolve_project_path(args.db)
    summary_path = resolve_project_path(args.summary_path)
    config = load_yaml_file(config_path)
    channels = config.get("channels") or []
    if not isinstance(channels, list) or not channels:
        raise SystemExit("channels-config must contain a non-empty channels list")

    source_name_prefix = str(config.get("source_name_prefix") or "telegram_public_delivery")
    if args.fresh and db_path.exists():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    existing_before = len(backend.list_raw())
    saved_count = 0
    total_messages_seen = 0
    channel_summaries: list[dict[str, Any]] = []

    for channel_cfg in channels:
        channel = str((channel_cfg or {}).get("channel") or "").strip()
        max_pages = int((channel_cfg or {}).get("max_pages") or 0)
        if not channel or max_pages <= 0:
            continue

        page_index = 0
        before: int | None = None
        seen_windows: set[tuple[int, int]] = set()
        channel_saved = 0
        channel_seen = 0
        channel_title: str | None = None
        first_relevant_page: dict[str, Any] | None = None

        while page_index < max_pages and saved_count < args.min_records:
            page_url = f"https://t.me/s/{channel}" + (f"?before={before}" if before is not None else "")
            try:
                html_text = fetch_html(page_url, timeout_seconds=args.timeout_seconds)
            except Exception as exc:  # best-effort collector; keep moving
                channel_summaries.append(
                    {
                        "channel": channel,
                        "channel_title": channel_title,
                        "status": "fetch_error",
                        "page_index": page_index + 1,
                        "page_url": page_url,
                        "error": str(exc),
                        "saved_count": channel_saved,
                        "message_count": channel_seen,
                    }
                )
                break

            channel_title, messages = parse_page(channel, page_url, html_text)
            if not messages:
                break
            min_post_id = min(item["post_id"] for item in messages)
            max_post_id = max(item["post_id"] for item in messages)
            window = (min_post_id, max_post_id)
            if window in seen_windows:
                break
            seen_windows.add(window)

            page_saved = 0
            for message in messages:
                total_messages_seen += 1
                channel_seen += 1
                decision = decide_text_relevance(
                    message["content_text"],
                    include_themes=["诈骗引流", "刷单作弊", "账号交易", "众包任务", "接码", "工具交易"],
                    exclude_keywords=DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS,
                    min_keyword_hits=1,
                )
                if not decision.relevant:
                    continue
                record = build_record(
                    source_name=f"{source_name_prefix}:{channel}",
                    message=message,
                    decision=decision,
                )
                backend.save_raw(record)
                saved_count += 1
                channel_saved += 1
                page_saved += 1
            if first_relevant_page is None and page_saved > 0:
                first_relevant_page = {
                    "page_url": page_url,
                    "page_index": page_index + 1,
                    "page_saved": page_saved,
                    "window": {"min_post_id": min_post_id, "max_post_id": max_post_id},
                }

            page_index += 1
            before = min_post_id
            if args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)

        channel_summaries.append(
            {
                "channel": channel,
                "channel_title": channel_title,
                "status": "completed",
                "pages_attempted": page_index,
                "message_count": channel_seen,
                "saved_count": channel_saved,
                "first_relevant_page": first_relevant_page,
            }
        )
        if saved_count >= args.min_records:
            break

    existing_after = len(backend.list_raw())
    backend.close()
    summary = {
        "status": "completed",
        "db_path": str(db_path),
        "existing_before": existing_before,
        "existing_after": existing_after,
        "new_saved_count": saved_count,
        "messages_seen": total_messages_seen,
        "target_min_records": args.min_records,
        "channels": channel_summaries,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
