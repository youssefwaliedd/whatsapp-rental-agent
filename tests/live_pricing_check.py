"""Opt-in Gemini smoke test using only fictional fixtures and fresh messages.

Run: python -m tests.live_pricing_check. No existing customer database is read.
Payments are simulated. This performs paid API requests.
"""
import json
from tests.conftest import FROZEN_NOW, REFERENCE_DATE
from rental_agent.agent.loop import build_agent
from rental_agent.context import ToolContext
from rental_agent.store.db import create_db_engine, init_db


def main():
    factory = init_db(create_db_engine(':memory:'))
    agent = build_agent()
    results = []
    with factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda:FROZEN_NOW, reference_date=REFERENCE_DATE)
        customer, _ = ctx.customers.get_or_create('+971500000099', FROZEN_NOW)
        conversation, _ = ctx.conversations.get_or_create(customer.customer_id, FROZEN_NOW)
        ctx.customer_id, ctx.conversation_id = customer.customer_id, conversation.conversation_id
        for message in [
            'I want a Mercedes from September 10th at noon until September 13th at noon, pickup at your branch.',
            'What are their prices? Please compare the available Mercedes options.'
        ]:
            result = agent.respond(ctx, message)
            session.commit()
            item = {'message':message, 'reply':result.reply, 'tools':result.tool_calls,
                    'provider_error':result.provider_error}
            results.append(item)
            print(json.dumps(item), flush=True)
    return all(not result['provider_error'] for result in results)


if __name__ == '__main__':
    raise SystemExit(0 if main() else 1)
