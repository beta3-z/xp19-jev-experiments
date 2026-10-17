from __future__ import annotations

from .client import Answer
from .policy import Verdict
from .questions import DIMENSIONS

RISK_MARK = {"low": "LOW", "medium": "MED", "high": "HIGH", "unknown": "????"}


def format_scores(scores: dict[str, float]) -> str:
    parts = [f"{name}={scores.get(name, 0.0):.2f}" for name in DIMENSIONS if name in scores]
    lines = [", ".join(parts[:5]), ", ".join(parts[5:])]
    return "\n".join(line for line in lines if line)


def format_verdict(command: str, verdict: Verdict, answer: Answer) -> str:
    header = f"[{RISK_MARK.get(verdict.risk, verdict.risk)}] {verdict.risk}  action={verdict.action}"
    lines = [
        f"COMMAND  {command}",
        f"RISK     {header}",
        f"CONF     {verdict.confidence:.2f}  (choice={verdict.top_choice})",
        f"TAGS     {', '.join(verdict.tags) if verdict.tags else '-'}",
        "SCORES",
    ]
    for line in format_scores(verdict.scores).splitlines():
        lines.append(f"         {line}")
    lines.append(f"WHY      {verdict.reason}")
    lines.append(f"RU       {verdict.human()}")
    lines.append(
        f"CALL     {answer.model or '?'} · {answer.latency_ms:.0f} ms · "
        f"{answer.input_tokens} in / {answer.output_tokens} out · attempt {answer.attempts}"
    )
    return "\n".join(lines)


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def confusion(rows: list[dict], labels: list[str]) -> str:
    counts = {expected: {actual: 0 for actual in labels} for expected in labels}
    for row in rows:
        counts[row["expected"]][row["actual"]] += 1
    header = ["expected \\ actual", *labels]
    body = [[expected, *(str(counts[expected][actual]) for actual in labels)] for expected in labels]
    return table(body, header)
