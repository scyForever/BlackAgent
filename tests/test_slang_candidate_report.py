import json

from scripts import build_slang_candidate_report
from scripts.build_slang_candidate_report import build_report
from src.enhancement.lifecycle import DynamicSlangLifecycleManager


def test_slang_candidate_report_mines_repeated_unknown_pending_terms_without_known_taxonomy_terms():
    records = [
        {
            "trace_id": "slang-u1",
            "source_name": "tg-a",
            "content_text": "火苗联系我，暗号777，今晚继续上车。",
        },
        {
            "trace_id": "slang-u2",
            "source_name": "forum-b",
            "content_text": "火苗找 @demo，截图里也写了火苗。",
        },
        {
            "trace_id": "known-risk",
            "source_name": "tg-known",
            "content_text": "接码注册联系 TG:known。",
        },
    ]
    classifications = [
        {"source_trace_id": "slang-u1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "slang-u2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "known-risk", "risk_category": "账号交易", "secondary_label": "接码注册", "review_required": False},
    ]

    report = build_report(records, classifications, min_count=2, max_candidates=10)

    terms = {candidate["term"]: candidate for candidate in report["candidates"]}
    assert report["status"] == "completed"
    assert terms["火苗"]["lifecycle_stage"] == DynamicSlangLifecycleManager.NEW_CANDIDATE
    assert terms["火苗"]["review_status"] == "pending_human_confirmation"
    assert terms["火苗"]["count"] >= 3
    assert terms["火苗"]["source_trace_ids_sample"] == ["slang-u1", "slang-u2"]
    assert "contact_or_call_to_action" in terms["火苗"]["context_markers"]
    assert "接码" not in terms


def test_slang_candidate_report_cli_escapes_stdout_when_console_cannot_encode_context(tmp_path, monkeypatch):
    records_path = tmp_path / "records.jsonl"
    classifications_path = tmp_path / "classifications.jsonl"
    output_path = tmp_path / "report.json"
    records = [
        {"trace_id": "emoji-1", "content_text": "📌火苗联系我，今晚继续上车。"},
        {"trace_id": "emoji-2", "content_text": "📌火苗找 @demo，火苗继续。"},
    ]
    classifications = [
        {"source_trace_id": "emoji-1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "emoji-2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
    ]
    records_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")
    classifications_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in classifications), encoding="utf-8")

    class GbkOnlyStdout:
        def __init__(self):
            self.values = []

        def write(self, text):
            text.encode("gbk")
            self.values.append(text)

        def flush(self):
            return None

    stdout = GbkOnlyStdout()
    monkeypatch.setattr(build_slang_candidate_report.sys, "stdout", stdout)

    assert build_slang_candidate_report.main(
        [
            "--records",
            str(records_path),
            "--classifications",
            str(classifications_path),
            "--output",
            str(output_path),
            "--min-count",
            "2",
        ]
    ) == 0

    assert output_path.exists()
    assert "\\ud83d\\udccc" in "".join(stdout.values)


def test_slang_candidate_report_skips_pure_latin_web_boilerplate_candidates():
    records = [
        {
            "trace_id": "latin-noise-1",
            "content_text": "Read full guide at automationforum.com and contact TG:demo. 火苗联系我。",
        },
        {
            "trace_id": "latin-noise-2",
            "content_text": "Read automationforum update and channel guide. 火苗找 @demo。",
        },
    ]
    classifications = [
        {"source_trace_id": "latin-noise-1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "latin-noise-2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
    ]

    report = build_report(records, classifications, min_count=2, max_candidates=10)

    terms = {candidate["term"] for candidate in report["candidates"]}
    assert "火苗" in terms
    assert "and" not in terms
    assert "Read" not in terms
    assert "automationforum" not in terms


def test_slang_candidate_report_mines_repeated_latin_shorthand_in_pending_rows():
    records = [
        {
            "trace_id": "latin-slang-1",
            "content_text": "wsp低价接单，联系 @seller，今晚继续上车。",
        },
        {
            "trace_id": "latin-slang-2",
            "content_text": "wsp老板可私聊，价格可以谈，暗号777。",
        },
    ]
    classifications = [
        {"source_trace_id": "latin-slang-1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "latin-slang-2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
    ]

    report = build_report(records, classifications, min_count=2, max_candidates=10)

    terms = {candidate["term"]: candidate for candidate in report["candidates"]}
    assert "wsp" in terms
    assert terms["wsp"]["count"] == 2
    assert "contact_or_call_to_action" in terms["wsp"]["context_markers"]
    assert "transaction_or_task_context" in terms["wsp"]["context_markers"]


def test_slang_candidate_report_filters_common_english_function_words_from_latin_candidates():
    records = [
        {
            "trace_id": "latin-function-1",
            "content_text": "wsp低价接单 contact for admin image useful in to of me is，联系 @seller。",
        },
        {
            "trace_id": "latin-function-2",
            "content_text": "wsp老板可私聊 contact for admin image useful in to of me is，价格可谈。",
        },
    ]
    classifications = [
        {"source_trace_id": "latin-function-1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "latin-function-2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
    ]

    report = build_report(records, classifications, min_count=2, max_candidates=20)

    terms = {candidate["term"] for candidate in report["candidates"]}
    assert "wsp" in terms
    assert {"contact", "for", "admin", "image", "useful", "in", "to", "of", "me", "is"}.isdisjoint(terms)


def test_slang_candidate_report_skips_known_markers_and_generic_cjk_terms():
    records = [
        {
            "trace_id": "generic-1",
            "content_text": "火苗联系我，价格可以详聊，自动使用教程联系客服。",
        },
        {
            "trace_id": "generic-2",
            "content_text": "火苗找 @demo，价格可以优惠，自动使用说明联系客服。",
        },
    ]
    classifications = [
        {"source_trace_id": "generic-1", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
        {"source_trace_id": "generic-2", "risk_category": "unknown", "secondary_label": "待研判", "review_required": True},
    ]

    report = build_report(records, classifications, min_count=2, max_candidates=20)

    terms = {candidate["term"] for candidate in report["candidates"]}
    assert "火苗" in terms
    assert "价格" not in terms
    assert "可以" not in terms
    assert "使用" not in terms
    assert "自动" not in terms
    assert "联系客" not in terms
    assert "系客服" not in terms
