"""Opt-in paid Gemini checkout smoke test with newly generated sample data only.

Uses fictional fleet fixtures, an empty in-memory database, temporary encrypted
sample files and simulated payments. Never opens a saved customer database.
Run: .venv/bin/python -m tests.live_checkout_check
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from tests.conftest import FROZEN_NOW, REFERENCE_DATE
from tests.live_document_check import sample
from rental_agent.agent.loop import build_agent
from rental_agent.context import ToolContext
from rental_agent.services import checkout, documents
from rental_agent.services.document_checks import DocumentChecker
from rental_agent.store.db import create_db_engine, init_db
from rental_agent.whatsapp.storage import DiskBackend, EncryptedStore


def main():
    factory = init_db(create_db_engine(':memory:'))
    agent = build_agent()
    with tempfile.TemporaryDirectory(prefix='fictional-checkout-') as directory, factory() as session:
        ctx = ToolContext(session=session, now_fn=lambda:FROZEN_NOW, reference_date=REFERENCE_DATE)
        customer,_ = ctx.customers.get_or_create('+971500000098',FROZEN_NOW)
        conversation,_ = ctx.conversations.get_or_create(customer.customer_id,FROZEN_NOW)
        ctx.customer_id,ctx.conversation_id = customer.customer_id,conversation.conversation_id
        ctx.engine.rules._data['booking']['enforce_checkout_flow'] = True
        state=ctx.load_state()
        state.current_vehicle_options=['veh_07']
        ctx.save_state(state)
        def turn(message):
            result=agent.respond(ctx,message)
            session.commit()
            print(json.dumps({'message':message,'reply':result.reply,'tools':result.tool_calls,
                              'provider_error':result.provider_error}),flush=True)
            assert not result.provider_error, 'Provider failed'
            return result
        turn('I want to book the Mercedes C300 from September 10th at 5pm until September 13th at 5pm.')
        turn('I am 31 years old and a UAE resident. Delivery to Dubai Marina.')
        assert checkout.evaluate(ctx,checkout.current_quote(ctx))['step']=='documents'
        store=EncryptedStore(DiskBackend(Path(directory)/'documents'),Fernet.generate_key().decode())
        checker=DocumentChecker(match_key='fictional-checkout-test-only')
        for index,kind in enumerate(['EMIRATES ID','DRIVING LICENCE']):
            data=sample(kind=kind,file_format='PNG' if index==0 else 'PDF')
            mime='image/png' if index==0 else 'application/pdf'
            message=SimpleNamespace(message_id=f'sample-{index}',media_kind='document',raw={'document':{'id':str(index)}})
            row=documents.receive(ctx,message,store,lambda _:(data,mime))
            checker.check(ctx,row,store)
            session.commit()
            print(json.dumps({'sample':kind,'status':row.status}),flush=True)
            assert row.status=='checks_passed'
        turn('continue')
        assert ctx.load_state().checkout.get('offered'), 'Terms were not presented'
        turn(checkout.ACCEPT)
        assert ctx.load_state().reservation_id, 'Model did not create the eligible reservation'
        turn('I want to pay')
        reservation=ctx.reservations.get(ctx.load_state().reservation_id)
        assert reservation.payment_status=='link_sent', 'Payment link missing'
        print(json.dumps({'passed':True,'payment_provider':'simulated'}),flush=True)


if __name__=='__main__':
    main()
