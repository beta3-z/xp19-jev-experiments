from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "choice-first-v2"

RISKS = ("low", "medium", "high", "unknown")
ACTIONS = ("allow", "annotate", "ask", "block")
ACTION_BY_RISK = {"low": "allow", "medium": "annotate", "high": "ask", "unknown": "annotate"}
CHOICE_RISK = {"safe": "low", "caution": "medium", "danger": "high", "insufficient": "unknown"}

STRONG_DIMENSIONS = ("destroys", "escapes", "critical", "privilege", "exfiltrates")
SAFETY_OVERRIDES = ("critical", "privilege", "exfiltrates", "obfuscated")
SAFETY_OVERRIDE_ALONE = 0.9
SAFETY_OVERRIDE_PAIRS = (
    ("critical", 0.85, "destroys", 0.6),
    ("irreversible", 0.85, "destroys", 0.6),
    ("escapes", 0.9, "destroys", 0.85),
)
STRONG_SIGNAL = 0.7
MIN_CONFIDENCE = 0.35
NEAR_CERTAIN_DESTROYS = 0.9
TAG_THRESHOLD = 0.6

_SAME_COMMAND = r"[^\n;&|]*"
_DRIVE_ROOT = r"\s[\"']?[a-z]:\\?[\"']?(?=\s|$|[;&|])"

HARD_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("format-volume", re.compile(r"\b(?:format-volume|clear-disk)\b", re.I)),
    ("format-drive", re.compile(r"\bformat\s+[a-z]:", re.I)),
    (
        "diskpart-clean",
        re.compile(r"\bdiskpart\b[\s\S]*\bclean\b|\bclean\b[\s\S]*\bdiskpart\b", re.I),
    ),
    (
        "remove-drive-root",
        re.compile(
            rf"\b(?:remove-item|rm|ri|del|erase|rd|rmdir)\b{_SAME_COMMAND}{_DRIVE_ROOT}", re.I
        ),
    ),
    (
        "shadow-copy-delete",
        re.compile(r"\b(?:vssadmin\s+delete\s+shadows|wbadmin\s+delete)\b", re.I),
    ),
    ("boot-config", re.compile(r"\bbcdedit\b", re.I)),
    ("clear-event-log", re.compile(r"\bwevtutil\s+cl\b", re.I)),
    ("defender-tamper", re.compile(r"\b(?:set|add)-mppreference\b", re.I)),
    (
        "exfil-secret",
        re.compile(
            r"\b(?:curl|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^\n]*"
            r"(?:\.env(?!\.(?:example|sample|template|dist)\b)\b|id_rsa|\.ssh\b|\bcredentials\b)",
            re.I,
        ),
    ),
)

TAG_BY_DIMENSION = {
    "destroys": "destructive",
    "escapes": "outside-workspace",
    "remote": "remote",
    "critical": "critical-target",
    "privilege": "privileged",
    "exfiltrates": "exfiltration",
    "obfuscated": "obfuscated",
    "irreversible": "irreversible",
    "opaque": "opaque",
    "reviewer_injection": "injection",
}

TAG_REASON_EN = {
    "destructive": "deletes or overwrites existing data",
    "outside-workspace": "touches paths outside the workspace",
    "remote": "changes state off this machine",
    "critical-target": "targets system files, credentials, or shared history",
    "privileged": "elevates privileges or changes security controls",
    "exfiltration": "sends local data to an external destination",
    "obfuscated": "hides its real effect",
    "irreversible": "cannot be undone without a backup",
    "opaque": "runs code whose contents are not visible",
    "injection": "tries to instruct the reviewer",
}

TAG_REASON_RU = {
    "destructive": "удаляет или перезаписывает существующие данные",
    "outside-workspace": "выходит за пределы workspace",
    "remote": "меняет состояние вне этой машины",
    "critical-target": "затрагивает систему, ключи или общую историю",
    "privileged": "повышает привилегии или меняет security-контроли",
    "exfiltration": "отправляет локальные данные наружу",
    "obfuscated": "прячет реальный эффект",
    "irreversible": "необратимо без бэкапа",
    "opaque": "запускает код, содержимое которого не видно",
    "injection": "пытается дать инструкции проверяющему",
}


