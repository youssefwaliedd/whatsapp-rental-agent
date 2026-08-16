"""Minimal .env loading.

Keeps API keys in a gitignored project file rather than a shell profile, so the
key travels with the project and never lands in a transcript or a global
environment. Deliberately dependency-free — this is ten lines, not a package.

Real environment variables always win, so CI and one-off overrides still work.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Read KEY=value lines into the environment. Returns what was loaded."""
    target = path or ENV_PATH
    loaded: dict[str, str] = {}
    if not target.exists():
        return loaded

    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        # setdefault, not assignment: an explicitly exported variable outranks
        # the file.
        os.environ.setdefault(key, value)
        loaded[key] = value
    return loaded
