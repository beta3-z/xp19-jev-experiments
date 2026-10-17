from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> None:
    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"env file not found: {file}")
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name or name in os.environ:
            continue
        os.environ[name] = value
