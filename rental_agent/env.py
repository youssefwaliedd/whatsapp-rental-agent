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


def _clean(value: str) -> str:
    """Strip surrounding quotes, or an inline comment from an unquoted value.

    Without the comment handling, `MODEL=gemini-3.5-flash  # has quota` becomes a
    model name with a sentence in it and every request 404s on an empty error —
    which looks like a provider problem rather than a parsing one.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value.split(" #", 1)[0].split("\t#", 1)[0].strip()


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
        value = _clean(value)
        if not key:
            continue
        # setdefault, not assignment: an explicitly exported variable outranks
        # the file.
        os.environ.setdefault(key, value)
        loaded[key] = value
    return loaded
