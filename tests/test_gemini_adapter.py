"""The Gemini adapter.

Translation between the agent's message shape and Gemini's is pure, so all of it
is verified here without a key or a network call. The end-to-end test drives the
real `Agent` through the real adapter with only the SDK's transport stubbed —
which is what proves the two actually compose.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rental_agent.agent.loop import Agent
from rental_agent.agent.providers import gemini
from rental_agent.agent.providers.gemini import (
    GeminiClient,
    Response,
    TextBlock,
    ToolUseBlock,
    decode_call_id,
    encode_call_id,
    from_response,
    to_contents,
    to_function_declarations,
    to_system_instruction,
)
from rental_agent.agent.schemas import TOOLS
from rental_agent.agent.settings import AgentSettings, PROVIDER_DEFAULT_MODELS
from tests.conftest import dt


# --------------------------------------------------------------------------
# System instruction and tools
# --------------------------------------------------------------------------


def test_system_blocks_flatten_to_one_instruction():
    result = to_system_instruction(
        [{"type": "text", "text": "first", "cache_control": {"type": "ephemeral"}}]
    )
    assert result == "first"


def test_a_plain_string_system_prompt_passes_through():
    assert to_system_instruction("be helpful") == "be helpful"


def test_every_tool_converts():
    declarations = to_function_declarations(TOOLS)
    assert len(declarations) == len(TOOLS)
    assert {d.name for d in declarations} == {t["name"] for t in TOOLS}


def test_a_no_argument_tool_omits_parameters():
    """Gemini rejects an empty parameter object — it must be absent entirely."""
    declarations = {d.name: d for d in to_function_declarations(TOOLS)}
    assert declarations["get_customer"].parameters_json_schema is None
    assert declarations["get_active_reservation"].parameters_json_schema is None


def test_a_tool_with_arguments_keeps_its_schema():
    declarations = {d.name: d for d in to_function_declarations(TOOLS)}
    schema = declarations["search_available_vehicles"].parameters_json_schema
    assert schema["properties"]["pickup_at"]["type"] == "string"
    assert "pickup_at" in schema["required"]


# --------------------------------------------------------------------------
# Message conversion
# --------------------------------------------------------------------------


def test_roles_map_to_gemini_roles():
    contents = to_contents(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert [c.role for c in contents] == ["user", "model"]
    assert contents[0].parts[0].text == "hi"


def test_a_system_message_becomes_a_marked_user_part():
    """Gemini has no mid-conversation system role, so operator context has to be
    distinguishable from something the customer said."""
    contents = to_contents(
        [{"role": "user", "content": "hi"}, {"role": "system", "content": "CURRENT STATE"}]
    )
    assert len(contents) == 1  # merged into the preceding user turn
    marked = contents[0].parts[-1].text
    assert marked.startswith(gemini.STATE_MARKER)
    assert "CURRENT STATE" in marked


def test_consecutive_same_role_turns_merge():
    contents = to_contents(
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "reply"},
        ]
    )
    assert [c.role for c in contents] == ["user", "model"]
    assert len(contents[0].parts) == 2


def test_empty_content_is_dropped():
    assert to_contents([{"role": "assistant", "content": ""}]) == []


def test_a_tool_call_becomes_a_function_call_part():
    contents = to_contents(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [ToolUseBlock(name="get_customer", input={}, id="fc:0:get_customer")],
            },
        ]
    )
    call = contents[-1].parts[0].function_call
    assert call.name == "get_customer"


def test_a_tool_result_recovers_the_function_name_from_the_id():
    """Gemini matches a response to its call by name, and gives no ids of its
    own — so the name has to survive the round trip inside the synthetic id."""
    contents = to_contents(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "fc:0:search_available_vehicles",
                        "content": '{"count": 2}',
                        "is_error": False,
                    }
                ],
            },
        ]
    )
    response_part = contents[0].parts[-1].function_response
    assert response_part.name == "search_available_vehicles"
    assert response_part.response == {"count": 2}


def test_a_non_json_tool_result_is_still_delivered():
    contents = to_contents(
        [
            {"role": "user", "content": "hi"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "fc:0:x", "content": "not json"}
                ],
            },
        ]
    )
    assert contents[0].parts[-1].function_response.response == {"result": "not json"}


def test_call_id_round_trip():
    assert decode_call_id(encode_call_id("modify_demo_reservation", 3)) == "modify_demo_reservation"


def test_an_unencoded_id_degrades_to_itself():
    assert decode_call_id("weird") == "weird"


# --------------------------------------------------------------------------
# Response conversion
# --------------------------------------------------------------------------


def _candidate(parts, finish="STOP"):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=parts),
                finish_reason=SimpleNamespace(name=finish),
            )
        ]
    )


def _part(text=None, call=None, thought=None):
    return SimpleNamespace(text=text, function_call=call, thought=thought)


def test_text_becomes_a_text_block():
    result = from_response(_candidate([_part(text="Here you go")]))
    assert result.content == [TextBlock(text="Here you go")]
    assert result.stop_reason == "end_turn"


def test_a_function_call_becomes_a_tool_use_block():
    call = SimpleNamespace(name="get_customer", args={"a": 1}, id=None)
    result = from_response(_candidate([_part(call=call)]))
    assert result.stop_reason == "tool_use"
    assert result.content[0].name == "get_customer"
    assert result.content[0].input == {"a": 1}
    assert decode_call_id(result.content[0].id) == "get_customer"


def test_thinking_parts_are_not_shown_to_the_customer():
    result = from_response(_candidate([_part(text="internal", thought=True), _part(text="visible")]))
    assert result.content == [TextBlock(text="visible")]


@pytest.mark.parametrize(
    "finish, expected",
    [
        ("STOP", "end_turn"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "refusal"),
        ("PROHIBITED_CONTENT", "refusal"),
        ("OTHER", "end_turn"),
    ],
)
def test_finish_reasons_map_onto_stop_reasons(finish, expected):
    assert from_response(_candidate([_part(text="x")], finish=finish)).stop_reason == expected


def test_a_blocked_response_with_no_candidates_is_a_refusal():
    """The agent turns a refusal into a safe reply plus an escalation, so this
    must not be mistaken for an empty but successful turn."""
    assert from_response(SimpleNamespace(candidates=[])).stop_reason == "refusal"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_a_model_name_is_resolved_per_provider():
    """The loop passes settings.resolved_model() — sending a Claude model name
    to Gemini would fail every request."""
    assert AgentSettings(provider="gemini", model=None).resolved_model().startswith("gemini")
    assert AgentSettings(provider="anthropic", model=None).resolved_model().startswith("claude")


def test_an_explicit_model_overrides_the_provider_default():
    assert AgentSettings(provider="gemini", model="gemini-2.5-pro").resolved_model() == "gemini-2.5-pro"


def test_extraction_runs_on_a_separate_model_by_default():
    """Rate limits are per-model, so two requests per turn should land in two
    quota buckets rather than exhausting one."""
    settings = AgentSettings(provider="gemini", model="gemini-x", extraction_model=None)
    assert settings.resolved_extraction_model() != "gemini-x"
    assert settings.resolved_extraction_model().startswith("gemini")


def test_an_explicit_extraction_model_wins():
    settings = AgentSettings(provider="gemini", model="a", extraction_model="b")
    assert settings.resolved_extraction_model() == "b"


def test_gemini_declares_no_fallback_support():
    """Server-side refusal fallbacks are Anthropic-only; the agent must not
    waste a request discovering that every turn."""
    assert GeminiClient.supports_fallbacks is False


# --------------------------------------------------------------------------
# End to end through the real adapter
# --------------------------------------------------------------------------


class StubModels:
    """Stands in for `client.raw.models`, replaying scripted Gemini responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.script.pop(0)


