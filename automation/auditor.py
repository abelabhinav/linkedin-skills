"""Audit rules for LinkedIn posts and comments.

Enforces 2026 voice rules, algorithm heuristics, and AI tell removal before enqueuing actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


BANNED_WORDS = [
    "leverage", "utilize", "facilitate", "streamline", "robust", "seamless",
    "delve", "navigate", "unlock", "harness", "foster", "cultivate",
    "fundamentally", "essentially", "ultimately", "crucially", "notably",
    "landscape", "ecosystem", "paradigm", "realm", "tapestry", "journey",
    "game-changer", "game changer", "deep dive", "deep-dive",
    "in today's fast-paced world", "it's not just x, it's y",
    "super excited to announce", "humbled and honored",
]


@dataclass
class AuditResult:
    passed: bool
    issues: list[str]
    warnings: list[str]
    char_count: int
    word_count: int


def audit_draft(content: str, kind: str = "POST") -> AuditResult:
    """Audit content draft against voice rules and algorithmic guidelines."""
    text = content.strip()
    issues: list[str] = []
    warnings: list[str] = []
    char_count = len(text)
    words = text.split()
    word_count = len(words)

    if not text:
        return AuditResult(
            passed=False,
            issues=["Draft is empty"],
            warnings=[],
            char_count=0,
            word_count=0,
        )

    # 1. Length checks
    kind_upper = kind.upper()
    if kind_upper in ("POST", "RESHARE"):
        if char_count > 3000:
            issues.append(f"Post length ({char_count} chars) exceeds LinkedIn 3,000 char maximum.")
        elif char_count < 100:
            warnings.append(f"Post length ({char_count} chars) is very short; recommended 500-1500 chars.")
    elif kind_upper in ("COMMENT", "REPLY"):
        if char_count > 1250:
            issues.append(f"Comment length ({char_count} chars) exceeds recommended 1,250 chars.")
        elif char_count < 25:
            warnings.append(f"Comment length ({char_count} chars) is very short.")

    # 2. Banned vocabulary checks
    lower_text = text.lower()
    for word in BANNED_WORDS:
        # Use regex to match whole words or phrases
        pattern = r"\b" + re.escape(word) + r"\b"
        if re.search(pattern, lower_text):
            issues.append(f"Contains AI buzzword/phrase: '{word}'. Replace with clear, grounded language.")

    # 3. Em-dash density checks
    em_dashes = text.count("—") + text.count("--")
    if word_count > 0:
        dash_rate = (em_dashes / word_count) * 100
        if dash_rate > 2.0 and em_dashes > 2:
            warnings.append(f"High em-dash density ({em_dashes} dashes in {word_count} words). Cap at ~1 per 100 words.")

    # 4. Opening line rule (never open with a question in posts)
    if kind_upper == "POST":
        first_line = text.split("\n", 1)[0].strip()
        if first_line.endswith("?") or first_line.startswith(("Have you ever", "What if", "Why do", "Did you know")):
            warnings.append("Post opens with a question (-34% median likes in 2026). Move question to the closing line.")

    passed = len(issues) == 0
    return AuditResult(
        passed=passed,
        issues=issues,
        warnings=warnings,
        char_count=char_count,
        word_count=word_count,
    )
