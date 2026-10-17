"""Load TYPESAFE_API_KEY (and friends) from a local .env file.

Dependency-free on purpose: the project ships with Pillow and numpy only, and a
.env is a handful of KEY=VALUE lines. Variables already present in the real
environment win, so CI and ad-hoc overrides keep working.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | str) -> bool:
    """Read `path` and fill os.environ with the variables it defines.

    A missing file is not an error. Returns True when the file was read.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        os.environ.setdefault(key, _value(value.strip()))
    return True


def _value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    # Allow `KEY=value  # comment`; a bare `#` inside a value stays.
    if " #" in raw:
        raw = raw.split(" #", 1)[0]
    return raw.strip()
