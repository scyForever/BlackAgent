# Telegram User-State Collection Design

## Goal

Run Telegram user-state collection in three ordered stages:

1. Make the existing Telethon collector runnable with configured credentials, proxy, and seed targets.
2. Harden the collector with clearer reporting, failure handling, resumability, and focused tests.
3. Wire Telegram user-state collection into BlackAgent's main CLI and scheduler collection paths.

The implementation must stay limited to authorized user-state collection and existing BlackAgent raw-record ingestion. It must not add scraping bypasses, credential exposure, or unrelated source refactors.

## Existing Context

The repository already contains:

- `scripts/telegram_telethon_collector.py`: Telethon collector that logs in as a configured Telegram user, resolves configured usernames and invite links, searches by keywords, backfills history, tails new messages, filters for relevance, and writes to SQLite `raw_records`.
- `config/telegram_watch.example.yaml`: Telethon credentials, proxy, seed keywords, target usernames, collection filters, and JSONL output path.
- `config/telegram_public_delivery_channels.yaml`: public Telegram channel usernames already used by the project for delivery and evaluation data.
- `docs/telegram_collection.md`: operator notes for running the current collector.
- `tests/test_telegram_telethon_collector.py`: initial unit coverage for proxy/session/client helpers.

The worktree already has uncommitted changes touching the Telegram collector/config and other files. New edits should preserve those changes and only modify files needed for this task.

## Stage 1: Run Existing Collection

Use `config/telegram_public_delivery_channels.yaml` as the initial seed source because it contains project-vetted Telegram usernames. Copy a conservative high-signal subset into `config/telegram_watch.example.yaml` under `telegram.watch.usernames`, keeping keyword discovery enabled as a supplement.

Initial seed targets should favor channels already associated with collection themes such as account trade, SMS/code receiving, traffic/fraud funneling, automation, and group-control tooling. The run should start with `--once` and a small backfill budget so login, proxy, target resolution, filtering, state persistence, JSONL output, and SQLite insertion can be verified before continuous tailing.

Expected operator command:

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once
```

Success criteria:

- Telethon starts with configured API ID/hash, phone, session, and proxy.
- At least one configured username resolves, or failures are reported clearly.
- Backfill completes without leaving the database connection open.
- The command prints a JSON summary with tracked chat count and persisted count.
- `data/blackagent_telegram.db`, session state, and optional `data/telegram_raw.jsonl` are updated when relevant messages pass filters.

## Stage 2: Collector Hardening

Improve the collector without changing its high-level behavior.

Planned changes:

- Add structured per-target run stats: resolved, joined, backfilled, saved, skipped as irrelevant, skipped as duplicate, failed, and error reason.
- Make target resolution resilient: a failed username, invalid invite, private channel, or search error should not abort the whole run unless it is a credential/login problem or a Telegram flood wait that must be honored.
- Keep state updates monotonic per chat and preserve resumability across `--once` and tail modes.
- Add CLI controls for safe smoke runs, such as limiting configured usernames or history count without editing YAML.
- Ensure FloodWait handling is explicit in both backfill and tail paths.
- Avoid printing secrets or full auth config.

Focused tests should cover pure helpers and async behavior with fakes, including target parsing/selection, summary construction, duplicate skipping, relevance skipping, and nonfatal target failures. Live Telegram runs remain manual/integration verification because they depend on credentials and network state.

## Stage 3: Main Flow Integration

Add an integration path that lets operators run Telegram user-state collection through existing BlackAgent entry points instead of only invoking the standalone script.

Preferred integration:

- Add a CLI command or flag in the existing runtime/CLI layer for Telegram user-state collection.
- Add scheduler support by registering a Telegram user-state collection job that delegates to the same collector entry function.
- Keep the standalone script as the thin command-line wrapper for direct operation.

Data flow:

1. Config is loaded from `config/telegram_watch.example.yaml` or an operator-provided path.
2. The collector resolves seed usernames, invite links, and keyword-discovered public entities.
3. Messages are normalized into existing raw-record payloads.
4. Relevance filtering uses existing `src.collector.relevance.decide_text_relevance`.
5. Raw records are saved through `storage.sql_backend.connect(...).save_raw(...)`.
6. The rest of the existing cleaning, classification, extraction, clue, and reporting pipeline consumes the same raw-record table.

## Error Handling

Credential/login errors should stop the run with a concise message. Per-target errors should be recorded in the summary and the collector should continue with other targets. Invalid invite links should be skipped and reported. Flood waits should be slept through when reasonable in tail mode; in one-shot mode they should be reported clearly so the operator can retry later.

The collector should distinguish:

- Auth/config failure.
- Proxy/network failure.
- Target resolution failure.
- Join failure.
- Backfill failure.
- Relevance-filter skip.
- Duplicate/state skip.
- Persistence failure.

## Safety And Compliance

The collector remains scoped to authorized Telegram user-state access. It should only collect from configured targets, invite links provided by the operator, and Telegram search results visible to the configured account. Secrets must come from environment/config interpolation and must not be logged. Raw data keeps existing legal basis fields and source metadata.

## Testing And Verification

Verification is split into local automated checks and live operator checks:

- Run focused unit tests for Telegram collector helpers.
- Run broader affected tests if shared scheduler/CLI code changes.
- Run a live one-shot collection command using configured credentials and proxy.
- Inspect the printed JSON summary and raw-record count.
- Confirm no secrets appear in stdout, logs, JSONL, or committed files.

## Out Of Scope

- Creating new Telegram accounts.
- Circumventing Telegram access controls.
- Bulk member scraping or private-message collection.
- Changing downstream classification/extraction behavior.
- Replacing Telethon with another Telegram library.
