"""Safety guardrails for the controlled exploration sandbox.

The guard only authorizes low-impact, review-queue oriented sandbox behavior.
It deliberately rejects production writes, online enforcement actions, PII
exfiltration, and unauthorized source expansion before an exploration agent can
act on hallucinated recommendations.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping


class SafetyPolicyViolation(RuntimeError):
    """Raised when an agent action crosses a hard safety boundary."""

    def __init__(self, message: str, *, rule: str | None = None, action: Any = None) -> None:
        super().__init__(message)
        self.rule = rule
        self.action = action


class PolicyGuard:
    """Hard red-line checks for BlackAgent controlled exploration.

    The guard is intentionally deterministic.  It accepts structured action
    dictionaries as the primary input, but also supports strings and small
    dataclass-like objects so tests and future agent code can share the same
    checker.
    """

    _WRITE_WORDS = (
        "write",
        "store",
        "persist",
        "insert",
        "update",
        "save",
        "add",
        "写入",
        "落库",
        "入库",
        "更新",
    )
    _FORMAL_TARGETS = (
        "formal",
        "official",
        "production",
        "prod",
        "entity_repo",
        "strategy_repo",
        "label_schema",
        "wordlist",
        "实体库",
        "正式库",
        "正式实体库",
        "正式词库",
        "正式标签",
        "正式策略",
        "策略库",
        "生产",
    )
    _ONLINE_ENFORCEMENT = (
        "ban",
        "block",
        "blacklist",
        "intercept",
        "enforce",
        "封禁",
        "拉黑",
        "拦截",
        "处置账号",
        "下发",
        "自动处置",
        "自动拉黑",
        "线上封禁",
        "线上拦截",
    )
    _OUTBOUND_WORDS = (
        "external",
        "network",
        "http",
        "webhook",
        "api",
        "send",
        "export",
        "upload",
        "公网",
        "外部",
        "对外",
        "发送",
        "导出",
        "上传",
    )
    _PII_WORDS = (
        "pii",
        "phone",
        "mobile",
        "account",
        "telegram",
        "wechat",
        "qq",
        "手机号",
        "电话",
        "银行卡",
        "身份证",
        "账号",
        "账户",
        "联系方式",
        "群组id",
        "用户id",
    )
    _LOCAL_REVIEW_TARGETS = (
        "review_repo",
        "review_queue",
        "human_review",
        "local_sandbox",
    )
    _UNAUTHORIZED_COLLECTION = (
        "unauthorized",
        "bypass",
        "proxy",
        "captcha",
        "login_state",
        "越权",
        "未授权",
        "绕过",
        "代理ip",
        "验证码",
        "登录态",
        "扩展采集源",
        "新增采集源",
        "外网采集",
        "全网采集",
        "反爬绕过",
        "分布式代理",
    )

    def check_action_safety(self, action: Any) -> bool:
        """Validate a proposed sandbox action.

        Returns True for allowed review-only actions and raises
        :class:`SafetyPolicyViolation` for hard red-line violations.
        """

        normalized = self._normalize(action)
        text = normalized["text"]
        action_type = normalized.get("type", "")
        target = normalized.get("target", "")

        if self._is_formal_write(action_type, target, text):
            self._raise(
                "Sandbox output must not be written to formal entity, strategy, label, or wordlist repositories.",
                "formal_write",
                action,
            )

        if self._contains_any(text, self._ONLINE_ENFORCEMENT):
            self._raise(
                "Sandbox actions must not trigger online ban, block, blacklist, or interception decisions.",
                "online_enforcement",
                action,
            )

        is_local_review_write = (
            self._contains_any(target, self._LOCAL_REVIEW_TARGETS)
            or self._contains_any(text, self._LOCAL_REVIEW_TARGETS)
            or normalized.get("destination", "") in self._LOCAL_REVIEW_TARGETS
        )
        if (
            not is_local_review_write
            and self._contains_any(text, self._OUTBOUND_WORDS)
            and self._contains_any(text, self._PII_WORDS)
        ):
            self._raise(
                "Account/contact PII must not be sent to external or unauthorized destinations.",
                "pii_exfiltration",
                action,
            )

        if self._contains_any(text, self._UNAUTHORIZED_COLLECTION):
            self._raise(
                "The exploration agent must not expand or bypass authorized collection boundaries.",
                "unauthorized_collection",
                action,
            )

        return True

    def assert_review_only(self, output: Any) -> bool:
        """Ensure sandbox output is explicitly gated for human review."""

        data = self._to_mapping(output)
        requires_review = data.get("requires_human_review")
        if requires_review is not True:
            self._raise(
                "Exploration outputs must set requires_human_review=true before leaving the sandbox.",
                "review_required",
                output,
            )
        destination = str(data.get("destination") or data.get("target_repo") or "").lower()
        if destination and destination not in {"review_repo", "review_queue", "human_review"}:
            self.check_action_safety({"type": "write", "target": destination, "payload": data})
        return True

    def _is_formal_write(self, action_type: str, target: str, text: str) -> bool:
        write_intent = self._contains_any(action_type, self._WRITE_WORDS) or self._contains_any(text, ("自动写入", "写入", "落库"))
        formal_target = self._contains_any(target, self._FORMAL_TARGETS) or self._contains_any(text, self._FORMAL_TARGETS)
        return write_intent and formal_target

    def _normalize(self, action: Any) -> dict[str, str]:
        data = self._to_mapping(action)
        if data:
            action_type = self._join_values(
                data.get("type"),
                data.get("action"),
                data.get("operation"),
                data.get("name"),
                data.get("tool"),
            )
            target = self._join_values(
                data.get("target"),
                data.get("destination"),
                data.get("target_repo"),
                data.get("repo"),
                data.get("source"),
            )
            try:
                text = json.dumps(data, ensure_ascii=False, sort_keys=True).lower()
            except TypeError:
                text = str(data).lower()
            destination = self._join_values(data.get("destination"), data.get("target_repo")).lower()
            return {"type": action_type.lower(), "target": target.lower(), "destination": destination, "text": text}
        text = str(action).lower()
        return {"type": text, "target": text, "destination": text, "text": text}

    def _to_mapping(self, value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            return dict(dumped) if isinstance(dumped, Mapping) else {}
        if hasattr(value, "dict"):
            dumped = value.dict()
            return dict(dumped) if isinstance(dumped, Mapping) else {}
        if hasattr(value, "__dict__") and not isinstance(value, type):
            return dict(value.__dict__)
        return {}

    def _join_values(self, *values: Any) -> str:
        return " ".join(str(value) for value in values if value is not None)

    def _contains_any(self, text: str, needles: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(needle.lower() in lowered for needle in needles)

    def _raise(self, message: str, rule: str, action: Any) -> None:
        raise SafetyPolicyViolation(message, rule=rule, action=action)