@pytest.fixture
def gemini_client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    return GeminiClient(model="gemini-test")


def test_a_full_turn_runs_through_the_adapter(booking_ctx, gemini_client):
    """The agent, the tool layer and the adapter composing for real — only the
    SDK's transport is stubbed."""
    search_call = SimpleNamespace(
        name="search_available_vehicles",
        args={
            "pickup_at": dt(4, 19).isoformat(),
            "return_at": dt(7, 19).isoformat(),
            "models": ["G63"],
        },
        id=None,
    )
    gemini_client.raw = SimpleNamespace(
        models=StubModels(
            [
                _candidate([_part(call=search_call)]),
                _candidate([_part(text="I have two G63s free for you.")]),
            ]
        )
    )

    bot = Agent(
        gemini_client,
        AgentSettings(provider="gemini", model="gemini-test", extraction_enabled=False),
    )
    turn = bot.respond(booking_ctx, "black G63 this weekend")

    assert turn.reply == "I have two G63s free for you."
    assert turn.tool_calls == ["search_available_vehicles"]
    assert "veh_13" in booking_ctx.load_state().presented_vehicle_ids

    # The second request must carry the tool result back as a function response.
    second = gemini_client.raw.models.calls[1]["contents"]
    function_response = second[-1].parts[-1].function_response
    assert function_response.name == "search_available_vehicles"
    assert function_response.response["count"] >= 1


