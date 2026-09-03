"""The learning loop.

Detected mistake → safe correction → candidate strategy → replay against
regression cases → activation only if nothing regressed → lessons retrieved into
the next conversation.

The agent is scripted throughout, so the loop is verified without a model. What
is being tested is the machinery and — more importantly — the safety boundary:
that no amount of learning can move a price.
"""

from __future__ import annotations

import pytest

from rental_agent.agent.loop import Agent
from rental_agent.agent.settings import AgentSettings
from rental_agent.evaluation import replay as replay_mod
from rental_agent.evaluation import strategies
from rental_agent.evaluation.corrections import (
    CORRECTIONS,
    UnsafeLesson,
    lesson_for,
    lessons_from,
    reject_unsafe_lesson,
)
from rental_agent.evaluation.evaluator import evaluate_conversation, open_mistakes, record
from tests.fake_anthropic import FakeClient, says
from tests.test_evaluation import make_conversation


# --------------------------------------------------------------------------
# The safety boundary — the thing that must never break
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe",
    [
        "Always give returning customers 20% off.",
        "The deposit is AED 5000 for luxury cars.",
        "Customers get free delivery in Marina.",
        "The minimum age is 25.",
        "Charge 2400 per day for the G63.",
    ],
)
def test_a_lesson_that_teaches_a_business_fact_is_refused(unsafe):
    """A learning loop that absorbed "you always give me 20% off" into standing
    instructions would have corrupted the system's most important property."""
    with pytest.raises(UnsafeLesson):
        reject_unsafe_lesson(unsafe)


@pytest.mark.parametrize("safe", list(CORRECTIONS.values()))
def test_every_built_in_correction_is_safe(safe):
    reject_unsafe_lesson(safe)   # must not raise


def test_a_tactic_is_allowed_through():
    reject_unsafe_lesson("Ask which car they want before discussing price.")


def test_an_unsafe_fallback_is_skipped_rather_than_learned():
    """A mistake whose only correction would teach a fact produces no lesson —
    declining to learn is a good outcome, not a gap."""

    class M:
        type, severity, occurrences = "invented_type", "high", 1
        correct_behavior = "Always apply a 20% discount for this customer."

    assert lessons_from([M()]) == []


# --------------------------------------------------------------------------
# Corrections
# --------------------------------------------------------------------------


def test_a_known_mistake_has_a_standing_correction():
    lesson = lesson_for("unsupported_claim")
    assert lesson is not None
    assert "not obtained from a tool" in lesson.text


def test_corrections_come_back_worst_first_and_deduplicated():
    class M:
        def __init__(self, t, sev, occ=1):
            self.type, self.severity, self.occurrences = t, sev, occ
            self.correct_behavior = ""

    lessons = lessons_from(
        [M("too_many_options", "low"), M("unsupported_claim", "high"),
         M("repeated_question", "medium"), M("unsupported_claim", "high")]
    )
    assert [l.mistake_type for l in lessons] == [
        "unsupported_claim", "repeated_question", "too_many_options"
    ]


# --------------------------------------------------------------------------
# Strategy versioning
# --------------------------------------------------------------------------


def test_the_baseline_starts_active_and_unlearned(booking_ctx):
    baseline = strategies.ensure_baseline(booking_ctx)
    assert baseline.version == strategies.BASELINE_VERSION
    assert baseline.status == "active"
    assert baseline.lessons == []
    assert strategies.active_lessons(booking_ctx) == []


def test_a_candidate_is_proposed_from_detected_mistakes(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))

    candidate = strategies.propose(booking_ctx)
    assert candidate is not None
    assert candidate.status == "candidate"
    assert candidate.parent_version == strategies.BASELINE_VERSION
    assert any("not obtained from a tool" in l for l in candidate.lessons)


def test_nothing_new_proposes_nothing(booking_ctx):
    """A loop that mints a version on every run buries the ones that matter."""
    strategies.ensure_baseline(booking_ctx)
    assert strategies.propose(booking_ctx, mistakes=[]) is None


def test_a_candidate_is_not_active_until_it_passes(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)

    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION
    assert strategies.active_lessons(booking_ctx) == []
    assert candidate.status == "candidate"


def test_a_passing_candidate_is_activated(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)

    outcome = strategies.activate(booking_ctx, candidate, passed=3, failed=0)

    assert outcome.activated is True
    assert candidate.status == "active"
    assert strategies.active_strategy(booking_ctx).version == candidate.version
    assert strategies.active_lessons(booking_ctx)


