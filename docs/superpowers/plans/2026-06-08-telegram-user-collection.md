# Telegram User Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Telegram user-state collection end to end, harden the Telethon collector, and expose it through BlackAgent's CLI/scheduler paths.

**Architecture:** Keep `scripts/telegram_telethon_collector.py` as the concrete Telethon implementation, but factor pure helpers inside the same module so they can be tested without live Telegram access. Reuse the existing raw-record SQL schema, relevance policy, public Telegram seed catalog, and scheduler task type `collect_telegram_watch`.

**Tech Stack:** Python 3.11, Telethon optional dependency, PyYAML config loading, SQLite via `storage.sql_backend`, pytest.

---

## File Structure

- Modify `config/telegram_watch.example.yaml`: replace the example single username with a conservative seed set copied from `config/telegram_public_delivery_channels.yaml`, add smaller default history/search limits for safe first runs, and keep the existing proxy/env references.
- Modify `scripts/telegram_telethon_collector.py`: add reusable config normalization, target summary structs, CLI smoke-run controls, resilient target resolution, monotonic state updates, per-target stats, and structured JSON summary.
- Modify `tests/test_telegram_telethon_collector.py`: add unit tests for seed normalization, CLI overrides, state monotonicity, summary stats, nonfatal target failures, duplicate skipping, and relevance skipping.
- Modify `src/blackagent/interfaces/cli/main.py`: add a `--collect-telegram` mode that delegates to the Telethon collector before the investigation path.
- Modify `scripts/run_agent_cli.py`: no behavior code; it re-exports packaged CLI symbols automatically after the packaged CLI changes. Only update imports if tests require explicit exported names.
- Modify `tests/test_run_agent_cli.py`: add tests for Telegram collection CLI argument parsing and delegated command behavior with monkeypatched collector.
- Modify `src/scheduling/cron_queue.py`: keep the existing `collect_telegram_watch` task type, but pass smoke-run-safe flags when payloads request them and include parsed collector output in task history.
- Modify `scripts/run_collection_scheduler.py`: expose Telegram one-shot scheduler payload controls if needed, such as `--telegram-history-limit` and `--telegram-username-limit`.
- Modify `tests/test_scheduler_runtime.py`: assert the default schedule contains `fast_telegram_collect` and that the scheduler dispatch command includes the configured Telegram config and `--once`.
- Modify `docs/telegram_collection.md`: update operator commands for target seeding, one-shot verification, CLI mode, and scheduler mode.

## Task 1: Seed Telegram Targets

**Files:**
- Modify: `config/telegram_watch.example.yaml`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write the failing test for curated username seeds**

Append this test to `tests/test_telegram_telethon_collector.py`:

```python
def test_telegram_watch_config_contains_curated_seed_usernames():
    from src.config_loader import load_yaml_file, resolve_project_path

    cfg = load_yaml_file(resolve_project_path("config/telegram_watch.example.yaml"))
    usernames = cfg["telegram"]["watch"]["usernames"]

    assert "haoshangmashang" in usernames
    assert "chaojiyun88" in usernames
    assert "paopaopayment" in usernames
    assert "Automationforum" not in usernames
    assert 5 <= len(usernames) <= 30
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_telegram_watch_config_contains_curated_seed_usernames -q
```

Expected: FAIL because the current config only includes `pythonzh` or does not contain the curated seed set.

- [ ] **Step 3: Update the Telegram watch config**

In `config/telegram_watch.example.yaml`, replace the current `watch.usernames` block with this exact conservative seed set:

```yaml
    usernames:
      - haoshangmashang
      - haoshango00
      - tgzs88
      - huzige1916
      - quannenghao
      - TGchengpin
      - chaojiyun88
      - jiema010101
      - inoprjiema
      - jiemaD2
      - sifangsms
      - thesupersms
      - tiger_sms_china
      - paopaopayment
      - xieyihao89
      - dongdongtgyinliu
      - heimayunkong1
      - pidanruanjian
      - laren_a2
      - tgheji66
```

Also change the first-run collection defaults to keep live smoke runs bounded:

