"""Counts describe the displayed shortlist, not how many inventory rows matched."""
from rental_agent.agent.options import correct_counts
from rental_agent.agent.loop import Agent
from rental_agent.agent.settings import AgentSettings
from tests.fake_anthropic import FakeClient, says
import pytest

TWO = '* Mercedes-AMG G63\n* BMW X7\n'


@pytest.mark.parametrize('intro,expected', [
    ('I have these three available for your dates:', 'I have these two available for your dates:'),
    ('Here are 3 options:', 'Here are 2 options:'),
    ('I found four available vehicles:', 'I found two available vehicles:'),
    ('Here is one option:', 'Here are two options:'),
    ('Here are ٣ options:', 'Here are 2 options:'),
])
def test_intro_matches_the_visible_rows(engine, intro, expected):
    assert correct_counts(intro + '\n\n' + TWO, engine.list_fleet()) == expected + '\n\n' + TWO


def test_single_option_has_singular_grammar(engine):
    assert correct_counts('Here are three cars:\n* BMW X7', engine.list_fleet()) == 'Here is one car:\n* BMW X7'
    assert correct_counts('I have these three available:\n* BMW X7', engine.list_fleet()) == 'I have this one available:\n* BMW X7'


def test_duplicate_inventory_units_do_not_inflate_visible_count(engine):
    # The fixture contains two G63 inventory rows, but this reply lists one.
    reply = 'Here are three models:\n* Mercedes-AMG G63\n* BMW X7'
    assert correct_counts(reply, engine.list_fleet()).startswith('Here are two models:')


def test_prices_dates_and_passenger_capacity_are_unchanged(engine):
    body = '\n\n- Mercedes-AMG G63: AED 2,799, 5 seats\n- BMW X7: AED 2,999, 7 seats\n'
    reply = 'For 6 to 10 September, I have these three options:' + body
    assert correct_counts(reply, engine.list_fleet()) == reply.replace('these three', 'these two')


@pytest.mark.parametrize('reply', [
    'Here are two options:\n' + TWO,
    'We have three vehicles in the fleet.',
    'Bring these three documents:\n* Passport\n* Driving licence',
    'Your rental lasts three days. The G63 carries five passengers.',
])
def test_correct_counts_and_unrelated_numbers_are_preserved(engine, reply):
    assert correct_counts(reply, engine.list_fleet()) == reply


def test_only_the_shortlist_announcement_changes(engine):
    reply = 'We have ten cars in the fleet. Here are three options:\n' + TWO
    assert correct_counts(reply, engine.list_fleet()) == reply.replace('three options', 'two options')


def test_numbered_list_and_wrapped_details(engine):
    body = '\n1. Mercedes-AMG G63\n   Five passengers\n\n2. BMW X7\n   Seven passengers\n'
    assert correct_counts('Here are three options:' + body, engine.list_fleet()) == 'Here are two options:' + body


def test_agent_repairs_count_without_an_extra_model_request(booking_ctx):
    client = FakeClient(script=[says('Here are three options to consider:\n' + TWO)])
    turn = Agent(client, AgentSettings(extraction_enabled=False)).respond(booking_ctx, 'Show me some SUVs')
    assert turn.reply.startswith('Here are two options')
    assert len(client.requests) == 1
    stored = booking_ctx.messages.for_conversation(booking_ctx.conversation_id)[-1]
    assert stored.content == turn.reply
