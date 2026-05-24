"""Classifier package for BlackAgent deterministic backbone."""

from .nlp_rule_matcher import (
    ACCOUNT_TRADING,
    CLICK_FARMING,
    FRAUD_TRAFFIC,
    NORMAL_NOISE,
    TOOL_TRADING,
    UNKNOWN,
    RuleFastTrackClassifier,
)

__all__ = [
    "ACCOUNT_TRADING",
    "CLICK_FARMING",
    "FRAUD_TRAFFIC",
    "NORMAL_NOISE",
    "TOOL_TRADING",
    "UNKNOWN",
    "RuleFastTrackClassifier",
]