```yaml
    search_limit_per_keyword: 8
    history_limit_per_chat: 50
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_telegram_watch_config_contains_curated_seed_usernames -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add -- config/telegram_watch.example.yaml tests/test_telegram_telethon_collector.py
git commit -m "Seed Telegram user-state watch targets"
```

## Task 2: Extract Collector Configuration Helpers

**Files:**
- Modify: `scripts/telegram_telethon_collector.py`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write failing tests for config normalization and CLI overrides**

Append these tests to `tests/test_telegram_telethon_collector.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_build_collection_options_applies_cli_overrides tests/test_telegram_telethon_collector.py::test_build_collection_options_rejects_missing_api_fields -q
```

Expected: FAIL because `CollectorCliOverrides` and `build_collection_options` do not exist.

- [ ] **Step 3: Add dataclasses and normalization helpers**

In `scripts/telegram_telethon_collector.py`, add these dataclasses after `TelegramTarget`:

```python
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
```

Add these helper functions below `prepare_session_path`:

```python
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
```

- [ ] **Step 4: Add CLI override arguments and use options in run**

In `parse_args()`, add:

```python
    parser.add_argument("--username-limit", type=int, default=0, help="Limit configured usernames for smoke runs")
    parser.add_argument("--search-limit", type=int, default=0, help="Override keyword search limit per keyword")
    parser.add_argument("--history-limit", type=int, default=0, help="Override backfill history limit per chat")
```

In `run()`, replace the local config variable extraction block from `telegram_cfg = ...` through `min_keyword_hits = ...` with:

```python
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
```

Then replace references:

```python
proxy -> options.proxy
session_path -> options.session_path
db_path -> options.db_path
state_path -> options.state_path
jsonl_file -> options.jsonl_path
source_name_prefix -> options.source_name_prefix
legal_basis -> options.legal_basis
keywords -> options.keywords
usernames -> options.usernames
invite_links -> options.invite_links
search_limit -> options.search_limit
history_limit -> options.history_limit
download_media -> options.download_media
include_keywords -> options.include_keywords
exclude_keywords -> options.exclude_keywords
include_themes -> options.include_themes
exclude_themes -> options.exclude_themes
min_keyword_hits -> options.min_keyword_hits
api_id -> options.api_id
api_hash -> options.api_hash
phone -> options.phone
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_build_collection_options_applies_cli_overrides tests/test_telegram_telethon_collector.py::test_build_collection_options_rejects_missing_api_fields -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add -- scripts/telegram_telethon_collector.py tests/test_telegram_telethon_collector.py
git commit -m "Normalize Telegram collector options"
```

## Task 3: Add Structured Collector Stats And Monotonic State

**Files:**
- Modify: `scripts/telegram_telethon_collector.py`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write failing tests for state and stats**

Append these tests:

```python
def test_update_last_message_id_is_monotonic():
    state = {"last_message_ids": {"123": 10}}

    assert collector.update_last_message_id(state, chat_id="123", message_id=8) is False
    assert state["last_message_ids"]["123"] == 10
    assert collector.update_last_message_id(state, chat_id="123", message_id=11) is True
    assert state["last_message_ids"]["123"] == 11


def test_target_run_stats_records_outcomes():
    stats = collector.TargetRunStats(chat_id=123, title="demo", username="demo", source_url="https://t.me/demo")

    stats.record_backfilled(saved=True)
    stats.record_backfilled(saved=False, skip_reason="irrelevant")
    stats.record_failure("join_failed", "private")

    assert stats.backfilled_count == 2
    assert stats.saved_count == 1
    assert stats.skipped_irrelevant_count == 1
    assert stats.status == "failed"
    assert stats.error_stage == "join_failed"
    assert stats.model_dump()["error"] == "private"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_update_last_message_id_is_monotonic tests/test_telegram_telethon_collector.py::test_target_run_stats_records_outcomes -q
```

Expected: FAIL because the stats class and state helper do not exist.

- [ ] **Step 3: Implement stats and monotonic state helper**

Add this dataclass after `CollectionOptions`:

```python
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
```

Add this helper below `save_state`:

```python
def update_last_message_id(state: dict[str, Any], *, chat_id: str, message_id: int) -> bool:
    if message_id <= 0:
        return False
    last_ids = state.setdefault("last_message_ids", {})
    previous = int(last_ids.get(str(chat_id), 0) or 0)
    if message_id <= previous:
        return False
    last_ids[str(chat_id)] = message_id
    return True
```

- [ ] **Step 4: Use the monotonic helper in `persist_message`**

Inside `persist_message`, replace:

```python
            if message_id:
                state["last_message_ids"][chat_id] = message_id
                save_state(state_path, state)
```

with:

```python
            if update_last_message_id(state, chat_id=chat_id, message_id=message_id):
                save_state(options.state_path, state)
```

Also ensure earlier `last_id` reads still use `state.setdefault("last_message_ids", {})`.

- [ ] **Step 5: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_update_last_message_id_is_monotonic tests/test_telegram_telethon_collector.py::test_target_run_stats_records_outcomes -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add -- scripts/telegram_telethon_collector.py tests/test_telegram_telethon_collector.py
git commit -m "Track Telegram collector run stats"
```

## Task 4: Make Target Resolution Nonfatal

**Files:**
- Modify: `scripts/telegram_telethon_collector.py`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write failing async tests for nonfatal username failures**

Append these tests:

```python
class _FakeEntity:
    def __init__(self, entity_id=1, title="Demo", username="demo"):
        self.id = entity_id
        self.title = title
        self.username = username
        self.megagroup = False
        self.broadcast = True


class _FakeSearchResult:
    def __init__(self, chats):
        self.chats = chats


class _FakeResolveClient:
    def __init__(self):
        self.calls = []

    async def get_entity(self, username):
        self.calls.append(username)
        if username == "bad":
            raise ValueError("not found")
        return _FakeEntity(entity_id=10, title="Good", username=username)

    async def __call__(self, request):
        return _FakeSearchResult([])


def test_resolve_target_entities_continues_after_bad_username():
    client = _FakeResolveClient()
    stats_by_key = {}

    entities = asyncio.run(
        collector.resolve_target_entities(
            client,
            usernames=["bad", "good"],
            keywords=[],
            invite_links=[],
            search_limit=0,
            stats_by_key=stats_by_key,
            functions=object(),
        )
    )

    assert [entity.username for entity in entities] == ["good"]
    assert stats_by_key["username:bad"].status == "failed"
    assert stats_by_key["username:good"].resolved is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_resolve_target_entities_continues_after_bad_username -q
```

Expected: FAIL because `resolve_target_entities` is currently nested in `run()`.

- [ ] **Step 3: Extract target resolution helper**

Move the nested `resolve_target_entities` logic out to module scope with this signature:

```python
async def resolve_target_entities(
    client: Any,
    *,
    usernames: list[str],
    keywords: list[str],
    invite_links: list[str],
    search_limit: int,
    stats_by_key: dict[str, TargetRunStats],
    functions: Any,
) -> list[Any]:
    found: dict[int, Any] = {}

    for username in usernames:
        key = f"username:{username}"
        try:
            entity = await client.get_entity(username)
        except Exception as exc:
            stats_by_key[key] = TargetRunStats(None, username, username, f"https://t.me/{username}")
            stats_by_key[key].record_failure("resolve_username", exc)
            continue
        found[getattr(entity, "id")] = entity
        stats = TargetRunStats(getattr(entity, "id", None), entity_title(entity), entity_username(entity), entity_source_url(entity))
        stats.mark_resolved()
        stats_by_key[key] = stats

    for keyword in keywords:
        try:
            result = await client(functions.contacts.SearchRequest(q=keyword, limit=search_limit))
        except Exception as exc:
            stats = TargetRunStats(None, f"search:{keyword}", None, f"telegram://search?q={keyword}")
            stats.record_failure("search_keyword", exc)
            stats_by_key[f"search:{keyword}"] = stats
            continue
        for chat in getattr(result, "chats", []):
            chat_id = getattr(chat, "id", None)
            if chat_id is None:
                continue
            if getattr(chat, "deactivated", False):
                continue
            if not (getattr(chat, "megagroup", False) or getattr(chat, "broadcast", False) or getattr(chat, "username", None)):
                continue
            found[chat_id] = chat

    for invite in invite_links:
        stats_by_key.setdefault(f"invite:{invite}", TargetRunStats(None, invite, None, invite))

    return list(found.values())
