"""Measuring the agent against the conversations Delta actually had.

The parsing is what these cover. Whether the agent's answer is *better* than the
salesperson's is a judgement for a person, and a test that claimed to make it
would be lying — but the harness must at least put the right two things next to
each other, and must not answer a message nobody sent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rental_agent.evaluation.benchmark import EMPTY, Exchange, parse, transcripts


@pytest.fixture
def transcript(tmp_path) -> Path:
    path = tmp_path / "chat-99.txt"
    path.write_text(
        "[07/15/2026 13:29:16] Customer: Hi, is the Urus available?\n"
        "[07/15/2026 13:29:18] Bot: Welcome to Delta Rentals.\n"
        "[07/15/2026 13:29:33] Agent: When would you like it?\n"
        "and for how many days?\n"
        "[07/15/2026 13:30:47] Customer: Friday, 2 days\n"
        "[07/15/2026 13:31:06] Agent: Well noted.\n"
        "[07/15/2026 13:53:29] Agent: [photo]\n"
        "[07/15/2026 13:53:55] Customer: [photo]\n"
        "[07/15/2026 13:58:24] Customer: Looks great\n",
        encoding="utf-8",
    )
    return path


def test_only_the_customers_side_is_asked(transcript):
    asked = [e.customer for e in parse(transcript)]
    assert asked == ["Hi, is the Urus available?", "Friday, 2 days", "Looks great"]


def test_consecutive_messages_from_one_side_are_one_reply(transcript):
    # A salesperson sending three lines in a row is one reply. Splitting them
    # would be answering a conversation that never happened.
    first = parse(transcript)[0]
    assert "Welcome to Delta Rentals." in first.theirs
    assert "When would you like it?" in first.theirs
    assert "and for how many days?" in first.theirs


def test_a_customer_turn_with_no_reply_is_kept(transcript):
    last = parse(transcript)[-1]
    assert last.customer == "Looks great"
    assert last.theirs == ""


def test_an_attachment_is_not_a_question(transcript):
    assert all(e.customer != "[photo]" for e in parse(transcript))


@pytest.mark.parametrize("text", ["[photo]", "  [document] ", "[photo] [photo]", ""])
def test_placeholder_only_messages_are_recognised(text):
    assert EMPTY.match(text)


@pytest.mark.parametrize("text", ["Looks great", "[photo] is this the right one?"])
def test_a_message_with_words_is_not(text):
    assert not EMPTY.match(text)


def test_the_real_corpus_parses_into_exchanges():
    paths = transcripts()
    if not paths:
        pytest.skip("no reference conversations")
    total = 0
    for path in paths:
        exchanges = parse(path)
        assert all(isinstance(e, Exchange) and e.customer.strip() for e in exchanges)
        total += len(exchanges)
    # Thirteen real conversations; anything near zero means the format moved.
    assert total > 100, total
