"""Build a conservative authorized real-source rerun evidence pack.

The pack aggregates small rerun rows from authorized public/partner/internal
sources. It separates real external evidence from loopback demonstrations so a
local smoke test cannot be mistaken for external Telegram or market coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.source_metadata import (  # noqa: E402
    normalize_source_access_type,
    source_class_for_record,
    source_quota_groups_for_record,
)


DEFAULT_OUTPUT = "data/authorized_source_rerun_pack.jsonl"
DEFAULT_REPORT = "data/authorized_source_rerun_pack_report.json"
REQUIRED_COVERAGE_GROUPS = (
    "real_telegram",
    "public_account_or_article",
    "secondhand_market",
    "crowdsourcing_platform",
    "im_or_group",
    "social_or_forum",
    "vertical_or_technical",
    "other_authorized",
    "public_account_article",
)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
FAILED_STATUS_FIELDS = ("status", "collection_status", "runtime_status", "fetch_status", "source_status")
FAILED_STATUS_VALUES = {
    "failed",
    "failure",
    "error",
    "blocked",
    "rejected",
    "unauthorized",
    "login_required",
    "partial_failure",
    "partial_failed",
    "incomplete",
    "timeout",
    "captcha",
    "403",
    "429",
}
FAILED_STATUS_MARKERS = (
    "failed",
    "failure",
    "error",
    "blocked",
    "rejected",
    "unauthorized",
    "login_required",
    "timeout",
    "captcha",
    "403",
    "429",
)
AUTHORIZED_SOURCE_ACCESS_TYPES = {"public_compliant", "authorized_partner", "internal_authorized"}
NON_CLAIMABLE_SOURCE_ACCESS_TYPES = {"loopback_demo", "manual_upload", "manual"}
AUTHORIZED_LEGAL_BASES = {
    "PUBLIC_COMPLIANT_DATA",
    "AUTHORIZED_PARTNER",
    "THIRD_PARTY_AUTHORIZED_FEED",
    "INTERNAL_AUTHORIZED_SOURCE",
}
CREDENTIAL_FIELD_MARKERS = (
    "api_id",
    "api_hash",
    "authorization",
    "bearer_token",
    "cookie",
    "credential",
    "credentials",
    "header",
    "headers",
    "password",
    "secret",
    "session",
    "session_cookie",
    "token",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an authorized real-source rerun evidence pack.")
    parser.add_argument("--input-jsonl", action="append", default=[], help="Input raw JSONL. Repeatable.")
    parser.add_argument("--source-report", action="append", default=[], help="Source/smoke report JSON. Repeatable.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--report", default=DEFAULT_REPORT)
    parser.add_argument("--collection-started-at")
    parser.add_argument("--collection-finished-at")
    return parser.parse_args(argv)


def build_pack(
    raw_rows: Iterable[Mapping[str, Any]],
    source_reports: Iterable[Mapping[str, Any]] | None = None,
    collection_started_at: str | None = None,
    collection_finished_at: str | None = None,
) -> dict[str, Any]:
    """Return normalized rows plus a conservative rerun coverage report."""

    report_rows = [dict(report) for report in (source_reports or []) if isinstance(report, Mapping)]
    rows = [
        normalize_row(
            row,
            index=index,
            collection_started_at=collection_started_at,
            collection_finished_at=collection_finished_at,
        )
        for index, row in enumerate(raw_rows)
        if isinstance(row, Mapping)
    ]

    real_external_rows = [row for row in rows if row["is_real_external_source"]]
    loopback_rows = [row for row in rows if row["is_loopback_source"]]
    all_group_counts = _group_counts(rows)
    covered_group_counts = _group_counts(real_external_rows)
    loopback_group_counts = _group_counts(loopback_rows)
    failure_summary = _failure_summary(rows, report_rows)
    credential_boundary = _credential_boundary(rows, report_rows)
    collection_window = _collection_window(rows, collection_started_at, collection_finished_at)

    if not rows:
        status = "empty"
    elif real_external_rows:
        status = "completed"
    else:
        status = "insufficient_real_authorized_sources"

    report = {
        "status": status,
        "pack_version": "authorized_source_rerun_pack_v1",
        "row_count": len(rows),
        "real_external_row_count": len(real_external_rows),
        "loopback_row_count": len(loopback_rows),
        "credential_boundary": credential_boundary,
        "source_coverage": {
            "covered_groups": _with_required_groups(covered_group_counts),
            "all_group_counts": _with_required_groups(all_group_counts),
            "loopback_group_counts": _with_required_groups(loopback_group_counts),
            "source_name_counts": dict(sorted(Counter(row["source_name"] for row in rows).items())),
            "real_external_source_names": sorted({row["source_name"] for row in real_external_rows if row["source_name"]}),
            "loopback_source_names": sorted({row["source_name"] for row in loopback_rows if row["source_name"]}),
        },
        "snapshot_coverage": _snapshot_coverage(rows),
        "failure_summary": failure_summary,
        "collection_window": collection_window,
        "claim_boundary": _claim_boundary(status),
    }
    return {"rows": rows, "report": report}


def normalize_row(
    row: Mapping[str, Any],
    *,
    index: int,
    collection_started_at: str | None = None,
    collection_finished_at: str | None = None,
) -> dict[str, Any]:
    data = dict(row)
    source_name = _string(data.get("source_name") or data.get("name") or data.get("source") or "unknown_source")
    source_type = _string(data.get("source_type") or data.get("type") or "")
    platform = _string(data.get("platform") or "")
    source_url = _string(data.get("source_url") or data.get("url") or data.get("permalink") or "")
    legal_basis = _string(data.get("legal_basis") or "")
    raw_source_access_type = _string(data.get("source_access_type") or "")
    source_access_type = normalize_source_access_type(
        data.get("source_access_type"),
        legal_basis=legal_basis,
        source_name=source_name,
        source_url=source_url,
    )
    crawl_time = _string(data.get("crawl_time") or data.get("publish_time") or data.get("created_at") or "")
    raw_snippet = _raw_snippet(data)
    content_hash = _string(data.get("content_hash") or data.get("hash_id") or "")
    if not content_hash:
        content_hash = hashlib.sha256(raw_snippet.encode("utf-8")).hexdigest() if raw_snippet else ""
    trace_id = _string(
        data.get("trace_id")
        or data.get("source_trace_id")
        or data.get("id")
        or data.get("hash_id")
        or content_hash[:16]
        or f"authorized-source-rerun-{index + 1}"
    )
    capture_snapshot_uri = _string(
        data.get("capture_snapshot_uri") or data.get("raw_snapshot_uri") or data.get("snapshot_uri") or ""
    )
    raw_payload_uri = _string(data.get("raw_payload_uri") or data.get("payload_uri") or data.get("raw_uri") or "")
    failure_reason = _failure_reason_for_row(data)
    normalized = {
        "trace_id": trace_id,
        "source_name": source_name,
        "source_type": source_type,
        "platform": platform,
        "source_url": source_url,
        "raw_source_access_type": raw_source_access_type,
        "source_access_type": source_access_type,
        "legal_basis": legal_basis,
        "crawl_time": crawl_time,
        "collection_started_at": _string(data.get("collection_started_at") or collection_started_at or ""),
        "collection_finished_at": _string(data.get("collection_finished_at") or collection_finished_at or ""),
        "capture_snapshot_uri": capture_snapshot_uri,
        "raw_payload_uri": raw_payload_uri,
        "failure_reason": failure_reason,
        "content_hash": content_hash,
        "raw_snippet": raw_snippet,
    }
    is_loopback = _is_loopback_row({**data, **normalized})
    is_external = _has_external_source_url(source_url)
    is_authorized = _has_authorization_metadata({**data, **normalized})
    is_evidence_complete = _has_claimable_evidence(normalized)
    is_real_external_candidate = is_external and not is_loopback and is_authorized
    is_real_external = is_real_external_candidate and is_evidence_complete
    normalized["is_loopback_source"] = is_loopback
    normalized["is_authorized_source"] = is_authorized
    normalized["is_real_external_candidate"] = is_real_external_candidate
    normalized["is_claimable_evidence"] = is_evidence_complete
    normalized["is_real_external_source"] = is_real_external
    normalized["source_groups"] = _source_groups({**data, **normalized}, include_real_telegram=is_real_external)
    return normalized


def load_input_rows(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        target = _project_path(path)
        if not target.exists():
            continue
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, Mapping):
                    rows.append(dict(payload))
    return rows


def load_source_reports(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in paths:
        target = _project_path(path)
        if not target.exists():
            continue
        payload = json.loads(target.read_text(encoding="utf-8"))
        reports.extend(_flatten_source_report_payload(payload))
    return reports


def write_jsonl(rows: Iterable[Mapping[str, Any]], output_path: str | Path) -> Path:
    target = _project_path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file_obj:
        for row in rows:
            file_obj.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    return target


def write_report(report: Mapping[str, Any], report_path: str | Path, *, output_path: Path) -> Path:
    target = _project_path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**dict(report), "output": str(output_path)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pack = build_pack(
        load_input_rows(args.input_jsonl),
        source_reports=load_source_reports(args.source_report),
        collection_started_at=args.collection_started_at,
        collection_finished_at=args.collection_finished_at,
    )
    output = write_jsonl(pack["rows"], args.output)
    write_report(pack["report"], args.report, output_path=output)
    print(json.dumps(pack["report"], ensure_ascii=False, indent=2))
    return 0 if pack["report"]["status"] == "completed" else 1


def _source_groups(row: Mapping[str, Any], *, include_real_telegram: bool) -> list[str]:
    groups = [group for group in source_quota_groups_for_record(row) if group != "real_telegram"]
    source_class = source_class_for_record(row)
    if source_class in {"im_or_group", "social_or_forum", "vertical_or_technical", "other_authorized"}:
        groups.append(source_class)
    if _is_real_telegram_row(row) and include_real_telegram:
        groups.append("real_telegram")
    return list(dict.fromkeys(group for group in groups if group))


def _has_authorization_metadata(row: Mapping[str, Any]) -> bool:
    raw_source_access_type = _string(row.get("raw_source_access_type") or "").lower()
    source_access_type = _string(row.get("source_access_type") or "").lower()
    legal_basis = _string(row.get("legal_basis") or "").upper()
    if raw_source_access_type:
        return raw_source_access_type in AUTHORIZED_SOURCE_ACCESS_TYPES
    if source_access_type in AUTHORIZED_SOURCE_ACCESS_TYPES:
        return True
    if source_access_type in NON_CLAIMABLE_SOURCE_ACCESS_TYPES:
        return False
    return bool(legal_basis and legal_basis in AUTHORIZED_LEGAL_BASES)


def _has_claimable_evidence(row: Mapping[str, Any]) -> bool:
    return (
        not _string(row.get("failure_reason") or "")
        and bool(row.get("capture_snapshot_uri"))
        and bool(row.get("raw_payload_uri"))
        and bool(_string(row.get("raw_snippet") or "") or _string(row.get("content_hash") or ""))
    )


def _is_real_telegram_row(row: Mapping[str, Any]) -> bool:
    source_url = _string(row.get("source_url") or row.get("url") or "")
    host = (urlparse(source_url).hostname or "").lower()
    source_name = _string(row.get("source_name") or row.get("name") or "").lower()
    source_type = _string(row.get("source_type") or row.get("type") or "").lower()
    platform = _string(row.get("platform") or "").lower()
    text = " ".join([source_name, source_type, platform, source_url.lower()])
    if platform in {"telegram", "tg"}:
        return True
    if source_type in {"telegram", "tg", "telegram_channel", "telegram_group"}:
        return True
    if host in {"t.me", "telegram.me", "telegram.org"}:
        return True
    return any(marker in text for marker in ("telegram", "t.me/", "telegram.me/", "tg_")) or source_name.startswith("tg")


def _is_loopback_row(row: Mapping[str, Any]) -> bool:
    source_access_type = _string(row.get("source_access_type") or "").lower()
    source_name = _string(row.get("source_name") or row.get("name") or "").lower()
    source_url = _string(row.get("source_url") or row.get("url") or "")
    capture_snapshot_uri = _string(row.get("capture_snapshot_uri") or "")
    raw_payload_uri = _string(row.get("raw_payload_uri") or "")
    host = (urlparse(source_url).hostname or "").lower()
    return (
        source_access_type == "loopback_demo"
        or "loopback" in source_name
        or host in LOCAL_HOSTS
        or capture_snapshot_uri.startswith("loopback://")
        or raw_payload_uri.startswith("loopback://")
    )


def _has_external_source_url(source_url: str) -> bool:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and _is_public_dns_or_ip_host(host)


def _is_public_dns_or_ip_host(host: str) -> bool:
    if not host:
        return False
    normalized_host = host.strip("[]").lower().rstrip(".")
    if normalized_host in LOCAL_HOSTS or normalized_host.endswith(".local") or "." not in normalized_host:
        return False
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _group_counts(rows: Iterable[Mapping[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(str(group) for group in row.get("source_groups") or [] if str(group))
    return counts


def _with_required_groups(counts: Counter[str] | Mapping[str, int]) -> dict[str, int]:
    merged = {group: int(counts.get(group, 0)) for group in REQUIRED_COVERAGE_GROUPS}
    for group, count in sorted(counts.items()):
        merged[str(group)] = int(count)
    return merged


def _snapshot_coverage(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    capture_count = sum(1 for row in rows if row.get("capture_snapshot_uri"))
    raw_payload_count = sum(1 for row in rows if row.get("raw_payload_uri"))
    missing_rows = [
        _string(row.get("trace_id") or "")
        for row in rows
        if not row.get("capture_snapshot_uri") or not row.get("raw_payload_uri")
    ]
    return {
        "capture_snapshot_uri_count": capture_count,
        "raw_snapshot_uri_count": capture_count,
        "raw_payload_uri_count": raw_payload_count,
        "missing_snapshot_count": len(missing_rows),
        "rows_missing_snapshots": missing_rows,
    }


def _failure_summary(rows: list[Mapping[str, Any]], source_reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_reason: Counter[str] = Counter()
    failed_source_names: set[str] = set()
    row_failure_keys: set[tuple[str, str]] = set()
    for row in rows:
        reason = _string(row.get("failure_reason") or "")
        if not reason:
            continue
        source_name = _string(row.get("source_name") or "unknown_source")
        by_reason[reason] += 1
        failed_source_names.add(source_name)
        row_failure_keys.add((source_name, reason))

    for report in source_reports:
        reason = _string(report.get("failure_reason") or report.get("failure") or "")
        if not reason:
            continue
        source_name = _source_report_name(report) or "unknown_source"
        if (source_name, reason) in row_failure_keys:
            continue
        by_reason[reason] += 1
        failed_source_names.add(source_name)
    return {
        "by_reason": dict(sorted(by_reason.items())),
        "failed_source_names": sorted(failed_source_names),
        "failed_source_count": len(failed_source_names),
    }


def _failure_reason_for_row(row: Mapping[str, Any]) -> str:
    explicit = _string(row.get("failure_reason") or "")
    if explicit:
        return explicit
    for field in FAILED_STATUS_FIELDS:
        value = _string(row.get(field) or "").lower()
        if value in FAILED_STATUS_VALUES or any(marker in value for marker in FAILED_STATUS_MARKERS):
            return value
    return ""


def _credential_boundary(rows: list[Mapping[str, Any]], source_reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    real_external_source_names = {row["source_name"] for row in rows if row.get("is_real_external_source")}
    loopback_only = bool(rows) and not real_external_source_names
    credentialed_source_names: set[str] = set()
    credential_fields_present: set[str] = set()

    for report in source_reports:
        source_name = _source_report_name(report)
        fields = _credential_fields_from_report(report)
        if fields:
            credential_fields_present.update(fields)
            if source_name:
                credentialed_source_names.add(source_name)
        if bool(report.get("credentialed")) and source_name:
            credentialed_source_names.add(source_name)

    for row in rows:
        if _string(row.get("source_access_type")).lower() in {"authorized_partner", "internal_authorized"}:
            credentialed_source_names.add(_string(row.get("source_name") or "unknown_source"))

    fields = sorted(credential_fields_present)
    return {
        "has_real_external_source": bool(real_external_source_names),
        "loopback_only": loopback_only,
        "credentialed_source_names": sorted(name for name in credentialed_source_names if name),
        "credential_fields_present": fields,
        "redacted_credential_fields": fields,
        "claim_boundary": (
            "Credential values are never stored in this artifact; only field names are listed. "
            "Rows marked loopback or localhost are excluded from real external source claims."
        ),
    }


def _credential_fields_from_report(report: Mapping[str, Any]) -> set[str]:
    fields: set[str] = set()
    explicit = report.get("credential_fields_present") or report.get("redacted_credential_fields")
    if isinstance(explicit, str):
        fields.update(item.strip() for item in explicit.split(",") if item.strip())
    elif explicit:
        try:
            fields.update(_string(item) for item in explicit if _string(item))
        except TypeError:
            fields.add(_string(explicit))

    metadata_keys = {"credential_fields_present", "redacted_credential_fields", "credentialed"}
    for key in report:
        if _string(key).lower() in metadata_keys:
            continue
        key_text = _string(key).lower()
        if any(marker in key_text for marker in CREDENTIAL_FIELD_MARKERS):
            fields.add(_string(key))
    if report.get("authorized_request_headers"):
        try:
            fields.update(_string(item) for item in report.get("authorized_request_headers") if _string(item))
        except TypeError:
            fields.add("authorized_request_headers")
    return {field for field in fields if field}


def _source_report_name(report: Mapping[str, Any]) -> str:
    for key in ("source_name", "name", "source"):
        value = report.get(key)
        if isinstance(value, Mapping):
            nested = value.get("source_name") or value.get("name")
            if isinstance(nested, (str, int, float, bool)):
                return _string(nested)
            continue
        if isinstance(value, (str, int, float, bool)):
            return _string(value)
    return ""


def _collection_window(
    rows: list[Mapping[str, Any]],
    collection_started_at: str | None,
    collection_finished_at: str | None,
) -> dict[str, Any]:
    crawl_times = sorted(_string(row.get("crawl_time") or "") for row in rows if row.get("crawl_time"))
    row_started = sorted(_string(row.get("collection_started_at") or "") for row in rows if row.get("collection_started_at"))
    row_finished = sorted(
        _string(row.get("collection_finished_at") or "") for row in rows if row.get("collection_finished_at")
    )
    return {
        "collection_started_at": _string(collection_started_at or (row_started[0] if row_started else "")),
        "collection_finished_at": _string(collection_finished_at or (row_finished[-1] if row_finished else "")),
        "crawl_time_min": crawl_times[0] if crawl_times else "",
        "crawl_time_max": crawl_times[-1] if crawl_times else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _claim_boundary(status: str) -> str:
    if status == "completed":
        return (
            "Authorized rerun pack contains at least one non-loopback external source row; "
            "it does not claim loopback-only demos as real external platform coverage."
        )
    if status == "empty":
        return "No authorized rerun rows were provided, so no real external source coverage is claimed."
    return (
        "Rows were provided, but none qualify as non-loopback external source evidence; "
        "loopback and localhost demos are retained only as local collection proof."
    )


def _flatten_source_report_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    reports = [dict(payload)]
    for key in ("sources", "source_reports", "per_source_reports"):
        nested = payload.get(key)
        if isinstance(nested, list):
            reports.extend(dict(item) for item in nested if isinstance(item, Mapping))
    return reports


def _raw_snippet(row: Mapping[str, Any]) -> str:
    for key in ("raw_snippet", "content_text", "raw_text", "text", "full_text", "full_article_body"):
        value = _string(row.get(key) or "")
        if value:
            return value[:500]
    return ""


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