def test_the_system_prompt_and_tools_reach_gemini(booking_ctx, gemini_client):
    gemini_client.raw = SimpleNamespace(
        models=StubModels([_candidate([_part(text="Hi there")])])
    )
    bot = Agent(
        gemini_client,
        AgentSettings(provider="gemini", model="gemini-test", extraction_enabled=False),
    )
    bot.respond(booking_ctx, "hello")

    config = gemini_client.raw.models.calls[0]["config"]
    assert "Sandline Rentals" in config.system_instruction
    assert len(config.tools[0].function_declarations) == len(TOOLS)
    # The SDK must not run its own tool loop underneath ours.
    assert config.automatic_function_calling.disable is True


def test_a_gemini_safety_block_escalates(booking_ctx, gemini_client):
    gemini_client.raw = SimpleNamespace(
        models=StubModels([_candidate([_part(text="")], finish="PROHIBITED_CONTENT")])
    )
    bot = Agent(
        gemini_client,
        AgentSettings(provider="gemini", model="gemini-test", extraction_enabled=False),
    )
    turn = bot.respond(booking_ctx, "something blocked")

    assert turn.refusal is True
    assert turn.escalated is True
    assert booking_ctx.load_state().escalated is True


# --------------------------------------------------------------------------
# Thought signatures
# --------------------------------------------------------------------------


def test_a_function_call_signature_survives_the_round_trip():
    """Gemini 3.x rejects a replayed function call whose thought_signature is
    missing. It must be carried from the response into the next request, never
    dropped and never invented."""
    call = SimpleNamespace(name="get_customer", args={}, id=None)
    part = SimpleNamespace(text=None, function_call=call, thought=None)
    part.thought_signature = b"sig-abc"

    block = from_response(
        SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[part]),
                    finish_reason=SimpleNamespace(name="STOP"),
                )
            ]
        )
    ).content[0]
    assert block.signature == b"sig-abc"

    contents = to_contents(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [block]}]
    )
    assert contents[-1].parts[0].thought_signature == b"sig-abc"


def test_a_missing_signature_is_not_invented():
    call = SimpleNamespace(name="get_customer", args={}, id=None)
    part = SimpleNamespace(text=None, function_call=call, thought=None, thought_signature=None)
    block = from_response(
        SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[part]),
                    finish_reason=SimpleNamespace(name="STOP"),
                )
            ]
        )
    ).content[0]
    assert block.signature is None


def test_the_servers_retry_delay_is_honoured():
    """A per-minute quota does not clear on a 2s backoff — when Google says how
    long to wait, waiting that long is the difference between recovering and
    burning the retries."""
    from rental_agent.agent.providers.gemini import _server_retry_delay

    body = "429 RESOURCE_EXHAUSTED {'details': [{'retryDelay': '31s'}]}"
    assert _server_retry_delay(body) == 31.0
    assert _server_retry_delay("503 UNAVAILABLE high demand") is None


# --------------------------------------------------------------------------
# Free-tier quota handling
# --------------------------------------------------------------------------


def test_a_daily_quota_is_told_apart_from_a_rate_limit():
    """They need opposite responses: wait out a per-minute limit, switch models
    on a per-day one. The advertised retryDelay is seconds in both cases."""
    from rental_agent.agent.providers.gemini import is_daily_quota_exhausted

    daily = "429 quotaId: 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'"
    per_minute = "429 quotaId: 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'"
    assert is_daily_quota_exhausted(daily) is True
    assert is_daily_quota_exhausted(per_minute) is False


