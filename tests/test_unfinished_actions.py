from rental_agent.agent.loop import Agent
from rental_agent.agent.settings import AgentSettings
from tests.fake_anthropic import FakeClient, says


def test_unperformed_promise_is_replaced_with_a_concrete_question(booking_ctx):
    model=FakeClient(script=[says("I'll send the payment link in a moment."),
                            says('Which booking would you like to pay for?')])
    turn=Agent(model,AgentSettings(extraction_enabled=False)).respond(booking_ctx,'send a payment link')
    assert turn.reply=='Which booking would you like to pay for?'
    assert len(model.requests)==2


def test_repeated_empty_promise_cannot_escape(booking_ctx):
    model=FakeClient(script=[says("Let me check availability for you."),
                            says("I'll send that shortly.")])
    turn=Agent(model,AgentSettings(extraction_enabled=False)).respond(booking_ctx,'find me a car')
    assert 'shortly' not in turn.reply
    assert 'could not complete' in turn.reply


def test_conditional_handoff_explanation_does_not_promise_background_execution():
    from rental_agent.agent.actions import unfinished,wants_payment
    assert not unfinished("Once a colleague confirms the car, I will send a link.")
    for message in ["I don't want to pay", 'what is a payment link?', 'send a deposit payment link']:
        assert not wants_payment(message)


def test_mixed_payment_and_rental_changes_use_the_normal_conversation_path():
    from rental_agent.agent.actions import wants_payment
    for message in ['Send a payment link for 5 days', 'I want to pay but change the car',
                    'send me a payment link for the Mercedes', 'عايز ادفع بس غير العربية']:
        assert not wants_payment(message)
    assert wants_payment('give me the link to pay')
