"""The conversational agent.

No API key and no network: every model response is scripted, so what is tested
is the machinery around the model — the additive state merge, tool dispatch,
escalation that cannot be forgotten, and the prompt structure the cache depends
on.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rental_agent.agent import prompt as prompt_mod
from rental_agent.agent.extraction import Extraction, merge
from rental_agent.agent.loop import SAFE_FALLBACK_REPLY, Agent
from rental_agent.agent.schemas import TOOLS, TOOL_NAMES
from rental_agent.agent.settings import AgentSettings
from rental_agent.domain.enums import Category, Stage
from rental_agent.tools.registry import ALL_HANDLERS
from tests.conftest import FROZEN_NOW, dt
from tests.fake_anthropic import FakeClient, calls, refuses, says


@pytest.fixture
def settings():
    return AgentSettings(max_tool_iterations=4)


def agent(script=None, extractions=None, settings=None, **kwargs) -> tuple[Agent, FakeClient]:
    client = FakeClient(script=script, extractions=extractions, **kwargs)
    return Agent(client, settings or AgentSettings(max_tool_iterations=4)), client


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------


def test_every_implemented_tool_is_exposed_to_the_model():
    assert sorted(TOOL_NAMES) == sorted(ALL_HANDLERS)


def test_tool_order_is_stable():
    """Tools render first in the prompt — reordering them would invalidate the
    entire cache on every request."""
    assert TOOL_NAMES == sorted(TOOL_NAMES)


def test_every_tool_declares_a_schema_and_says_when_to_call_it():
    for tool in TOOLS:
        assert tool["input_schema"]["type"] == "object"
        assert len(tool["description"]) > 80, tool["name"]
        assert "Call this" in tool["description"] or "call this" in tool["description"].lower()


# --------------------------------------------------------------------------
# Prompt structure — what prompt caching depends on
# --------------------------------------------------------------------------


def test_system_prompt_contains_nothing_volatile(engine):
    """A date or a name in the system prompt would move the cached prefix on
    every request. Volatile content belongs in the per-turn state message."""
    blocks = prompt_mod.build_system(engine.rules, engine.operator)
    text = blocks[0]["text"]
    assert "2026" not in text
    assert "veh_" not in text
    assert "DEMO-" not in text
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_system_prompt_is_byte_identical_across_calls(engine):
    first = prompt_mod.build_system(engine.rules, engine.operator)
    second = prompt_mod.build_system(engine.rules, engine.operator)
    assert first == second


def test_system_prompt_carries_the_fact_boundary(engine):
    text = prompt_mod.build_system(engine.rules, engine.operator)[0]["text"]
    assert "never produce one" in text
    assert "demonstration" in text.lower()
    assert "escalate_conversation" in text


def test_state_snapshot_tells_the_agent_what_not_to_ask(booking_ctx):
    state = booking_ctx.load_state()
    state.pickup_at = dt(4, 19)
    state.delivery_location = "Dubai Marina"
    snapshot = prompt_mod.render_state(state, now=FROZEN_NOW)

    assert "DO NOT ask for any of these again" in snapshot
    assert "Dubai Marina" in snapshot
    assert "when they will return it" in snapshot  # the one genuinely missing slot
    assert "where to deliver it" not in snapshot.split("Still needed")[1]


def test_state_snapshot_surfaces_the_active_booking(booking_ctx):
    from tests.test_booking import book

    reservation = book(booking_ctx)
    snapshot = prompt_mod.render_state(
        booking_ctx.load_state(), now=FROZEN_NOW, active_reservation=reservation
    )
    assert "DEMO-1042" in snapshot
    assert "refers to this" in snapshot


def test_state_snapshot_stops_selling_once_escalated(booking_ctx):
    state = booking_ctx.load_state()
    state.escalated = True
    state.escalation_reason = "accident"
    snapshot = prompt_mod.render_state(state, now=FROZEN_NOW)
    assert "ESCALATED" in snapshot
    assert "Do not sell" in snapshot


# --------------------------------------------------------------------------
# Extraction merge — the "never ask twice" guarantee
# --------------------------------------------------------------------------


def test_merge_adds_new_facts(engine):
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="x")
    merge(
        state,
        Extraction(
            pickup_at="2026-09-04T19:00:00+04:00",
            delivery_location="Dubai Marina",
            models=["G63"],
            color="black",
            budget_per_day=2500,
        ),
        engine.tz,
    )
    assert state.pickup_at == dt(4, 19)
    assert state.delivery_location == "Dubai Marina"
    assert state.vehicle_preferences.models == ["G63"]
    assert state.vehicle_preferences.budget_per_day == Decimal("2500")


def test_merge_never_clears_a_known_value(engine):
    """The whole point of the deterministic merge: an empty extraction cannot
    make the agent forget a date and ask for it again."""
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="x")
    state.pickup_at = dt(4, 19)
    state.delivery_location = "Dubai Marina"
    state.vehicle_preferences.color = "black"

    merge(state, Extraction(), engine.tz)

    assert state.pickup_at == dt(4, 19)
    assert state.delivery_location == "Dubai Marina"
    assert state.vehicle_preferences.color == "black"


def test_merge_updates_a_changed_value(engine):
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="x")
    state.pickup_at = dt(4, 19)
    merge(state, Extraction(pickup_at="2026-09-04T20:00:00+04:00"), engine.tz)
    assert state.pickup_at == dt(4, 20)


def test_merge_ignores_a_malformed_timestamp(engine):
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="x")
    state.pickup_at = dt(4, 19)
    merge(state, Extraction(pickup_at="friday evening"), engine.tz)
    assert state.pickup_at == dt(4, 19)


def test_merge_does_not_duplicate_preferences(engine):
    from rental_agent.domain.models import ConversationState

    state = ConversationState(conversation_id="c", customer_id="x")
    merge(state, Extraction(models=["G63"], categories=["luxury_suv"]), engine.tz)
    merge(state, Extraction(models=["G63"], categories=["luxury_suv"]), engine.tz)
    assert state.vehicle_preferences.models == ["G63"]
    assert state.vehicle_preferences.categories == [Category.LUXURY_SUV]


# --------------------------------------------------------------------------
# Turn mechanics
# --------------------------------------------------------------------------


def test_a_plain_reply_is_sent_and_stored(booking_ctx, settings):
    bot, _ = agent(script=[says("Happy to help — when do you need it?")], settings=settings)
    turn = bot.respond(booking_ctx, "I need a car")

    assert turn.reply.startswith("Happy to help")
    stored = booking_ctx.messages.for_conversation(booking_ctx.conversation_id)
    assert [m.direction for m in stored] == ["inbound", "outbound"]


def test_a_redelivered_whatsapp_message_is_not_answered_twice(booking_ctx, settings):
    bot, client = agent(script=[says("Sure, when for?")], settings=settings)

    first = bot.respond(booking_ctx, "hi", provider_message_id="wamid.1")
    second = bot.respond(booking_ctx, "hi", provider_message_id="wamid.1")

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.reply == ""
    assert len(client.requests) == 1  # the model was not asked a second time


def test_tool_calls_run_and_their_results_come_back(booking_ctx, settings):
    bot, client = agent(
        script=[
            calls(
                "search_available_vehicles",
                pickup_at=dt(4, 19).isoformat(),
                return_at=dt(7, 19).isoformat(),
                models=["G63"],
            ),
            says("I've got two G63s free for you."),
        ],
        settings=settings,
    )
    turn = bot.respond(booking_ctx, "black G63 this weekend")

    assert turn.tool_calls == ["search_available_vehicles"]
    assert turn.reply == "I've got two G63s free for you."

    followup = client.requests[-1]["messages"]
    tool_result = followup[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_search_available_vehicles"
    assert "veh_13" in tool_result["content"]


def test_a_failing_tool_is_reported_as_an_error_not_a_crash(booking_ctx, settings):
    bot, client = agent(
        script=[calls("get_vehicle_details", vehicle_id="veh_999"), says("Let me check again.")],
        settings=settings,
    )
    turn = bot.respond(booking_ctx, "tell me about that car")

    tool_result = client.requests[-1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "vehicle_not_found" in tool_result["content"]
    assert turn.reply == "Let me check again."


def test_vehicles_shown_are_remembered(booking_ctx, settings):
    bot, _ = agent(
        script=[
            calls(
                "search_available_vehicles",
                pickup_at=dt(4, 19).isoformat(),
                return_at=dt(7, 19).isoformat(),
                models=["G63"],
            ),
            says("Here are your options."),
        ],
        settings=settings,
    )
    bot.respond(booking_ctx, "black G63 this weekend")

    state = booking_ctx.load_state()
    assert "veh_13" in state.presented_vehicle_ids
    assert state.stage is Stage.OPTIONS_PRESENTED


def test_the_state_snapshot_is_sent_as_an_operator_message(booking_ctx, settings):
    bot, client = agent(script=[says("Sure.")], settings=settings)
    bot.respond(booking_ctx, "hello")

    systems = client.system_texts()
    assert len(systems) == 1
    assert systems[0].startswith("CURRENT STATE")
    # It must not be the first message, and the cached system prompt is separate.
    assert client.last_request["messages"][0]["role"] == "user"
    assert client.last_request["system"][0]["cache_control"]


def test_history_is_replayed_across_turns(booking_ctx, settings):
    bot, client = agent(script=[says("When for?"), says("Got it.")], settings=settings)
    bot.respond(booking_ctx, "I want a G63")
    bot.respond(booking_ctx, "Friday")

    roles = [m["role"] for m in client.last_request["messages"]]
    assert roles == ["user", "assistant", "user", "system"]


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------


def test_an_accident_escalates_even_if_the_model_never_calls_the_tool(booking_ctx, settings):
    """Escalation is executed in code from the extraction signal. A model that
    forgets to call the tool cannot cause a missed incident."""
    bot, _ = agent(
        script=[says("Here are three SUVs."), says("Are you and your passengers safe?")],
        extractions=[Extraction(intent="report_problem", escalation_signal="accident")],
        settings=settings,
    )
    bot.respond(booking_ctx, "I crashed the car on Sheikh Zayed Road")

    state = booking_ctx.load_state()
    assert state.escalated is True
    assert state.escalation_reason == "accident"
    assert state.stage is Stage.ESCALATED
    assert len(booking_ctx.escalations.open_escalations()) == 1


def test_a_reply_written_before_the_accident_was_known_is_thrown_away(booking_ctx, settings):
    """Extraction runs alongside the first conversation call, so a reply can be
    composed before anyone knows the customer has crashed. That reply is
    discarded and the turn answered again against the escalated state.

    It costs one extra model call on the rarest and highest-stakes turn there
    is, which is the right side of that trade."""
    bot, client = agent(
        script=[says("Great — three SUVs for those dates!"), says("Are you safe?")],
        extractions=[Extraction(escalation_signal="accident")],
        settings=settings,
    )
    turn = bot.respond(booking_ctx, "i've just crashed the car")

    assert turn.reply == "Are you safe?", "the pre-escalation reply must not reach the customer"
    assert "ESCALATED" in client.system_texts()[-1], "the second call knew"
    assert len(client.requests) == 2


def test_a_turn_with_no_escalation_is_not_re_run(booking_ctx, settings):
    """The guard must fire on the transition only. Re-running every turn would
    double the cost of the thing this parallelism exists to make cheaper."""
    bot, client = agent(
        script=[says("Sure — when do you need it?")],
        extractions=[Extraction(intent="new_rental")],
        settings=settings,
    )
    bot.respond(booking_ctx, "i need a car")
    assert len(client.requests) == 1


def test_an_already_escalated_conversation_is_not_re_run(booking_ctx, settings):
    """A second accident signal on a conversation that is already escalated is
    not a transition, and must not restart the turn."""
    bot, client = agent(
        script=[says("Are you safe?"), says("A colleague is on the way."),
                says("Still with you.")],
        extractions=[Extraction(escalation_signal="accident"),
                     Extraction(escalation_signal="accident")],
        settings=settings,
    )
    bot.respond(booking_ctx, "i crashed")
    before = len(client.requests)
    bot.respond(booking_ctx, "the police are here now")

    assert len(client.requests) - before == 1


def test_a_low_severity_signal_is_left_to_the_agent(booking_ctx, settings):
    """Abuse and discount pressure are judgement calls; only safety-critical
    signals are forced in code."""
    bot, _ = agent(
        script=[says("I understand — let me see what I can do.")],
        extractions=[Extraction(escalation_signal="abusive_language")],
        settings=settings,
    )
    bot.respond(booking_ctx, "this is ridiculous")
    assert booking_ctx.load_state().escalated is False


def test_the_escalated_state_reaches_the_next_turn(booking_ctx, settings):
    bot, client = agent(
        # Three: the first turn is answered twice, because the accident only
        # became known once extraction landed alongside the first call.
        script=[says("Three SUVs available."), says("Are you safe?"),
                says("A colleague is taking over.")],
        extractions=[Extraction(escalation_signal="accident"), Extraction()],
        settings=settings,
    )
    bot.respond(booking_ctx, "I had an accident")
    bot.respond(booking_ctx, "ok")
    assert "ESCALATED" in client.system_texts()[-1]


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_a_refusal_never_leaves_the_customer_with_silence(booking_ctx, settings):
    bot, _ = agent(script=[refuses()], settings=settings)
    turn = bot.respond(booking_ctx, "something the model declines")

    assert turn.refusal is True
    assert turn.reply == SAFE_FALLBACK_REPLY
    assert turn.escalated is True
    assert booking_ctx.load_state().escalated is True


def test_a_runaway_tool_loop_is_stopped_and_handed_over(booking_ctx, settings):
    bot, _ = agent(script=[calls("get_customer") for _ in range(10)], settings=settings)
    turn = bot.respond(booking_ctx, "hello")

    assert turn.iterations == settings.max_tool_iterations
    assert turn.reply == SAFE_FALLBACK_REPLY
    assert turn.escalated is True


def test_fallbacks_are_requested_when_the_account_supports_them(booking_ctx, settings):
    bot, client = agent(script=[says("Hi")], settings=settings)
    bot.respond(booking_ctx, "hello")
    assert client.beta_calls == 1


def test_a_missing_fallbacks_beta_degrades_instead_of_failing(booking_ctx, settings):
    bot, client = agent(script=[says("Hi")], settings=settings, supports_fallbacks=False)
    turn = bot.respond(booking_ctx, "hello")

    assert turn.reply == "Hi"
    assert client.beta_calls == 0
    assert bot._fallbacks_supported is False


def test_asked_slots_are_only_recorded_for_actual_questions(booking_ctx, settings):
    bot, _ = agent(script=[says("Sure thing.")], settings=settings)
    bot.respond(booking_ctx, "hi")
    assert booking_ctx.load_state().asked_slots == []

    bot.client.script.append(says("When do you need the car?"))
    bot.respond(booking_ctx, "a car please")
    assert "pickup_at" in booking_ctx.load_state().asked_slots


def test_asking_again_because_nobody_answered_is_not_a_mistake(booking_ctx, settings):
    """The customer ignoring a question is not the agent's failure. Asking a
    second time is how a salesperson gets an answer, so it must leave no trace
    the evaluator can mistake for one."""
    bot, _ = agent(script=[says("When do you need the car?")], settings=settings)
    bot.respond(booking_ctx, "a car please")

    bot.client.script.append(says("Sure — when do you need it collected?"))
    bot.respond(booking_ctx, "just book something")

    state = booking_ctx.load_state()
    assert state.asked_slots.count("pickup_at") == 2
    assert state.redundant_asks == []


def test_asking_for_something_already_known_is_recorded(booking_ctx, settings):
    """This is the real repeated-question failure, and it is only visible at ask
    time — by evaluation time the value is in state either way."""
    state = booking_ctx.load_state()
    state.pickup_at = dt(4, 19)
    booking_ctx.save_state(state)

    bot, _ = agent(script=[says("When do you need the car?")], settings=settings)
    bot.respond(booking_ctx, "book it")

    assert booking_ctx.load_state().redundant_asks == ["pickup_at"]


def test_the_extraction_prompt_lists_the_category_vocabulary():
    """Without the allowed values in the prompt, the extractor returns an empty
    category list for "I need an SUV" — and the agent then thrashes through
    repeated searches because it has no constraint to work with."""
    from rental_agent.agent.extraction import EXTRACTION_PROMPT
    from rental_agent.domain.enums import Category

    for category in Category:
        assert category.value in EXTRACTION_PROMPT


# --------------------------------------------------------------------------
# Latency — the shape of a turn
#
# The client's brief asks for 2-5 second replies. What governs that is not how
# fast any one call is but how many of them happen in sequence, so what these
# tests pin is the count and the ordering, which is the part we control.
# --------------------------------------------------------------------------


def test_extraction_does_not_add_a_round_trip(booking_ctx, settings):
    """Extraction runs alongside the first conversation call rather than before
    it. A turn with no tools is one conversation call, not two in sequence."""
    bot, client = agent(
        script=[says("Sure — when do you need it?")],
        extractions=[Extraction(intent="new_rental")],
        settings=settings,
    )
    bot.respond(booking_ctx, "i need a car")

    assert len(client.requests) == 1


def test_parallel_and_sequential_extraction_reach_the_same_state(booking_ctx, settings):
    """Concurrency must be an optimisation, not a behaviour change. If the two
    modes disagreed, the fast path would be quietly answering differently."""
    from dataclasses import replace

    from rental_agent.domain.enums import Category

    extracted = Extraction(
        intent="new_rental",
        pickup_at="2026-09-04T19:00:00+04:00",
        delivery_location="Dubai Marina",
        categories=[Category.LUXURY_SUV],
    )

    def state_after(parallel: bool):
        ctx = booking_ctx
        blank = ctx.load_state()
        blank.pickup_at = None
        blank.delivery_location = None
        blank.vehicle_preferences.categories = []
        ctx.save_state(blank)

        bot, _ = agent(
            script=[says("Got it.")],
            extractions=[extracted],
            settings=replace(settings, parallel_extraction=parallel),
        )
        bot.respond(ctx, "black g wagon friday 7pm in marina")
        return ctx.load_state()

    parallel = state_after(True)
    sequential = state_after(False)

    assert parallel.pickup_at == sequential.pickup_at
    assert parallel.delivery_location == sequential.delivery_location
    assert parallel.vehicle_preferences.categories == sequential.vehicle_preferences.categories


def test_the_state_block_carries_the_quote_not_just_its_id(booking_ctx, settings):
    """Given only a reference, the agent cannot use the quote it already has —
    on "book it" it searches and re-quotes from scratch. That is three extra
    round trips and a second chance to produce a total that differs from the one
    the customer was shown."""
    from rental_agent.tools.registry import execute_tool

    quote = execute_tool(
        booking_ctx,
        "create_demo_quote",
        {
            "vehicle_id": "veh_13",
            "pickup_at": dt(4, 19).isoformat(),
            "return_at": dt(7, 19).isoformat(),
            "delivery_location": "Dubai Marina",
        },
    )
    state = booking_ctx.load_state()
    state.quote_id = quote["quote_id"]
    booking_ctx.save_state(state)

    bot, client = agent(script=[says("Booking that now.")], settings=settings)
    bot.respond(booking_ctx, "ok book it")

    block = client.system_texts()[-1]
    assert quote["quote_id"] in block
    assert str(quote["total_charge"]) in block, "the figure, not just the reference"
    assert "do not search or re-quote" in block


# --------------------------------------------------------------------------
# Provider outages
#
# Before this, an unreachable model meant the customer got "send that again"
# and nothing else happened — no retry, nobody told. An hour-long outage lost
# live customers in silence, and the company never found out.
# --------------------------------------------------------------------------


def test_one_bad_turn_gets_an_apology_not_a_handover(booking_ctx, settings):
    """Outages are usually brief. Pulling a person in for every blip would
    train the owner to ignore the alerts."""
    from rental_agent.agent.loop import PROVIDER_BUSY_REPLY
    from rental_agent.agent.providers.errors import ProviderUnavailable

    bot, _ = agent(script=[ProviderUnavailable("503 UNAVAILABLE")], settings=settings)
    turn = bot.respond(booking_ctx, "do you have a g wagon")

    assert turn.reply == PROVIDER_BUSY_REPLY
    assert turn.escalated is False
    assert booking_ctx.load_state().consecutive_provider_failures == 1


def test_a_second_failure_in_a_row_pulls_a_person_in(booking_ctx, settings):
    """Repeating the apology is a slower way of losing the customer."""
    from rental_agent.agent.loop import PROVIDER_DOWN_REPLY
    from rental_agent.agent.providers.errors import ProviderUnavailable

    bot, _ = agent(
        script=[ProviderUnavailable("503"), ProviderUnavailable("503")], settings=settings
    )
    bot.respond(booking_ctx, "do you have a g wagon")
    turn = bot.respond(booking_ctx, "hello?")

    assert turn.reply == PROVIDER_DOWN_REPLY
    assert turn.escalated is True

    state = booking_ctx.load_state()
    assert state.escalated is True
    assert state.escalation_reason == "repeated_agent_failure"
    assert len(booking_ctx.escalations.open_escalations()) == 1


def test_the_staff_note_carries_what_the_customer_was_asking(booking_ctx, settings):
    """A colleague picking this up needs to know what the customer wanted, not
    just that something broke."""
    from rental_agent.agent.providers.errors import ProviderUnavailable

    bot, _ = agent(
        script=[ProviderUnavailable("503"), ProviderUnavailable("503")], settings=settings
    )
    bot.respond(booking_ctx, "can i get a black g63 for friday")
    bot.respond(booking_ctx, "can i get a black g63 for friday")

    [escalation] = booking_ctx.escalations.open_escalations()
    assert "black g63" in (escalation.detail or "")


def test_a_turn_that_gets_through_clears_the_count(booking_ctx, settings):
    """Otherwise an outage next week inherits a count from this one and hands
    over on its first blip."""
    from rental_agent.agent.providers.errors import ProviderUnavailable

    bot, _ = agent(
        script=[ProviderUnavailable("503"), says("Sure — when do you need it?")],
        settings=settings,
    )
    bot.respond(booking_ctx, "hello")
    assert booking_ctx.load_state().consecutive_provider_failures == 1

    bot.respond(booking_ctx, "hello again")
    assert booking_ctx.load_state().consecutive_provider_failures == 0
