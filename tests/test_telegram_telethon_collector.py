from __future__ import annotations

import asyncio
import sys

import pytest

from scripts import telegram_telethon_collector as collector


class _FakeSocks:
    SOCKS4 = 1
    SOCKS5 = 2
    HTTP = 3


def test_build_proxy_returns_none_when_disabled():
    assert collector.build_proxy({}) is None
    assert collector.build_proxy({"enabled": False, "host": "127.0.0.1", "port": 7890}) is None


def test_build_proxy_builds_socks5_tuple(monkeypatch):
    monkeypatch.setitem(sys.modules, "socks", _FakeSocks)

    proxy = collector.build_proxy(
        {
            "enabled": True,
            "type": "socks5",
            "host": "127.0.0.1",
            "port": "7890",
            "rdns": True,
        }
    )

    assert proxy == (_FakeSocks.SOCKS5, "127.0.0.1", 7890, True, None, None)


def test_build_proxy_rejects_unknown_proxy_type(monkeypatch):
    monkeypatch.setitem(sys.modules, "socks", _FakeSocks)

    with pytest.raises(ValueError, match="unsupported telegram proxy type"):
        collector.build_proxy({"enabled": True, "type": "mtproto", "host": "127.0.0.1", "port": 7890})


def test_prepare_session_path_creates_parent_directory(tmp_path):
    session_path = tmp_path / "nested" / "blackagent_telegram"

    collector.prepare_session_path(session_path)

    assert session_path.parent.exists()


class _FakeClient:
    def __init__(self) -> None:
        self.start_kwargs = None

    async def start(self, **kwargs):
        self.start_kwargs = kwargs
        return self


def test_start_telegram_client_passes_configured_phone():
    client = _FakeClient()

    result = asyncio.run(collector.start_telegram_client(client, "+8613000000000"))

    assert result is client
    assert client.start_kwargs == {"phone": "+8613000000000"}


def test_telegram_watch_config_contains_curated_seed_usernames():
    from src.config_loader import load_yaml_file, resolve_project_path

    cfg = load_yaml_file(resolve_project_path("config/telegram_watch.example.yaml"))
    usernames = cfg["telegram"]["watch"]["usernames"]

    assert "haoshangmashang" in usernames
    assert "chaojiyun88" in usernames
    assert "paopaopayment" in usernames
    assert "Automationforum" not in usernames
    assert 5 <= len(usernames) <= 30


def test_build_collection_options_applies_cli_overrides(tmp_path):
    cfg = {
        "api_id": "123",
        "api_hash": "hash",
        "phone": "+8613000000000",
        "session": str(tmp_path / "session" / "tg"),
        "db": str(tmp_path / "telegram.db"),
        "source_name_prefix": "telegram_watch",
        "legal_basis": "AUTHORIZED_PARTNER",
        "watch": {
            "keywords": ["接码", "跑分"],
            "usernames": ["@one", "two", "two", ""],
            "invite_links": ["https://t.me/+abc"],
        },
        "collection": {
            "search_limit_per_keyword": 20,
            "history_limit_per_chat": 200,
            "include_keywords": ["接码"],
            "exclude_keywords": ["反诈"],
            "include_themes": ["接码"],
            "exclude_themes": [],
            "min_keyword_hits": 2,
            "save_jsonl_path": str(tmp_path / "raw.jsonl"),
        },
    }
    args = collector.CollectorCliOverrides(
        db=None,
        jsonl_path=None,
        username_limit=1,
        search_limit=3,
        history_limit=7,
    )

    options = collector.build_collection_options(cfg, args)

    assert options.api_id == "123"
    assert options.usernames == ["one"]
    assert options.invite_links == ["https://t.me/+abc"]
    assert options.search_limit == 3
    assert options.history_limit == 7
    assert options.min_keyword_hits == 2
    assert options.jsonl_path.name == "raw.jsonl"


def test_build_collection_options_rejects_missing_api_fields(tmp_path):
    cfg = {"api_id": "123", "api_hash": None, "watch": {}, "collection": {}}
    args = collector.CollectorCliOverrides(db=None, jsonl_path=None, username_limit=0, search_limit=0, history_limit=0)

    with pytest.raises(SystemExit, match="telegram.api_id and telegram.api_hash are required"):
        collector.build_collection_options(cfg, args)
