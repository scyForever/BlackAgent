"""PII masking utilities shared by safety-sensitive outputs."""

from __future__ import annotations

import re


class PIIMasker:
    """Mask common contact identifiers while preserving review utility.

    Covers phone numbers, Telegram/WeChat/QQ handles, email, bank-card and
    mainland (15/18 digit) ID-card numbers. Masking keeps a short suffix so
    reviewers can still correlate values without exposing the full identifier.
    Patterns are applied longest/most-specific first so an ID/bank-card number is
    never partially re-masked by the 11-digit phone rule.
    """

    _EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    _ID18_RE = re.compile(r"(?<![0-9A-Za-z])(\d{6})\d{8}(\d{3}[0-9Xx])(?![0-9A-Za-z])")
    _ID15_RE = re.compile(r"(?<!\d)(\d{6})\d{6}(\d{3})(?!\d)")
    _BANKCARD_RE = re.compile(r"(?<!\d)(\d{6})\d{6,9}(\d{4})(?!\d)")
    _PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
    _QQ_RE = re.compile(r"(?i)(QQ|企鹅|🐧)\s*[:：]?\s*([1-9]\d{4,11})")
    _WECHAT_RE = re.compile(r"(?i)(微信|薇信|围信|威信|V信|VX|WX|wechat)\s*[:：]?\s*([A-Za-z][-_A-Za-z0-9]{5,19})")
    _TG_RE = re.compile(r"\b(TG|Telegram)\s*[:：@]?\s*([A-Za-z0-9_]{4,})", flags=re.IGNORECASE)

    def mask_text(self, text: str) -> str:
        masked = str(text or "")
        masked = self._EMAIL_RE.sub(lambda match: f"{match.group(1)}***{match.group(2)}", masked)
        masked = self._ID18_RE.sub(lambda match: f"{match.group(1)}********{match.group(2)}", masked)
        masked = self._ID15_RE.sub(lambda match: f"{match.group(1)}******{match.group(2)}", masked)
        masked = self._BANKCARD_RE.sub(lambda match: f"{match.group(1)}******{match.group(2)}", masked)
        masked = self._PHONE_RE.sub(lambda match: match.group(1)[:3] + "****" + match.group(1)[-4:], masked)
        masked = self._QQ_RE.sub(lambda match: f"{match.group(1)}:***{match.group(2)[-2:]}", masked)
        masked = self._WECHAT_RE.sub(lambda match: f"{match.group(1)}:***{match.group(2)[-2:]}", masked)
        masked = self._TG_RE.sub(lambda match: f"{match.group(1)}:***{match.group(2)[-2:]}", masked)
        return masked


__all__ = ["PIIMasker"]
