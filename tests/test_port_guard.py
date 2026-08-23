"""Refusing to start into a lie.

A process from weeks earlier held port 8000. uvicorn's bind error scrolled past
in the fleet-refresh logging and the terminal still read "Uvicorn running on
http://0.0.0.0:8000" from the line above it. Every request afterwards reached the
old process — which had none of the routes being tested — so a feature that
worked looked broken for twenty minutes.

The failure mode is not a crash. It is a server that appears to be running and is
not yours, which is why this is checked before anything starts rather than left
to the bind.
"""

from __future__ import annotations

import socket

import pytest

from rental_agent.whatsapp.port import in_use, refuse_if_taken


@pytest.fixture
def taken_port():
    """A port with something actually listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


@pytest.fixture
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


def test_a_taken_port_is_detected(taken_port):
    assert in_use(taken_port) is True


def test_a_free_port_is_not(free_port):
    assert in_use(free_port) is False


def test_starting_on_a_free_port_is_silent(free_port):
    assert refuse_if_taken(free_port) is None


def test_starting_on_a_taken_port_stops(taken_port):
    with pytest.raises(SystemExit) as stopped:
        refuse_if_taken(taken_port)

    message = str(stopped.value)
    # Actionable, because the person reading it is mid-debug and the cause is
    # not obvious from anything else on screen.
    assert "already in use" in message
    assert f"lsof -ti:{taken_port}" in message
    assert "would not be the server you reach" in message