```

Leave invite joining in `run()` for now, but record invalid invite failures in Task 5.

In `run()`, delete the nested `resolve_target_entities` function and call the module-level helper:

```python
        target_stats: dict[str, TargetRunStats] = {}
        targets = await resolve_target_entities(
            client,
            usernames=options.usernames,
            keywords=options.keywords,
            invite_links=options.invite_links,
            search_limit=options.search_limit,
            stats_by_key=target_stats,
            functions=functions,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_resolve_target_entities_continues_after_bad_username -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add -- scripts/telegram_telethon_collector.py tests/test_telegram_telethon_collector.py
git commit -m "Keep Telegram target resolution nonfatal"
```

## Task 5: Add Structured Run Summary

**Files:**
- Modify: `scripts/telegram_telethon_collector.py`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write failing test for summary payload**

Append:

```python
def test_build_run_summary_includes_target_stats(tmp_path):
    stats = collector.TargetRunStats(chat_id=1, title="Demo", username="demo", source_url="https://t.me/demo")
    stats.mark_resolved()
    stats.mark_joined()
    stats.record_backfilled(saved=True)

    summary = collector.build_run_summary(
        status="completed",
        mode="backfill_once",
        db_path=tmp_path / "telegram.db",
        tracked_chat_count=1,
        persisted_count=1,
        target_stats=[stats],
    )

    assert summary["status"] == "completed"
    assert summary["persisted_count"] == 1
    assert summary["target_count"] == 1
    assert summary["failed_target_count"] == 0
    assert summary["targets"][0]["saved_count"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_build_run_summary_includes_target_stats -q
```

Expected: FAIL because `build_run_summary` does not exist.

- [ ] **Step 3: Implement summary helper**

Add below `append_jsonl`:

```python
def build_run_summary(
    *,
    status: str,
    mode: str,
    db_path: Path,
    tracked_chat_count: int,
    persisted_count: int,
    target_stats: Iterable[TargetRunStats],
) -> dict[str, Any]:
    targets = [item.model_dump() for item in target_stats]
    return {
        "status": status,
        "mode": mode,
        "db_path": str(db_path),
        "tracked_chat_count": tracked_chat_count,
        "persisted_count": persisted_count,
        "target_count": len(targets),
        "failed_target_count": sum(1 for item in targets if item.get("status") == "failed"),
        "saved_target_count": sum(1 for item in targets if int(item.get("saved_count") or 0) > 0),
        "targets": targets,
    }
```

- [ ] **Step 4: Replace print payloads in `run()`**

For `--once`, replace the inline `json.dumps({...})` with:

```python
            print(
                json.dumps(
                    build_run_summary(
                        status="completed",
                        mode="backfill_once",
                        db_path=options.db_path,
                        tracked_chat_count=len(tracked_chat_ids),
                        persisted_count=persisted_count,
                        target_stats=target_stats.values(),
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
```

For tail mode, use the same helper with `status="running"` and `mode="tail"`.

- [ ] **Step 5: Run test to verify it passes**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_build_run_summary_includes_target_stats -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add -- scripts/telegram_telethon_collector.py tests/test_telegram_telethon_collector.py
git commit -m "Print structured Telegram collector summaries"
```

## Task 6: Wire Stats Into Backfill And Skip Paths

**Files:**
- Modify: `scripts/telegram_telethon_collector.py`
- Test: `tests/test_telegram_telethon_collector.py`

- [ ] **Step 1: Write failing pure test for message persistence outcome names**

Append:

```python
def test_message_outcome_values_are_stable():
    assert collector.MESSAGE_OUTCOME_SAVED == "saved"
    assert collector.MESSAGE_OUTCOME_EMPTY == "empty"
    assert collector.MESSAGE_OUTCOME_IRRELEVANT == "irrelevant"
    assert collector.MESSAGE_OUTCOME_DUPLICATE == "duplicate"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_message_outcome_values_are_stable -q
```

Expected: FAIL because outcome constants do not exist.

- [ ] **Step 3: Add outcome constants and return outcomes from `persist_message`**

Add near the regex constant:

```python
MESSAGE_OUTCOME_SAVED = "saved"
MESSAGE_OUTCOME_EMPTY = "empty"
MESSAGE_OUTCOME_IRRELEVANT = "irrelevant"
MESSAGE_OUTCOME_DUPLICATE = "duplicate"
```

In nested `persist_message`, change returns:

```python
            if not text:
                return MESSAGE_OUTCOME_EMPTY
```

```python
            if not decision.relevant:
                return MESSAGE_OUTCOME_IRRELEVANT
```

```python
            if message_id and message_id <= last_id:
                return MESSAGE_OUTCOME_DUPLICATE
```

At the end after state save:

```python
            return MESSAGE_OUTCOME_SAVED
```

Update the function signature:

```python
        async def persist_message(entity: Any, message: Any) -> str:
```

- [ ] **Step 4: Record stats during backfill and tail**

Before backfill, create a helper in `run()`:

```python
        def stats_for_entity(entity: Any) -> TargetRunStats:
            key = f"chat:{getattr(entity, 'id', 'unknown')}"
            existing = target_stats.get(key)
            if existing is None:
                existing = TargetRunStats(
                    getattr(entity, "id", None),
                    entity_title(entity),
                    entity_username(entity),
                    entity_source_url(entity),
                )
                target_stats[key] = existing
            return existing
```

When adding joined targets, call:

```python
            stats_for_entity(entity).mark_joined()
```

During backfill, replace:

```python
                await persist_message(entity, msg)
```

with:

```python
                outcome = await persist_message(entity, msg)
                stats_for_entity(entity).record_backfilled(
                    saved=outcome == MESSAGE_OUTCOME_SAVED,
                    skip_reason=None if outcome == MESSAGE_OUTCOME_SAVED else outcome,
                )
```

In tail mode, after `await persist_message(chat, event.message)`, record the same outcome against `stats_for_entity(chat)`.

- [ ] **Step 5: Run the targeted test**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py::test_message_outcome_values_are_stable -q
```

Expected: PASS.

- [ ] **Step 6: Run all Telegram collector unit tests**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```powershell
git add -- scripts/telegram_telethon_collector.py tests/test_telegram_telethon_collector.py
git commit -m "Record Telegram backfill skip outcomes"
```

## Task 7: Add CLI Telegram Collection Mode

**Files:**
- Modify: `src/blackagent/interfaces/cli/main.py`
- Modify if required: `scripts/run_agent_cli.py`
- Test: `tests/test_run_agent_cli.py`

- [ ] **Step 1: Write failing CLI parse and delegation tests**

Append to `tests/test_run_agent_cli.py`:

```python
def test_cli_parses_collect_telegram_mode():
    args = parse_args(
        [
            "--collect-telegram",
            "--telegram-config",
            "config/telegram_watch.example.yaml",
            "--telegram-history-limit",
            "5",
            "--telegram-username-limit",
            "2",
            "--telegram-once",
        ]
    )

    assert args.collect_telegram is True
    assert args.telegram_config == "config/telegram_watch.example.yaml"
    assert args.telegram_history_limit == 5
    assert args.telegram_username_limit == 2
    assert args.telegram_once is True


def test_cli_collect_telegram_delegates_to_collector(monkeypatch, capsys):
    calls = []

    def fake_collect(argv):
        calls.append(argv)
        print('{"status":"completed","persisted_count":0}')
        return 0

    monkeypatch.setattr("blackagent.interfaces.cli.main.run_telegram_collection_cli", fake_collect)

    exit_code = main(
        [
            "--collect-telegram",
            "--telegram-config",
            "config/telegram_watch.example.yaml",
            "--telegram-history-limit",
            "5",
            "--telegram-username-limit",
            "2",
            "--telegram-once",
        ]
    )

    assert exit_code == 0
    assert calls == [
        [
            "--config",
            "config/telegram_watch.example.yaml",
            "--once",
            "--username-limit",
            "2",
            "--history-limit",
            "5",
        ]
    ]
    assert "completed" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
pytest tests/test_run_agent_cli.py::test_cli_parses_collect_telegram_mode tests/test_run_agent_cli.py::test_cli_collect_telegram_delegates_to_collector -q
```

Expected: FAIL because Telegram CLI arguments and `run_telegram_collection_cli` do not exist.

- [ ] **Step 3: Add parser arguments**

In `src/blackagent/interfaces/cli/main.py`, inside `parse_args`, add:

```python
    parser.add_argument("--collect-telegram", action="store_true", help="Run Telegram user-state collection and exit.")
    parser.add_argument("--telegram-config", default="config/telegram_watch.example.yaml", help="Telegram collector config path.")
    parser.add_argument("--telegram-db", default="", help="Optional Telegram collector DB path override.")
    parser.add_argument("--telegram-jsonl-path", default="", help="Optional Telegram collector JSONL output path override.")
    parser.add_argument("--telegram-once", action="store_true", help="Backfill once and exit.")
    parser.add_argument("--telegram-fresh-state", action="store_true", help="Delete Telegram collector state before running.")
    parser.add_argument("--telegram-username-limit", type=int, default=0, help="Limit configured Telegram usernames for smoke runs.")
    parser.add_argument("--telegram-search-limit", type=int, default=0, help="Override Telegram search limit per keyword.")
    parser.add_argument("--telegram-history-limit", type=int, default=0, help="Override Telegram history limit per chat.")
```

- [ ] **Step 4: Add delegation helper**

In `src/blackagent/interfaces/cli/main.py`, above `main`, add:

```python
def telegram_collector_argv_from_args(args: argparse.Namespace) -> list[str]:
    argv = ["--config", args.telegram_config]
    if args.telegram_once or args.collect_telegram:
        argv.append("--once")
    if args.telegram_db:
        argv.extend(["--db", args.telegram_db])
    if args.telegram_jsonl_path:
        argv.extend(["--jsonl-path", args.telegram_jsonl_path])
    if args.telegram_fresh_state:
        argv.append("--fresh-state")
    if args.telegram_username_limit:
        argv.extend(["--username-limit", str(args.telegram_username_limit)])
    if args.telegram_search_limit:
        argv.extend(["--search-limit", str(args.telegram_search_limit)])
    if args.telegram_history_limit:
        argv.extend(["--history-limit", str(args.telegram_history_limit)])
    return argv


def run_telegram_collection_cli(argv: list[str]) -> int:
    from scripts.telegram_telethon_collector import main as telegram_main

    return telegram_main(argv)
```

Update `scripts/telegram_telethon_collector.py` so `parse_args` and `main` accept argv:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ...
    return parser.parse_args(argv)
```

```python
async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
```

```python
def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))
```

- [ ] **Step 5: Branch early in CLI main**

In `src/blackagent/interfaces/cli/main.py`, immediately after `args = parse_args(argv)` and before query validation, add:

```python
    if args.collect_telegram:
        load_project_env_file()
        return run_telegram_collection_cli(telegram_collector_argv_from_args(args))
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_run_agent_cli.py::test_cli_parses_collect_telegram_mode tests/test_run_agent_cli.py::test_cli_collect_telegram_delegates_to_collector -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```powershell
git add -- src/blackagent/interfaces/cli/main.py scripts/telegram_telethon_collector.py tests/test_run_agent_cli.py
git commit -m "Expose Telegram collection in CLI"
```

## Task 8: Tighten Scheduler Telegram Dispatch

**Files:**
- Modify: `src/scheduling/cron_queue.py`
- Modify: `scripts/run_collection_scheduler.py`
- Test: `tests/test_scheduler_runtime.py`

- [ ] **Step 1: Write failing scheduler tests**

Append to `tests/test_scheduler_runtime.py`:

```python
def test_default_schedules_include_fast_telegram_collect(tmp_path):
    backend = connect(sqlite_dsn(tmp_path / "scheduler.db"))
    backend.create_schema()
    scheduler = CollectionQueueScheduler(backend)

    schedules = scheduler.default_schedules(
        telegram_config="config/telegram_watch.example.yaml",
        fast_interval_seconds=30,
    )

    telegram = next(item for item in schedules if item.schedule_name == "fast_telegram_collect")
    assert telegram.task_type == "collect_telegram_watch"
    assert telegram.task_payload["config"] == "config/telegram_watch.example.yaml"
    assert telegram.interval_seconds == 30
    backend.close()


def test_scheduler_dispatches_telegram_once_with_optional_limits(tmp_path):
    backend = connect(sqlite_dsn(tmp_path / "scheduler.db"))
    backend.create_schema()
    commands = []

    def fake_runner(command):
        commands.append(command)
        return {"status": "completed", "parsed_output": {"persisted_count": 0}}

    scheduler = CollectionQueueScheduler(backend, runner=fake_runner)
    job = {
        "job_id": "job-1",
        "task_type": "collect_telegram_watch",
        "task_payload": {
            "config": "config/telegram_watch.example.yaml",
            "username_limit": 2,
            "history_limit": 5,
            "search_limit": 3,
        },
    }

    result = scheduler._dispatch_builtin(job)

    command_text = " ".join(commands[0])
    assert "scripts/telegram_telethon_collector.py" in command_text
    assert "--once" in commands[0]
    assert "--username-limit" in commands[0]
    assert "2" in commands[0]
    assert "--history-limit" in commands[0]
    assert "5" in commands[0]
    assert result["subprocess"]["parsed_output"]["persisted_count"] == 0
    backend.close()
```

- [ ] **Step 2: Run tests to verify current behavior**

Run:

```powershell
pytest tests/test_scheduler_runtime.py::test_default_schedules_include_fast_telegram_collect tests/test_scheduler_runtime.py::test_scheduler_dispatches_telegram_once_with_optional_limits -q
```

Expected: first test may already PASS; second FAIL because optional limits are not passed.

- [ ] **Step 3: Add optional Telegram payload flags in scheduler dispatch**

In `src/scheduling/cron_queue.py`, replace the `collect_telegram_watch` command construction with:

```python
            command = [
                sys.executable,
                "scripts/telegram_telethon_collector.py",
                "--config",
                str(payload.get("config") or "config/telegram_watch.example.yaml"),
                "--db",
                self._scheduler_db_path,
                "--once",
            ]
            for payload_name, flag_name in (
                ("username_limit", "--username-limit"),
                ("search_limit", "--search-limit"),
                ("history_limit", "--history-limit"),
            ):
                value = payload.get(payload_name)
                if value:
                    command.extend([flag_name, str(value)])
            return self._execute_collection_job(job, command)
```

- [ ] **Step 4: Add scheduler CLI payload controls**

In `scripts/run_collection_scheduler.py`, add parse args:

```python
    parser.add_argument("--telegram-username-limit", type=int, default=0, help="Limit Telegram usernames in scheduled smoke runs")
    parser.add_argument("--telegram-search-limit", type=int, default=0, help="Override Telegram scheduled search limit")
    parser.add_argument("--telegram-history-limit", type=int, default=0, help="Override Telegram scheduled history limit")
```

Before calling `scheduler.sync_schedules(...)`, assign:

```python
    default_schedules = scheduler.default_schedules(
        public_catalog=args.public_catalog,
        x_config=args.x_config,
        telegram_config=args.telegram_config,
        fast_interval_seconds=settings.scheduler.fast_interval_seconds,
        slow_interval_seconds=settings.scheduler.slow_interval_seconds,
        clue_build_interval_seconds=settings.scheduler.clue_build_interval_seconds,
        lease_seconds=settings.scheduler.lease_seconds,
        max_attempts=settings.scheduler.max_attempts,
        cron_overrides=settings.scheduler.cron_overrides,
    )
    for schedule in default_schedules:
        if schedule.schedule_name == "fast_telegram_collect":
            if args.telegram_username_limit:
                schedule.task_payload["username_limit"] = args.telegram_username_limit
            if args.telegram_search_limit:
                schedule.task_payload["search_limit"] = args.telegram_search_limit
            if args.telegram_history_limit:
                schedule.task_payload["history_limit"] = args.telegram_history_limit
```

Then pass `default_schedules` to `scheduler.sync_schedules(default_schedules)`.

If `ScheduleDefinition` is frozen and direct mutation fails, replace the schedule item with:

```python
from dataclasses import replace
...
schedule = replace(schedule, task_payload={**schedule.task_payload, **telegram_payload})
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```powershell
pytest tests/test_scheduler_runtime.py::test_default_schedules_include_fast_telegram_collect tests/test_scheduler_runtime.py::test_scheduler_dispatches_telegram_once_with_optional_limits -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add -- src/scheduling/cron_queue.py scripts/run_collection_scheduler.py tests/test_scheduler_runtime.py
git commit -m "Pass Telegram scheduler smoke limits"
```

## Task 9: Update Operator Documentation

**Files:**
- Modify: `docs/telegram_collection.md`

- [ ] **Step 1: Update docs with current commands**

In `docs/telegram_collection.md`, update the startup section to include:

```markdown
## 推荐首次验证流程

配置好 `BLACKAGENT_TG_API_ID`、`BLACKAGENT_TG_API_HASH`、`BLACKAGENT_TG_PHONE` 和代理后，先跑小规模一次性回补：

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once --username-limit 2 --history-limit 10 --search-limit 2
```

CLI 入口等价命令：

```powershell
python scripts/run_agent_cli.py --collect-telegram --telegram-once --telegram-username-limit 2 --telegram-history-limit 10 --telegram-search-limit 2
```

调度器入口：

```powershell
python scripts/run_collection_scheduler.py --cycles 1 --telegram-username-limit 2 --telegram-history-limit 10 --telegram-search-limit 2
```

输出 JSON 中重点看：

- `tracked_chat_count`
- `persisted_count`
- `failed_target_count`
- `targets[].status`
- `targets[].error_stage`
```

- [ ] **Step 2: Commit**

Run:

```powershell
git add -- docs/telegram_collection.md
git commit -m "Document Telegram user collection workflow"
```

## Task 10: Verification

**Files:**
- No code edits.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
pytest tests/test_telegram_telethon_collector.py tests/test_run_agent_cli.py tests/test_scheduler_runtime.py -q
```

Expected: PASS.

- [ ] **Step 2: Run the live one-shot Telegram smoke command**

Run:

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once --username-limit 2 --history-limit 10 --search-limit 2
```

Expected: command exits `0` and prints JSON containing `status: completed`, `tracked_chat_count`, `persisted_count`, and `targets`.

- [ ] **Step 3: Run the CLI Telegram command**

Run:

```powershell
python scripts/run_agent_cli.py --collect-telegram --telegram-once --telegram-username-limit 1 --telegram-history-limit 5 --telegram-search-limit 1
```

Expected: command exits `0` and prints the collector JSON summary.

- [ ] **Step 4: Run scheduler bootstrap/status check**

Run:

```powershell
python scripts/run_collection_scheduler.py --bootstrap-only --telegram-username-limit 1 --telegram-history-limit 5 --telegram-search-limit 1
```

Expected: command exits `0`; output includes `fast_telegram_collect`.

- [ ] **Step 5: Inspect git status**

Run:

```powershell
git status --short
```

Expected: only intentional uncommitted runtime artifacts remain, such as `data/` DB/session files, if live commands created them. Source/config/test/docs changes should be committed.

## Self-Review

- Spec coverage: Stage 1 is covered by Task 1 and Task 10 live smoke run. Stage 2 is covered by Tasks 2 through 6. Stage 3 is covered by Tasks 7 and 8. Docs and verification are covered by Tasks 9 and 10.
- Red-flag scan: The plan contains no deferred or unspecified implementation instructions. Every code-changing step includes concrete snippets or exact replacement guidance.
- Type consistency: `CollectorCliOverrides`, `CollectionOptions`, `TargetRunStats`, `build_collection_options`, `update_last_message_id`, `build_run_summary`, and Telegram CLI argument names are consistent across tasks.
