"""PII masking utilities shared by safety-sensitive outputs."""

from __future__ import annotations

import re


class PIIMasker:
    """Mask common contact identifiers while preserving review utility."""

    _PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
    _TG_RE = re.compile(r"\b(TG|Telegram)\s*[:：@]?\s*([A-Za-z0-9_]{4,})", flags=re.IGNORECASE)

    def mask_text(self, text: str) -> str:
        masked = self._PHONE_RE.sub(lambda match: match.group(1)[:3] + "****" + match.group(1)[-4:], str(text or ""))
        return self._TG_RE.sub(lambda match: f"{match.group(1)}:***{match.group(2)[-2:]}", masked)


__all__ = ["PIIMasker"]
