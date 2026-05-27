from __future__ import annotations

from scripts.collect_telegram_public_delivery import build_record, parse_page


class _Decision:
    matched_keywords = ("账号交易", "加v")
    excluded_keywords = ()
    matched_themes = ("账号交易",)
    excluded_themes = ()
    hit_count = 2
    policy_version = "keyword_relevance_v6"


def test_parse_page_preserves_photo_wrap_as_media_metadata():
    html = """
    <meta property="og:title" content="号商频道" />
    <div class="tgme_widget_message_wrap js-widget_message_wrap">
      <div class="tgme_widget_message" data-post="demochan/31">
      <time datetime="2026-05-27T10:00:00+00:00"></time>
      <a class="tgme_widget_message_photo_wrap x y" href="https://t.me/demochan/31"
         style="width:800px;background-image:url('https://cdn.example/31.jpg')">
        <div class="tgme_widget_message_photo" style="padding-top:70%"></div>
      </a>
      <div class="tgme_widget_message_text js-message_text" dir="auto">加薇 demo001，白号料子长期有货</div>
      </div>
    </div>
    """.strip()

    title, items = parse_page("demochan", "https://t.me/s/demochan", html)

    assert title == "号商频道"
    assert len(items) == 1
    assert items[0]["has_media"] is True
    assert items[0]["message_text_source"] == "photo_caption"
    assert items[0]["photo_urls"] == ["https://cdn.example/31.jpg"]


def test_build_record_writes_media_caption_into_attachments():
    record = build_record(
        source_name="telegram_public_delivery:demochan",
        message={
            "channel": "demochan",
            "channel_title": "号商频道",
            "post_id": 31,
            "publish_time": "2026-05-27T10:00:00+00:00",
            "content_text": "加薇 demo001，白号料子长期有货",
            "page_url": "https://t.me/s/demochan",
            "source_url": "https://t.me/demochan/31",
            "photo_urls": ["https://cdn.example/31.jpg"],
            "has_media": True,
            "message_text_source": "photo_caption",
        },
        decision=_Decision(),
    )

    assert record["has_media"] is True
    assert record["attachments"] == [
        {
            "type": "photo",
            "image_url": "https://cdn.example/31.jpg",
            "caption": "加薇 demo001，白号料子长期有货",
        }
    ]