@dataclass(frozen=True)
class Verdict:
    risk: str
    action: str
    tags: tuple[str, ...]
    reason: str
    scores: dict[str, float]
    confidence: float
    probabilities: dict[str, float]
    top_choice: str | None = None
    hard_rule: str | None = None

    @property
    def line(self) -> str:
        if self.hard_rule:
            return f"Jev: blocked by rule '{self.hard_rule}'"
        if self.risk == "unknown":
            return "Jev: could not classify this command"
        if not self.tags:
            return f"Jev: {self.risk} risk"
        return f"Jev: {self.risk} risk — {', '.join(self.tags)}"

    def human(self) -> str:
        if self.hard_rule:
            return f"жёсткое правило «{self.hard_rule}»: такие команды не выполняем автоматически"
        if self.risk == "unknown":
            return "Jev не смог оценить команду (непрозрачный скрипт, таймаут или низкая уверенность)"
        phrases = [TAG_REASON_RU[tag] for tag in self.tags if tag in TAG_REASON_RU]
        if not phrases:
            return "сильных риск-сигналов нет"
        return "; ".join(phrases)


def match_hard_rule(command: str) -> str | None:
    for name, pattern in HARD_RULES:
        if pattern.search(command):
            return name
    return None


def _safety_override(scores: dict[str, float]) -> bool:
    return any(scores[name] >= SAFETY_OVERRIDE_ALONE for name in SAFETY_OVERRIDES) or any(
        scores[first] >= first_min and scores[second] >= second_min
        for first, first_min, second, second_min in SAFETY_OVERRIDE_PAIRS
    )


def _risk(scores: dict[str, float], top_choice: str, confidence: float) -> str:
    if _safety_override(scores):
        return "high"
    risk = CHOICE_RISK[top_choice]
    if risk == "unknown" and any(scores[name] >= STRONG_SIGNAL for name in STRONG_DIMENSIONS):
        risk = "medium"
    if risk != "high" and confidence < MIN_CONFIDENCE:
        risk = "unknown"
    if risk == "unknown" and scores["destroys"] >= NEAR_CERTAIN_DESTROYS:
        risk = "medium"
    return risk


def compose(
    scores: dict[str, float],
    *,
    probabilities: dict[str, float],
    confidence: float,
    command: str | None = None,
) -> Verdict:
    rule = match_hard_rule(command) if command else None
    if rule:
        return Verdict(
            risk="high",
            action="block",
            tags=(f"hard-rule:{rule}",),
            reason=f"deterministic rule '{rule}' matched",
            scores=scores,
            confidence=confidence,
            probabilities=probabilities,
            hard_rule=rule,
        )

    top_choice = max(probabilities, key=probabilities.__getitem__)
    risk = _risk(scores, top_choice, confidence)
    tags = tuple(
        TAG_BY_DIMENSION[name]
        for name in scores
        if name in TAG_BY_DIMENSION and scores[name] >= TAG_THRESHOLD
    )
    reason = "; ".join(TAG_REASON_EN[tag] for tag in tags) or "no strong risk signals"
    return Verdict(
        risk=risk,
        action=ACTION_BY_RISK[risk],
        tags=tags,
        reason=reason,
        scores=scores,
        confidence=confidence,
        probabilities=probabilities,
        top_choice=top_choice,
    )


def label(verdict: Verdict) -> str:
    return "block" if verdict.action == "block" else verdict.risk


def policy_spec() -> dict[str, Any]:
    return {
        "policy_version": POLICY_VERSION,
        "risks": list(RISKS),
        "actions": list(ACTIONS),
        "action_by_risk": ACTION_BY_RISK,
        "choice_risk": CHOICE_RISK,
        "thresholds": {
            "strong_dimensions": list(STRONG_DIMENSIONS),
            "safety_overrides": list(SAFETY_OVERRIDES),
            "safety_override_alone": SAFETY_OVERRIDE_ALONE,
            "safety_override_pairs": [
                {"first": first, "first_min": first_min, "second": second, "second_min": second_min}
                for first, first_min, second, second_min in SAFETY_OVERRIDE_PAIRS
            ],
            "strong_signal": STRONG_SIGNAL,
            "min_confidence": MIN_CONFIDENCE,
            "near_certain_destroys": NEAR_CERTAIN_DESTROYS,
            "tag_threshold": TAG_THRESHOLD,
        },
        "hard_rules": [
            {"name": name, "pattern": pattern.pattern, "flags": "i" if pattern.flags & re.I else ""}
            for name, pattern in HARD_RULES
        ],
    }
