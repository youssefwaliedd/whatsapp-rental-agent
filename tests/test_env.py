"""`.env` parsing.

Small, but every credential and model name in the project comes through here —
a parsing slip surfaces as an unexplained provider error, not a config error.
"""

from __future__ import annotations

import os

from rental_agent.env import load_dotenv


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_a_plain_value_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_KEY", raising=False)
    load_dotenv(write(tmp_path, "DEMO_KEY=abc123\n"))
    assert os.environ["DEMO_KEY"] == "abc123"


def test_an_inline_comment_is_not_part_of_the_value(tmp_path, monkeypatch):
    """`MODEL=gemini-3.5-flash  # has quota` must not become a model name with a
    sentence in it — that 404s with an empty message and reads as a provider
    fault rather than a config one."""
    monkeypatch.delenv("DEMO_MODEL", raising=False)
    load_dotenv(write(tmp_path, "DEMO_MODEL=gemini-3.5-flash   # has quota today\n"))
    assert os.environ["DEMO_MODEL"] == "gemini-3.5-flash"


def test_a_quoted_value_is_kept_verbatim(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_QUOTED", raising=False)
    load_dotenv(write(tmp_path, 'DEMO_QUOTED="keep # this"\n'))
    assert os.environ["DEMO_QUOTED"] == "keep # this"


def test_full_line_comments_and_blanks_are_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_AFTER", raising=False)
    load_dotenv(write(tmp_path, "# a comment\n\n   \nDEMO_AFTER=yes\n"))
    assert os.environ["DEMO_AFTER"] == "yes"


def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
    """CI and one-off overrides must outrank the file."""
    monkeypatch.setenv("DEMO_PRIORITY", "from-environment")
    load_dotenv(write(tmp_path, "DEMO_PRIORITY=from-file\n"))
    assert os.environ["DEMO_PRIORITY"] == "from-environment"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}
