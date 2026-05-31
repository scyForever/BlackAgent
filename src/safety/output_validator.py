"""Output validation helpers for LLM and agent results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class OutputValidator:
    """Whitelist required keys and reject obvious unsafe content markers."""

    DANGEROUS_MARKERS = ("绕过风控", "诈骗教程", "盗号教程", "bypass captcha", "credential theft")

    def require_keys(self, payload: Mapping[str, Any], required: set[str]) -> bool:
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"missing required output keys: {', '.join(sorted(missing))}")
        return True

    def reject_dangerous_text(self, text: str) -> bool:
        lowered = str(text or "").lower()
        for marker in self.DANGEROUS_MARKERS:
            if marker.lower() in lowered:
                raise ValueError("output contains unsafe procedural content")
        return True


__all__ = ["OutputValidator"]
