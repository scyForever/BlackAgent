"""Hard collection boundary for configured intelligence sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .policy_guard import SafetyPolicyViolation


AUTHORIZED_LEGAL_BASES = {
    "PUBLIC_COMPLIANT_DATA",
    "AUTHORIZED_PARTNER",
    "INTERNAL_AUTHORIZED_SOURCE",
    "THIRD_PARTY_AUTHORIZED_FEED",
}

FORBIDDEN_TRUE_FLAGS = {
    "allow_login_bypass",
    "allow_captcha_bypass",
    "allow_interaction",
    "allow_auto_join",
    "allow_private_group_access",
    "allow_file_download",
}

_CREDENTIAL_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|auth|authorization|cookie|session)[=:][^&\s]+"
)
_BASIC_AUTH_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@", re.IGNORECASE)


@dataclass(frozen=True)
class SourcePolicyDecision:
    allowed: bool
    reason: str = "allowed"
    rule: str | None = None


class SourcePolicyGuard:
    """Validate sources at orchestration/workflow boundaries before collection."""

    def validate_for_collection(self, source: Mapping[str, Any]) -> None:
        decision = self.check(source)
        if not decision.allowed:
            raise SafetyPolicyViolation(
                f"Source collection denied: {decision.reason}",
                rule=decision.rule or decision.reason,
                action=dict(source),
            )

    def check(self, source: Mapping[str, Any]) -> SourcePolicyDecision:
        legal_basis = str(source.get("legal_basis") or "").strip()
        if legal_basis not in AUTHORIZED_LEGAL_BASES:
            return SourcePolicyDecision(False, "missing_authorized_legal_basis", "missing_authorized_legal_basis")
        for field in sorted(FORBIDDEN_TRUE_FLAGS):
            if source.get(field) is True:
                return SourcePolicyDecision(False, f"{field}_forbidden", field)
        urls = [
            str(source.get("source_url") or ""),
            str(source.get("query_url_template") or ""),
            str(source.get("url") or ""),
        ]
        if any(self._contains_credentials(url) for url in urls if url):
            return SourcePolicyDecision(False, "credentials_in_source_url_forbidden", "credentials_in_source_url")
        return SourcePolicyDecision(True)

    def allowed(self, source: Mapping[str, Any]) -> bool:
        return self.check(source).allowed

    def _contains_credentials(self, url: str) -> bool:
        text = str(url or "").strip()
        return bool(_BASIC_AUTH_RE.search(text) or _CREDENTIAL_RE.search(text))


__all__ = [
    "AUTHORIZED_LEGAL_BASES",
    "FORBIDDEN_TRUE_FLAGS",
    "SourcePolicyDecision",
    "SourcePolicyGuard",
]