def test_an_exhausted_daily_quota_moves_to_the_next_model(gemini_client):
    """The demo keeps going on another model's budget instead of stopping dead
    mid-conversation."""
    calls = []

    def generate_content(*, model, contents, config):
        calls.append(model)
        if model in ("gemini-test", "gemini-3.7-flash"):
            raise RuntimeError("429 quotaId: 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'")
        return _candidate([_part(text="served by the fallback")])

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    result = gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])

    assert result.content[0].text == "served by the fallback"
    assert len(calls) > 1
    assert gemini_client.active_model == calls[-1]


def test_every_model_exhausted_raises_provider_unavailable(gemini_client):
    from rental_agent.agent.providers.errors import ProviderUnavailable

    def generate_content(*, model, contents, config):
        raise RuntimeError("429 quotaId: 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'")

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with pytest.raises(ProviderUnavailable):
        gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])


def test_a_non_retryable_error_is_not_masked_by_failover(gemini_client):
    """A bad request must surface immediately, not be retried across five
    models and reported as a quota problem."""
    def generate_content(*, model, contents, config):
        raise RuntimeError("400 INVALID_ARGUMENT bad schema")

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])


def test_a_busy_model_is_swapped_before_it_is_retried(gemini_client):
    """A 503 means *that* model is busy, so trying another one beats backing off
    against the same one. Waiting is for when the whole fleet is unavailable."""
    calls = []

    def generate_content(*, model, contents, config):
        calls.append(model)
        if len(calls) == 1:
            raise RuntimeError("503 UNAVAILABLE high demand")
        return _candidate([_part(text="second model answered")])

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    result = gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])

    assert result.content[0].text == "second model answered"
    assert calls[0] != calls[1]      # switched rather than retried
    assert len(calls) == 2           # and did so without sleeping first


# --------------------------------------------------------------------------
# Latency controls
# --------------------------------------------------------------------------


def test_a_thinking_level_is_sent_by_default(gemini_client):
    """Internal reasoning dominates turn latency — measured 15.6s vs 9.4s for the
    same request on gemini-3.5-flash. A rental conversation does not need deep
    reasoning; the engine does the thinking that matters."""
    seen = {}

    def generate_content(*, model, contents, config):
        seen["config"] = config
        return _candidate([_part(text="ok")])

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])
    assert seen["config"].thinking_config is not None


def test_a_model_that_rejects_the_thinking_level_still_answers(gemini_client):
    """Not every model accepts the knob. One 400 must not fail every request for
    the rest of the process."""
    attempts = []

    def generate_content(*, model, contents, config):
        attempts.append(config.thinking_config)
        if config.thinking_config is not None:
            raise RuntimeError("400 INVALID_ARGUMENT: thinking_level is not supported")
        return _candidate([_part(text="answered without it")])

    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    result = gemini_client.messages.create(messages=[{"role": "user", "content": "hi"}])

    assert result.content[0].text == "answered without it"
    assert attempts[0] is not None and attempts[-1] is None
    assert gemini_client.thinking_level is None   # remembered, not retried each time


def test_short_remaining_budget_never_sends_an_invalid_gemini_deadline(gemini_client):
    from rental_agent.agent.providers.budget import turn_budget
    from rental_agent.agent.providers.errors import ProviderUnavailable
    gemini_client.raw = SimpleNamespace(models=StubModels([]))
    with turn_budget(5), pytest.raises(ProviderUnavailable, match='deadline'):
        gemini_client.messages.create(messages=[{'role': 'user', 'content': 'hi'}])
    assert not gemini_client.raw.models.calls


def test_gemini_timeout_is_bounded_and_sdk_retries_are_disabled(gemini_client):
    gemini_client.raw = SimpleNamespace(models=StubModels([_candidate([_part(text='ok')])]))
    gemini_client.messages.create(messages=[{'role': 'user', 'content': 'hi'}])
    config = gemini_client.raw.models.calls[0]['config']
    assert 10000 <= config.http_options.timeout <= 15000
    assert config.http_options.retry_options.attempts == 1