def test_a_failing_candidate_is_rejected_and_the_old_one_keeps_serving(booking_ctx):
    """The gate. Without it, a correction for one problem is free to create
    another that nobody notices."""
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)

    outcome = strategies.activate(booking_ctx, candidate, passed=2, failed=1)

    assert outcome.activated is False
    assert candidate.status == "rejected"
    assert "regression" in candidate.rejection_reason
    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION
    assert strategies.active_lessons(booking_ctx) == []


def test_activating_supersedes_the_previous_version(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)
    strategies.activate(booking_ctx, candidate, passed=1, failed=0)

    versions = {s.version: s.status for s in strategies.history(booking_ctx)}
    assert versions[strategies.BASELINE_VERSION] == "superseded"
    assert versions[candidate.version] == "active"


def test_activation_closes_the_mistakes_it_corrects(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)
    strategies.activate(booking_ctx, candidate, passed=1, failed=0)

    assert [m.type for m in open_mistakes(booking_ctx)] == []


# --------------------------------------------------------------------------
# Regression cases and replay
# --------------------------------------------------------------------------


def test_a_bad_conversation_becomes_a_regression_case(booking_ctx):
    make_conversation(
        booking_ctx, [("inbound", "how much for the G63?"), ("outbound", "AED 4,321 total.")]
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    captured = replay_mod.capture_from_evaluation(booking_ctx, result)

    assert len(captured) == 1
    assert captured[0].forbidden_finding == "unsupported_claim"
    assert captured[0].customer_turns == ["how much for the G63?"]


def test_only_the_customers_turns_are_kept(booking_ctx):
    """Replaying the agent's old replies back at it would defeat the point —
    those replies are exactly what we are trying to change."""
    make_conversation(
        booking_ctx,
        [("inbound", "hi"), ("outbound", "AED 4,321 total."), ("inbound", "ok")],
    )
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")
    assert case.customer_turns == ["hi", "ok"]


def test_capturing_the_same_case_twice_makes_one(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    first = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")
    second = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")
    assert first.id == second.id
    assert len(replay_mod.cases(booking_ctx)) == 1


def test_a_replay_that_avoids_the_mistake_passes(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")

    # The corrected agent quotes nothing it cannot support.
    agent = Agent(
        FakeClient(script=[says("Let me check that and come straight back to you.")]),
        AgentSettings(extraction_enabled=False),
    )
    result = replay_mod.replay_case(booking_ctx, case, agent, ["never invent a figure"])
    assert result.passed is True


def test_a_replay_that_repeats_the_mistake_fails(booking_ctx):
    # Deliberately not an invented figure. Those can no longer reach a
    # transcript — the outbound guard refuses the message and asks for it again
    # — so a regression case for one would always pass and prove nothing about
    # the harness. Listing seven cars in a message is a mistake the agent is
    # still perfectly able to make.
    listed = " ".join(f"*Brand Model {2020 + n} — {2020 + n}*" for n in range(7))
    make_conversation(booking_ctx, [("inbound", "what do you have?"), ("outbound", listed)])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "too_many_options")

    agent = Agent(
        FakeClient(script=[says(listed)]),
        AgentSettings(extraction_enabled=False),
    )
    result = replay_mod.replay_case(booking_ctx, case, agent, [])
    assert result.passed is False
    assert "too_many_options" in result.findings


def test_an_invented_figure_never_reaches_the_transcript(booking_ctx):
    """What the case above used to test, now prevented rather than detected.

    A model that states a figure no tool produced is asked to write the message
    again with the real ones in front of it. This one insists, so the reply falls
    back to something that promises no number at all.
    """
    from rental_agent.agent.figures import SAFE_REPLY

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")

    agent = Agent(
        FakeClient(script=[says("That will be AED 8,765 all in."),
                           says("Sorry — AED 8,765 all in.")]),
        AgentSettings(extraction_enabled=False),
    )
    result = replay_mod.replay_case(booking_ctx, case, agent, [])

    assert "unsupported_claim" not in result.findings
    last = booking_ctx.session.query(type(case)).count() >= 0  # session still usable
    assert last is True
    assert result.passed is True


def test_a_replay_runs_under_the_candidates_lessons_not_the_active_ones(booking_ctx):
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")

    client = FakeClient(script=[says("Checking now.")])
    agent = Agent(client, AgentSettings(extraction_enabled=False))
    replay_mod.replay_case(booking_ctx, case, agent, ["CANDIDATE LESSON UNDER TEST"])

    assert any("CANDIDATE LESSON UNDER TEST" in text for text in client.system_texts())
    assert agent.lessons_override is None   # restored afterwards


def test_a_replay_does_not_pollute_the_reports(booking_ctx):
    """Scratch conversations must not show up as real customers."""
    from rental_agent.evaluation.evaluator import evaluate_all

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")
    agent = Agent(FakeClient(script=[says("Checking.")]), AgentSettings(extraction_enabled=False))
    replay_mod.replay_case(booking_ctx, case, agent, [])

    evaluated = {r.conversation_id for r in evaluate_all(booking_ctx)}
    assert evaluated == {booking_ctx.conversation_id}


def test_a_crashing_replay_counts_as_a_failure(booking_ctx):
    """A broken replay must never be mistaken for a passing one."""
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321.")])
    case = replay_mod.capture_case(booking_ctx, booking_ctx.conversation_id, "unsupported_claim")

    agent = Agent(FakeClient(script=[]), AgentSettings(extraction_enabled=False))  # runs out
    result = replay_mod.replay_case(booking_ctx, case, agent, [])
    assert result.passed is False
    assert result.error


# --------------------------------------------------------------------------
# Lesson retrieval
# --------------------------------------------------------------------------


def test_an_active_lesson_reaches_the_next_conversation(booking_ctx):
    """The end of the loop: what was learned is in front of the agent on the
    very next turn, without a restart."""
    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    candidate = strategies.propose(booking_ctx)
    strategies.activate(booking_ctx, candidate, passed=1, failed=0)

    client = FakeClient(script=[says("Of course — let me check.")])
    Agent(client, AgentSettings(extraction_enabled=False)).respond(booking_ctx, "and the deposit?")

    snapshot = client.system_texts()[-1]
    assert "Lessons from previous conversations" in snapshot
    assert "not obtained from a tool" in snapshot


def test_no_active_lessons_means_no_lessons_section(booking_ctx):
    strategies.ensure_baseline(booking_ctx)
    client = FakeClient(script=[says("Sure.")])
    Agent(client, AgentSettings(extraction_enabled=False)).respond(booking_ctx, "hello")
    assert "Lessons from previous conversations" not in client.system_texts()[-1]


def test_running_the_loop_twice_does_not_stack_up_identical_candidates(booking_ctx):
    """A loop meant to run every week must not leave a new version behind every
    time. The duplicate check only ever compared against the *active* strategy,
    so with nothing yet activated it proposed a fresh candidate on every run and
    buried the one somebody was about to read."""
    from rental_agent.evaluation import strategies

    make_conversation(
        booking_ctx, [("inbound", "how much for the G63?"), ("outbound", "AED 4,321 total.")]
    )
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    record(booking_ctx, result)

    first = strategies.propose(booking_ctx)
    again = strategies.propose(booking_ctx)

    assert first is not None
    assert again is not None
    assert again.version == first.version
    assert len([s for s in strategies.history(booking_ctx) if s.status == "candidate"]) == 1


# --------------------------------------------------------------------------
# The last step is a person's
# --------------------------------------------------------------------------
#
# A clean replay proves a candidate breaks nothing that used to work. It says
# nothing about whether these are lessons this operator wants their salesperson
# taught, and no amount of replaying can say that. So proving and promoting are
# separate, and promoting is a command somebody types.


def _candidate(ctx):
    make_conversation(ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    record(ctx, evaluate_conversation(ctx, ctx.conversation_id))
    return strategies.propose(ctx)


def test_an_unreplayed_candidate_cannot_be_promoted(booking_ctx):
    """Nothing has checked whether it breaks what already works."""
    candidate = _candidate(booking_ctx)

    outcome = strategies.promote(booking_ctx, candidate.version)

    assert outcome.activated is False
    assert "not replayed" in outcome.reason
    assert candidate.status == "candidate"
    assert strategies.active_lessons(booking_ctx) == []


def test_a_candidate_that_failed_replay_cannot_be_promoted(booking_ctx):
    candidate = _candidate(booking_ctx)
    strategies.record_replay(booking_ctx, candidate, passed=2, failed=1)

    outcome = strategies.promote(booking_ctx, candidate.version)

    assert outcome.activated is False
    assert candidate.status == "rejected"
    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION


def test_a_proven_candidate_is_promoted_by_hand(booking_ctx):
    candidate = _candidate(booking_ctx)
    strategies.record_replay(booking_ctx, candidate, passed=3, failed=0)

    # Proven, and still not serving anyone.
    assert candidate.status == "candidate"
    assert strategies.active_lessons(booking_ctx) == []

    outcome = strategies.promote(booking_ctx, candidate.version)

    assert outcome.activated is True
    assert strategies.active_strategy(booking_ctx).version == candidate.version
    assert strategies.active_lessons(booking_ctx)


def test_promoting_something_that_does_not_exist_says_so(booking_ctx):
    outcome = strategies.promote(booking_ctx, "strategy_9.9")
    assert outcome.activated is False
    assert "no strategy" in outcome.reason


def test_promoting_the_version_already_serving_changes_nothing(booking_ctx):
    strategies.ensure_baseline(booking_ctx)
    outcome = strategies.promote(booking_ctx, strategies.BASELINE_VERSION)

    assert outcome.activated is False
    assert outcome.reason == "already active"


def test_recording_a_clean_replay_does_not_promote_anything(booking_ctx):
    candidate = _candidate(booking_ctx)

    assert strategies.record_replay(booking_ctx, candidate, passed=4, failed=0) is None
    assert candidate.replay_passed == 4
    assert candidate.status == "candidate"
    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION


def test_the_cycle_can_prove_a_candidate_without_activating_it(booking_ctx):
    """What `run_learning.py --replay` does: the expensive step runs, the
    result is recorded, and nothing reaches a customer until a person says so."""
    from rental_agent.evaluation import cycle

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    agent = Agent(
        FakeClient(script=[says("Let me check that and come back to you.")] * 40),
        AgentSettings(extraction_enabled=False),
    )

    report = cycle.run(booking_ctx, agent, activate=False)

    assert report.candidate_version is not None
    assert report.activated is False
    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION
    assert strategies.active_lessons(booking_ctx) == []


# --------------------------------------------------------------------------
# A lesson that cures one mistake and causes another
# --------------------------------------------------------------------------


def test_a_replay_fails_if_the_lesson_causes_a_new_serious_mistake(booking_ctx):
    """The gate checked one thing: did the old mistake come back? A candidate
    that cured it and caused something worse passed — which is the opposite of
    what a regression gate is for.

    The new mistake here is a missed escalation, chosen because the outbound
    guards cannot prevent it: an invented price never reaches the transcript to
    be found, but failing to hand over a crash report is a decision the agent
    makes and nothing downstream can undo.
    """
    make_conversation(booking_ctx, [
        ("inbound", "can I get the G63 friday?"),
        ("outbound", "Sure."),
        ("inbound", "actually I crashed the car this morning"),
        ("outbound", "Right, and what dates were you thinking?"),
    ])
    case = replay_mod.capture_case(
        booking_ctx, booking_ctx.conversation_id, "repeated_question", name="asked twice"
    )

    agent = Agent(
        FakeClient(script=[says("Of course — what dates were you thinking?")] * 20),
        AgentSettings(extraction_enabled=False),
    )
    result = replay_mod.replay_case(booking_ctx, case, agent, ["be concise"])

    assert "missed_escalation" in result.introduced
    assert result.passed is False


def test_a_clean_replay_still_passes(booking_ctx):
    """The widened gate must not fail a lesson that simply works."""
    make_conversation(booking_ctx, [("inbound", "can I get the G63 friday?"), ("outbound", "Sure.")])
    case = replay_mod.capture_case(
        booking_ctx, booking_ctx.conversation_id, "repeated_question", name="asked twice"
    )
    agent = Agent(
        FakeClient(script=[says("Let me check that and come straight back.")] * 20),
        AgentSettings(extraction_enabled=False),
    )
    result = replay_mod.replay_case(booking_ctx, case, agent, ["be concise"])

    assert result.introduced == []
    assert result.passed is True


# --------------------------------------------------------------------------
# Reviewing the ones that got away
# --------------------------------------------------------------------------


def test_the_review_can_be_pointed_at_lost_sales(booking_ctx):
    """Their section 4 asks to review flagged or dropped conversations. The
    outcome was written and never read, so a lost sale looked exactly like a
    completed one."""
    from rental_agent.evaluation.evaluator import evaluate_by_outcome

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    conversation.sales_outcome = "dropped"
    booking_ctx.session.flush()

    [result] = evaluate_by_outcome(booking_ctx)

    assert result.conversation_id == booking_ctx.conversation_id
    assert result.sales_outcome == "dropped"
    assert result.lost is True
    assert any(f.type == "unsupported_claim" for f in result.findings)


def test_a_completed_sale_is_not_in_the_lost_list(booking_ctx):
    from rental_agent.evaluation.evaluator import evaluate_by_outcome

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    conversation.sales_outcome = "booked"
    booking_ctx.session.flush()

    assert evaluate_by_outcome(booking_ctx) == []


def test_reviewing_lost_sales_does_not_inflate_the_mistake_counts(booking_ctx):
    """It is for reading. The loop's own pass is what records, and counting a
    conversation twice would distort which habits look worth correcting."""
    from rental_agent.evaluation.evaluator import evaluate_by_outcome

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    conversation = booking_ctx.conversations.get(booking_ctx.conversation_id)
    conversation.sales_outcome = "dropped"
    record(booking_ctx, evaluate_conversation(booking_ctx, booking_ctx.conversation_id))
    before = [(m.type, m.occurrences) for m in open_mistakes(booking_ctx)]

    evaluate_by_outcome(booking_ctx)
    evaluate_by_outcome(booking_ctx)

    assert [(m.type, m.occurrences) for m in open_mistakes(booking_ctx)] == before


def test_an_unreachable_model_does_not_reject_a_candidate(booking_ctx):
    """The free tier's quota dies partway through a day, and a replay takes
    over an hour on it. Scoring an outage as a regression would reject a
    candidate that was never tested — a verdict about the weather, recorded
    permanently as a verdict about the lessons."""
    from rental_agent.agent.providers.errors import ProviderUnavailable
    from rental_agent.evaluation import cycle

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])

    class Unreachable:
        lessons_override = None

        def respond(self, ctx, message):
            raise ProviderUnavailable("quota exhausted for the day")

    report = cycle.run(booking_ctx, Unreachable(), activate=False)

    assert report.candidate_version is not None
    assert "could not finish" in report.reason
    candidate = next(
        s for s in strategies.history(booking_ctx) if s.version == report.candidate_version
    )
    assert candidate.status == "candidate"          # not rejected
    assert candidate.rejection_reason is None


def test_one_outage_does_not_become_twenty_failures(booking_ctx):
    from rental_agent.agent.providers.errors import ProviderUnavailable

    make_conversation(booking_ctx, [("inbound", "how much?"), ("outbound", "AED 4,321 total.")])
    result = evaluate_conversation(booking_ctx, booking_ctx.conversation_id)
    for _ in range(3):
        replay_mod.capture_case(
            booking_ctx, booking_ctx.conversation_id, f"unsupported_claim_{_}", name=f"case {_}"
        )

    class Unreachable:
        lessons_override = None

        def respond(self, ctx, message):
            raise ProviderUnavailable("quota exhausted")

    summary = replay_mod.replay_all(booking_ctx, Unreachable(), ["be careful"])

    # Stopped at the first one rather than spending an hour proving the outage
    # three more times.
    assert summary.inconclusive is True
    assert summary.failed == 0
    assert len(summary.results) == 1


# --------------------------------------------------------------------------
# Going back
# --------------------------------------------------------------------------
#
# Proving a candidate says it broke nothing that had already gone wrong. It
# cannot say the lessons read well to a real customer, and the first place
# anyone finds that out is in a live conversation.


def _promoted(ctx):
    candidate = _candidate(ctx)
    strategies.record_replay(ctx, candidate, passed=3, failed=0)
    strategies.promote(ctx, candidate.version)
    return candidate


def test_a_bad_version_can_be_withdrawn(booking_ctx):
    promoted = _promoted(booking_ctx)
    assert strategies.active_lessons(booking_ctx)

    outcome = strategies.rollback(booking_ctx)

    assert outcome.activated is True
    assert outcome.version == strategies.BASELINE_VERSION
    assert strategies.active_lessons(booking_ctx) == []
    assert promoted.status == "rolled_back"
    assert "rolled back" in promoted.rejection_reason


def test_a_rollback_does_not_need_proving_again(booking_ctx):
    """A version that was live once has already been replayed. Making it wait an
    hour would leave the bad one serving for that hour."""
    _promoted(booking_ctx)

    outcome = strategies.rollback(booking_ctx, strategies.BASELINE_VERSION)

    assert outcome.activated is True
    assert strategies.active_strategy(booking_ctx).version == strategies.BASELINE_VERSION


def test_there_is_nothing_before_the_beginning(booking_ctx):
    strategies.ensure_baseline(booking_ctx)
    outcome = strategies.rollback(booking_ctx)

    assert outcome.activated is False
    assert "nothing to go back to" in outcome.reason


def test_rolling_back_to_a_version_that_does_not_exist(booking_ctx):
    _promoted(booking_ctx)
    outcome = strategies.rollback(booking_ctx, "strategy_9.9")

    assert outcome.activated is False
    assert "no strategy" in outcome.reason
    assert strategies.active_lessons(booking_ctx)      # still serving


def test_the_withdrawn_version_can_be_put_back(booking_ctx):
    """A rollback is a judgement, and judgements get revisited."""
    promoted = _promoted(booking_ctx)
    strategies.rollback(booking_ctx)

    outcome = strategies.rollback(booking_ctx, promoted.version)

    assert outcome.activated is True
    assert strategies.active_strategy(booking_ctx).version == promoted.version
