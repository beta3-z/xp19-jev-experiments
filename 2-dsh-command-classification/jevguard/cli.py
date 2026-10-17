from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import DEFAULT_MODEL, DEFAULT_URL, PRICE_PER_MTOKEN, Answer, Client, TypeSafeError, mask_key
from .envfile import load_env_file
from .policy import POLICY_VERSION, RISKS, Verdict, compose, label, policy_spec
from .questions import DIMENSIONS, QUESTIONS_VERSION, build_questions, build_state
from .redact import redact
from .report import confusion, format_verdict, table

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_DATASET = ROOT / "data" / "commands.yaml"
DEFAULT_CWD = "C:\\Users\\user\\project"
LABELS = [*RISKS, "block"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Classify shell commands with TypeSafe Jev (DeepSeek Harness experiment).",
    )
    sub = parser.add_subparsers(dest="command_name", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help=f"load TYPESAFE_API_KEY from this .env file (default: {DEFAULT_ENV_FILE})")
    common.add_argument("--model", help=f"Jev model (default: {DEFAULT_MODEL})")
    common.add_argument("--base-url", help=f"override endpoint (default: {DEFAULT_URL})")
    common.add_argument("--timeout", type=float, default=20.0, help="per-request timeout, seconds")
    common.add_argument("--api-key", help="API key; prefer TYPESAFE_API_KEY or --env-file")

    doctor = sub.add_parser("doctor", parents=[common], help="check credentials and reachability")
    doctor.set_defaults(func=cmd_doctor)

    classify = sub.add_parser("classify", parents=[common], help="classify one command")
    classify.add_argument("command", nargs="*", help="command text; omitted means --stdin or --file")
    classify.add_argument("--stdin", action="store_true", help="read the command from stdin")
    classify.add_argument("--file", help="read the command from a file")
    classify.add_argument("--shell", default="pwsh", help="pwsh, bash, cmd (default: pwsh)")
    classify.add_argument("--cwd", default=DEFAULT_CWD, help=f"working directory shown to Jev (default: {DEFAULT_CWD})")
    classify.add_argument(
        "--mode",
        choices=["workspace-write", "danger-full-access"],
        help="sandbox mode the agent requested",
    )
    classify.add_argument("--justification", help="agent's stated reason (untrusted; omitted by default)")
    classify.add_argument("--no-redact", action="store_true", help="send the command verbatim")
    classify.add_argument("--json", action="store_true", help="machine-readable output")
    classify.set_defaults(func=cmd_classify)

    replay = sub.add_parser("replay", parents=[common], help="replay a labelled dataset")
    replay.add_argument("dataset", nargs="?", default=str(DEFAULT_DATASET))
    replay.add_argument("--jsonl", help="write per-command records here")
    replay.add_argument("--no-save", action="store_true", help="do not write a JSONL record")
    replay.add_argument("--limit", type=int, help="only the first N entries")
    replay.add_argument("--sleep", type=float, default=0.05, help="pause between calls, seconds")
    replay.add_argument("--fail-under", type=float, help="exit 1 when accuracy is below this")
    replay.set_defaults(func=cmd_replay)

    spec = sub.add_parser("export-spec", help="dump the question set and policy as JSON")
    spec.add_argument("-o", "--output", default=str(ROOT / "spec.json"))
    spec.set_defaults(func=cmd_export_spec)

    return parser


def _read_command(args: argparse.Namespace) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8").strip()
    if args.stdin:
        return sys.stdin.read().strip()
    if args.command:
        return " ".join(args.command).strip()
    raise RuntimeError("pass a command, or use --stdin / --file")


def _make_client(args: argparse.Namespace) -> Client:
    if args.env_file:
        load_env_file(args.env_file)
    elif DEFAULT_ENV_FILE.is_file():
        load_env_file(DEFAULT_ENV_FILE)
    return Client(
        api_key=args.api_key,
        model=args.model or os.environ.get("TYPESAFE_MODEL") or DEFAULT_MODEL,
        base_url=args.base_url,
        timeout=args.timeout,
    )


def _judge(
    client: Client,
    command: str,
    *,
    shell: str,
    cwd: str,
    mode: str | None,
    justification: str | None = None,
    no_redact: bool = False,
) -> tuple[str, list[str], Answer, Verdict]:
    sent, hits = (command, []) if no_redact else redact(command)
    state = build_state(sent, shell=shell, cwd=cwd, requested_mode=mode)
    if justification:
        state["claimed_justification_untrusted"] = justification
    answer = client.ask(state, build_questions())
    scores = {name: answer.noul(name) for name in DIMENSIONS}
    probabilities, confidence = answer.choice("verdict")
    verdict = compose(scores, probabilities=probabilities, confidence=confidence, command=command)
    return sent, hits, answer, verdict


def _load_dataset(path: Path, limit: int | None) -> list[dict[str, Any]]:
    import yaml

    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(entries, list):
        raise RuntimeError(f"{path} must contain a YAML list")
    for index, entry in enumerate(entries, start=1):
        where = f"{path} entry {index}"
        if not isinstance(entry, dict):
            raise RuntimeError(f"{where} is not a mapping")
        if not isinstance(entry.get("command"), str) or not entry["command"].strip():
            raise RuntimeError(f"{where} has no command")
        if entry.get("expected") not in LABELS:
            raise RuntimeError(f"{where} has expected={entry.get('expected')!r}; use one of {LABELS}")
    return entries[:limit] if limit else entries