def test_slow_failure_can_fail_over_with_remaining_turn_time(gemini_client, monkeypatch):
    from rental_agent.agent.providers.budget import turn_budget
    clock = [100.0]
    monkeypatch.setattr(gemini.time, 'monotonic', lambda: clock[0])
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            clock[0] += 9.2
            raise RuntimeError('503 UNAVAILABLE high demand')
        return _candidate([_part(text='Prices recovered')])
    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with turn_budget(30):
        result = gemini_client.messages.create(messages=[{'role':'user','content':'What are their prices?'}])
    assert result.content[0].text == 'Prices recovered'
    assert len(calls) == 2
    assert calls[0]['model'] != calls[1]['model']
    assert calls[1]['config'].http_options.timeout <= 15000


def test_failover_does_not_extend_total_turn_deadline(gemini_client, monkeypatch):
    from rental_agent.agent.providers.budget import turn_budget
    from rental_agent.agent.providers.errors import ProviderUnavailable
    clock = [100.0]
    monkeypatch.setattr(gemini.time, 'monotonic', lambda: clock[0])
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs)
        clock[0] += 15
        raise TimeoutError('request timed out')
    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with turn_budget(30), pytest.raises(ProviderUnavailable):
        gemini_client.messages.create(messages=[{'role':'user','content':'prices'}])
    assert len(calls) == 2
    assert clock[0] == 130


def test_retry_after_is_not_shortened_by_backoff_cap(gemini_client, monkeypatch):
    from rental_agent.agent.providers.budget import turn_budget
    clock = [100.0]
    monkeypatch.setattr(gemini.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(gemini.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    gemini_client.fallback_models = []
    gemini_client.max_backoff = 1
    calls = []
    def generate_content(**kwargs):
        calls.append(clock[0])
        if len(calls) == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '31s'}")
        return _candidate([_part(text='Recovered')])
    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with turn_budget(60):
        gemini_client.messages.create(messages=[{'role':'user','content':'prices'}])
    assert calls == [100, 131]


def test_provider_logs_and_errors_do_not_store_response_bodies(gemini_client, caplog):
    from rental_agent.agent.providers.errors import ProviderUnavailable
    gemini_client.max_retries = 0
    gemini_client.fallback_models = []
    def generate_content(**kwargs):
        raise RuntimeError('503 UNAVAILABLE private-customer-data secret-api-key')
    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with pytest.raises(ProviderUnavailable) as caught:
        gemini_client.messages.create(messages=[{'role':'user','content':'prices'}])
    assert str(caught.value) == 'overloaded (HTTP 503)'
    assert 'private-customer-data' not in caplog.text
    assert 'secret-api-key' not in caplog.text


def test_empty_network_timeout_still_fails_over(gemini_client):
    import httpx
    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs['model'])
        if len(calls) == 1:
            raise httpx.ReadTimeout('')
        return _candidate([_part(text='Recovered')])
    gemini_client.raw = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    response = gemini_client.messages.create(messages=[{'role':'user','content':'prices'}])
    assert response.content[0].text == 'Recovered'
    assert len(calls) == 2


def test_transient_failures_retry_automatically_after_failover(gemini_client, monkeypatch):
    from rental_agent.agent.providers.budget import turn_budget
    clock=[100.0];attempts=[];pauses=[]
    monkeypatch.setattr(gemini.time,'monotonic',lambda:clock[0])
    def sleep(seconds):
        pauses.append(seconds);clock[0]+=seconds
    monkeypatch.setattr(gemini.time,'sleep',sleep)
    gemini_client.fallback_models=['gemini-fallback-test']
    def generate_content(**kwargs):
        attempts.append(kwargs['model'])
        if len(attempts)==1: raise RuntimeError('503 UNAVAILABLE high demand')
        if len(attempts)==2: raise RuntimeError('504 DEADLINE_EXCEEDED')
        return _candidate([_part(text='Recovered automatically')])
    gemini_client.raw=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    with turn_budget(30):
        result=gemini_client.messages.create(messages=[{'role':'user','content':'Show the cars'}])
    assert result.content[0].text=='Recovered automatically'
    assert attempts==['gemini-test','gemini-fallback-test','gemini-test']
    assert pauses==[2.0]
