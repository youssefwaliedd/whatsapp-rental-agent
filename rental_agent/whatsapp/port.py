"""Refusing to pretend the server started.

A process from weeks earlier was still holding port 8000. uvicorn logged its
bind error into a wall of fleet-refresh output, printed nothing else, and the
terminal was left showing "Uvicorn running on http://0.0.0.0:8000" from the
line before. Every request afterwards went to the old process, which had none of
the routes being tested — so a working feature looked broken for twenty minutes.

Checked before anything starts, and refused loudly, because the failure mode is
not a crash. It is a server that appears to be running and is not yours.
"""

from __future__ import annotations

import socket


def in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something is already listening there."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def refuse_if_taken(port: int, *, host: str = "127.0.0.1") -> None:
    """Stop with something actionable rather than starting into a lie."""
    if not in_use(port, host):
        return
    raise SystemExit(
        f"\n  Port {port} is already in use, so this would not be the server you "
        f"reach.\n\n"
        f"  Something else is listening — most likely an older run of this that was "
        f"never stopped.\n  It will answer your requests with whatever code it "
        f"started with.\n\n"
        f"  Find it:  lsof -i :{port}\n"
        f"  Stop it:  lsof -ti:{port} | xargs kill -9\n"
    )