def _save_records(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {path}")


def cmd_doctor(args: argparse.Namespace) -> int:
    client = _make_client(args)
    print(f"key       {mask_key(client.api_key)}")
    print(f"endpoint  {client.base_url}")
    print(f"model     {client.model}")
    answer = client.ask(
        {"platform": "windows", "shell": "pwsh", "command": "Get-ChildItem -Force"},
        {
            "probe": {
                "type": "noul",
                "instructions": "Is this a harmless read-only directory listing command?",
            }
        },
    )
    print(f"probe     noul={answer.noul('probe'):.2f}")
    print(f"latency   {answer.latency_ms:.0f} ms")
    print(f"tokens    {answer.input_tokens} in / {answer.output_tokens} out")
    print(f"served by {answer.model or '(model not reported)'}")
    print("ok")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    client = _make_client(args)
    sent, hits, answer, verdict = _judge(
        client,
        _read_command(args),
        shell=args.shell,
        cwd=args.cwd,
        mode=args.mode,
        justification=args.justification,
        no_redact=args.no_redact,
    )

    if args.json:
        payload = {
            "command": sent,
            "redacted": bool(hits),
            "redaction_rules": hits,
            "risk": verdict.risk,
            "action": verdict.action,
            "tags": list(verdict.tags),
            "reason": verdict.reason,
            "scores": verdict.scores,
            "confidence": verdict.confidence,
            "probabilities": verdict.probabilities,
            "policy_version": POLICY_VERSION,
            "questions_version": QUESTIONS_VERSION,
            "model": answer.model,
            "latency_ms": round(answer.latency_ms, 1),
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(format_verdict(sent, verdict, answer))
    if hits:
        print(f"REDACTED {', '.join(hits)}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    client = _make_client(args)
    dataset = Path(args.dataset)
    entries = _load_dataset(dataset, args.limit)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.jsonl) if args.jsonl else ROOT / "runs" / f"replay-{stamp}.jsonl"
    records: list[dict[str, Any]] = []
    rows: list[dict[str, str]] = []
    total_tokens = 0

    print(f"dataset  {dataset}  ({len(entries)} commands)")
    print(f"model    {client.model}")
    print()

    for index, entry in enumerate(entries, start=1):
        command = entry["command"]
        expected = entry["expected"]
        shell = str(entry.get("shell") or "pwsh")
        mode = entry.get("mode")
        cwd = entry.get("cwd") or DEFAULT_CWD

        try:
            _, hits, answer, verdict = _judge(client, command, shell=shell, cwd=cwd, mode=mode)
        except TypeSafeError as exc:
            print(f"[{index:>2}/{len(entries)}] FAIL  {command[:60]}")
            print(f"          {exc}")
            if records and not args.no_save:
                _save_records(records, out_path)
            return 2

        actual = label(verdict)
        ok = actual == expected
        total_tokens += answer.input_tokens
        rows.append({"expected": expected, "actual": actual})
        records.append(
            {
                "id": entry.get("id"),
                "command": command,
                "shell": shell,
                "requested_mode": mode,
                "expected": expected,
                "actual": actual,
                "ok": ok,
                "risk": verdict.risk,
                "action": verdict.action,
                "tags": list(verdict.tags),
                "scores": verdict.scores,
                "confidence": verdict.confidence,
                "probabilities": verdict.probabilities,
                "redaction_rules": hits,
                "policy_version": POLICY_VERSION,
                "questions_version": QUESTIONS_VERSION,
                "model": answer.model,
                "latency_ms": round(answer.latency_ms, 1),
                "input_tokens": answer.input_tokens,
            }
        )
        marker = "ok  " if ok else "MISS"
        print(f"[{index:>2}/{len(entries)}] {marker} {expected:>6} -> {actual:<7} {command[:64]}")
        if args.sleep:
            time.sleep(args.sleep)

    exact = sum(1 for row in rows if row["expected"] == row["actual"])
    accuracy = exact / len(rows) if rows else 0.0
    dangerous = [row for row in rows if row["expected"] in ("high", "block")]
    caught = [row for row in dangerous if row["actual"] in ("high", "block")]
    low_rows = [row for row in rows if row["expected"] == "low"]
    low_ok = [row for row in low_rows if row["actual"] == "low"]

    print()
    print(f"total {len(rows)}  exact {exact}  accuracy {accuracy:.1%}")
    if dangerous:
        print(f"danger recall   {len(caught)}/{len(dangerous)}  ({len(caught) / len(dangerous):.1%})")
    if low_rows:
        print(f"low precision   {len(low_ok)}/{len(low_rows)}  ({len(low_ok) / len(low_rows):.1%})")
    print(f"tokens {total_tokens:,}  est. cost ${total_tokens / 1_000_000 * PRICE_PER_MTOKEN:.4f}")
    print()
    print(confusion(rows, LABELS))
    misses = [record for record in records if not record["ok"]]
    if misses:
        print()
        print("mismatches")
        print(
            table(
                [
                    [
                        str(record["id"] or "")[:18],
                        str(record["expected"]),
                        str(record["actual"]),
                        str(record["command"])[:70],
                    ]
                    for record in misses
                ],
                ["id", "expected", "actual", "command"],
            )
        )

    if not args.no_save:
        print()
        _save_records(records, out_path)

    if args.fail_under is not None and accuracy < args.fail_under:
        return 1
    return 0


def cmd_export_spec(args: argparse.Namespace) -> int:
    spec = {
        "questions_version": QUESTIONS_VERSION,
        "questions": build_questions(),
        "dimensions": list(DIMENSIONS),
        **policy_spec(),
        "notes": {
            "model": DEFAULT_MODEL,
            "price_per_mtoken_usd": PRICE_PER_MTOKEN,
            "justification": "never trust the agent's stated reason; judge the command text",
            "malformed_reply": "a reply that fails validation is unknown, never low",
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (TypeSafeError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
