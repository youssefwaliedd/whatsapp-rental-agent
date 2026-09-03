"""Reading a conversation for what a fixed check cannot see.

The twelve deterministic checks find what is wrong. They cannot find what is
weak — an objection brushed aside, a close never attempted, a customer talked
past — and that is most of what loses a sale.

These tests run against a scripted reviewer, so they check the part that has to
be right whatever the model says: that a finding without evidence is discarded,
that nothing it produces can carry a figure into a lesson, and that a reviewer
which fails loses nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rental_agent.evaluation.corrections import UnsafeLesson, reject_unsafe_lesson
from rental_agent.evaluation.review import KINDS, review_conversation


@dataclass
class Block:
    text: str
    type: str = "text"


@dataclass
class Reply:
    content: list


class Reviewer:
    """A model that says exactly what the test needs it to say."""

    def __init__(self, text: str):
        self.messages = self
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Reply(content=[Block(self._text)])


class Broken:
    def __init__(self):
        self.messages = self

    def create(self, **kwargs):
        raise RuntimeError("review model unreachable")


@dataclass
class Msg:
    direction: str
    content: str


TALK = [
    Msg("inbound", "how much for the G63 this weekend?"),
    Msg("outbound", "AED 1,899 a day. Shall I check availability?"),
    Msg("inbound", "that's a lot more than the other place quoted me"),
    Msg("outbound", "What dates were you thinking?"),
]


def test_a_weak_reply_is_reported_with_what_was_said(booking_ctx):
    reviewer = Reviewer('''{"findings": [
      {"kind": "weak_objection_handling",
       "summary": "the customer said it was more expensive elsewhere and was asked for dates instead",
       "quote": "that's a lot more than the other place quoted me",
       "better": "acknowledge the comparison and say what the price includes before moving on"}]}''')

    result = review_conversation(reviewer, TALK)

    [finding] = result.findings
    assert finding.type == "weak_objection_handling"
    assert finding.evidence["quote"].startswith("that's a lot more")
    assert finding.evidence["source"] == "review"


def test_a_finding_with_no_evidence_is_discarded(booking_ctx):
    """A reviewer that cannot point at what it means is guessing, and a guess
    that reaches the mistake ledger becomes a lesson."""
    reviewer = Reviewer('''{"findings": [
      {"kind": "tone_mismatch", "summary": "the tone was off", "quote": "", "better": "be warmer"}]}''')

    assert review_conversation(reviewer, TALK).findings == []


def test_an_unknown_kind_is_discarded(booking_ctx):
    """A closed list, so habits accumulate instead of a new category appearing
    every run and nothing ever reaching a count worth correcting."""
    reviewer = Reviewer('''{"findings": [
      {"kind": "vibes_were_wrong", "summary": "s", "quote": "q", "better": "b"}]}''')

    assert review_conversation(reviewer, TALK).findings == []


def test_questions_and_objections_are_recorded_separately(booking_ctx):
    """Their Stage 2 asks for frequently asked questions and customer
    objections. Those are the operator's reading, not the agent's mistakes."""
    reviewer = Reviewer('''{"findings": [
      {"kind": "faq", "summary": "asked whether the deposit is refundable",
       "quote": "is the deposit refundable?", "better": "have a ready answer"},
      {"kind": "objection", "summary": "price compared to a competitor",
       "quote": "that's a lot more than the other place", "better": "say what is included"}]}''')

    result = review_conversation(reviewer, TALK)

    assert result.findings == []
    assert [o["kind"] for o in result.observations] == ["faq", "objection"]


def test_a_review_never_outranks_a_deterministic_check(booking_ctx):
    """A check proves what it found. This is a judgement, and a judgement that
    could outrank proof would decide which habits get corrected first."""
    reviewer = Reviewer('''{"findings": [
      {"kind": "no_close_attempted", "summary": "never asked for the booking",
       "quote": "What dates were you thinking?", "better": "ask for the booking"}]}''')

    [finding] = review_conversation(reviewer, TALK).findings
    assert finding.severity == "medium"


@pytest.mark.parametrize("better", [
    "offer AED 200 off to close it",
    "tell them the deposit is 5,000",
    "always give 10% on weekly rentals",
])
def test_a_review_cannot_smuggle_a_figure_into_a_lesson(better):
    """The gate does not care where a lesson came from. A reviewer that decided
    the agent should have quoted a number is refused exactly like anything else."""
    with pytest.raises(UnsafeLesson):
        reject_unsafe_lesson(better)


def test_a_reviewer_that_fails_loses_nothing(booking_ctx):
    result = review_conversation(Broken(), TALK)

    assert result.ran is False
    assert result.findings == []


def test_a_clean_conversation_produces_nothing(booking_ctx):
    """Saying nothing is a valid and common answer."""
    assert review_conversation(Reviewer('{"findings": []}'), TALK).findings == []


def test_unreadable_output_is_discarded_not_guessed_at(booking_ctx):
    assert review_conversation(Reviewer("I think it went fine!"), TALK).findings == []


def test_the_reviewer_is_given_the_conversation_and_told_what_not_to_do(booking_ctx):
    reviewer = Reviewer('{"findings": []}')
    review_conversation(reviewer, TALK)

    [call] = reviewer.calls
    assert "that's a lot more than the other place quoted me" in call["messages"][0]["content"]
    # It must not duplicate the deterministic checks, which are better at it.
    assert "Do not report those" in call["system"]
    assert "Never state or suggest a price" in call["system"]


def test_every_kind_has_a_situation_a_person_can_read():
    assert all(KINDS.values())


# --------------------------------------------------------------------------
# What customers ask, kept rather than counted and dropped
# --------------------------------------------------------------------------


def test_questions_and_objections_are_kept(booking_ctx, monkeypatch):
    """They were being collected, logged as a number, and thrown away — so two
    of the operator's Stage 2 bullets looked done and were not."""
    from sqlalchemy import select

    from rental_agent.evaluation import evaluator
    from rental_agent.store.models import Observation

    reviewer = Reviewer('''{"findings": [
      {"kind": "faq", "summary": "asked whether the deposit is refundable",
       "quote": "is the deposit refundable?", "better": "have a ready answer"},
      {"kind": "objection", "summary": "compared the price to another company",
       "quote": "that's more than the other place", "better": "say what is included"}]}''')

    monkeypatch.setattr(evaluator, "review_enabled", lambda: True)
    monkeypatch.setattr(
        "rental_agent.agent.providers.build_client", lambda *a, **k: reviewer
    )
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound", content="is the deposit refundable?", now=booking_ctx.now(),
    )
    booking_ctx.session.flush()

    evaluator.review_and_record(booking_ctx, booking_ctx.conversation_id)

    kept = list(booking_ctx.session.scalars(select(Observation)))
    assert sorted(o.kind for o in kept) == ["faq", "objection"]
    assert any("refundable" in o.quote for o in kept)


def test_an_observation_is_not_a_mistake(booking_ctx, monkeypatch):
    """An agent is not wrong for being asked what the deposit is. Filing that as
    a mistake would teach it to stop being asked."""
    from rental_agent.evaluation import evaluator
    from rental_agent.evaluation.evaluator import open_mistakes

    reviewer = Reviewer('''{"findings": [
      {"kind": "faq", "summary": "asked about the deposit",
       "quote": "what deposit do you take?", "better": "have a ready answer"}]}''')
    monkeypatch.setattr(evaluator, "review_enabled", lambda: True)
    monkeypatch.setattr(
        "rental_agent.agent.providers.build_client", lambda *a, **k: reviewer
    )
    booking_ctx.messages.record(
        conversation_id=booking_ctx.conversation_id,
        direction="inbound", content="what deposit do you take?", now=booking_ctx.now(),
    )
    booking_ctx.session.flush()

    evaluator.review_and_record(booking_ctx, booking_ctx.conversation_id)

    assert open_mistakes(booking_ctx) == []
