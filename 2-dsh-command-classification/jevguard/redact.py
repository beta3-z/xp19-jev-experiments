from __future__ import annotations

import re

_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("bearer", re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (
        "secret-assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)"
            r"(\s*[:=]\s*)([\"']?)[^\s\"';|&]{4,}"
        ),
        r"\1\2\3***",
    ),
    ("url-credentials", re.compile(r"(?i)\b(https?://[^/\s:@]+:)[^@\s]+@"), r"\1***@"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "***"),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "***"),
    ("typesafe-key", re.compile(r"\b(?:ts|apik)_[A-Za-z0-9_-]{16,}\b"), "***"),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
)


def redact(text: str) -> tuple[str, list[str]]:
    hits: list[str] = []
    result = text
    for name, pattern, replacement in _RULES:
        result, count = pattern.subn(replacement, result)
        if count:
            hits.append(name)
    return result, hits
