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


# --- nagging ----------------------------------------------------------------
#
# From the benchmark against chat-11: the agent asked where to deliver the car in
# three consecutive replies while the customer asked, twice, whether the price
# was final. Their salesperson asked once and followed the customer. A person who
# keeps steering back to price is not going to produce an address because they
# were asked a third time — they are telling you what they care about.


from dataclasses import dataclass, field  # noqa: E402

from rental_agent.evaluation.checks import check_nagging  # noqa: E402


@dataclass
class NagState:
    asked_slots: list[str] = field(default_factory=list)
    redundant_asks: list[str] = field(default_factory=list)
    delivery_location: str | None = None
    pickup_at: str | None = None


def test_asking_a_third_time_for_something_never_given_is_a_finding():
    state = NagState(asked_slots=["delivery_location"] * 3)
    assert [f.type for f in check_nagging(state)] == ["nagging"]


def test_asking_twice_is_a_fair_second_try():
    assert check_nagging(NagState(asked_slots=["delivery_location"] * 2)) == []


def test_asking_repeatedly_and_getting_it_is_not_nagging():
    state = NagState(asked_slots=["delivery_location"] * 3, delivery_location="Dubai Marina")
    assert check_nagging(state) == []


def test_each_ignored_question_is_reported_once():
    state = NagState(asked_slots=["delivery_location"] * 3 + ["pickup_at"] * 4)
    findings = check_nagging(state)
    assert len(findings) == 2
    assert {f.evidence["times_asked"] for f in findings} == {3, 4}


def test_the_prompt_tells_the_agent_to_stop_asking():
    from rental_agent.agent.prompt import REASK_LIMIT
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="conv_x", customer_id="cus_x")
    state.asked_slots = ["delivery_location"] * REASK_LIMIT
    assert state.unanswered_asks("delivery_location") >= REASK_LIMIT

    state.delivery_location = "Dubai Marina"
    assert state.unanswered_asks("delivery_location") == 0


def test_the_state_block_stops_asking_once_they_have_ignored_it_twice():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from rental_agent.agent.prompt import render_state
    from rental_agent.domain.models import ConversationState

    now = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Dubai"))
    state = ConversationState(conversation_id="c", customer_id="u")
    state.pickup_at, state.return_at = now, now

    state.asked_slots = ["delivery_location"]
    once = render_state(state, now=now)
    assert "STOP ASKING" not in once
    assert "You will need" in once

    state.asked_slots = ["delivery_location", "delivery_location"]
    twice = render_state(state, now=now)
    assert "STOP ASKING" in twice
    # And it stops listing it as the thing to ask for.
    assert "ask once they are interested" not in twice


def test_a_supplied_answer_clears_the_stop_notice():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from rental_agent.agent.prompt import render_state
    from rental_agent.domain.models import ConversationState

    now = datetime(2026, 9, 1, 10, tzinfo=ZoneInfo("Asia/Dubai"))
    state = ConversationState(conversation_id="c", customer_id="u")
    state.pickup_at, state.return_at = now, now
    state.asked_slots = ["delivery_location"] * 4
    state.delivery_location = "Dubai Marina"
    assert "STOP ASKING" not in render_state(state, now=now)
