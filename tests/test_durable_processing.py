"""Crash recovery and outbox integration with isolated databases and no network."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from rental_agent.agent.loop import AgentTurn
from rental_agent.store.models import WorkItem, CustomerDocument, Message
from rental_agent.whatsapp.jobs import Worker, enqueue, utcnow, retry_dead
from rental_agent.whatsapp.webhook import create_app
from rental_agent.whatsapp.client import WhatsAppClient
from rental_agent.whatsapp.media import PrivateMediaStore
from rental_agent.whatsapp.pacing import Pacer, Pacing
from tests.test_whatsapp import post, text_payload, APP_SECRET
from tests.test_document_collection import attachment, PDF, SETTINGS


def add(factory, key='a', lane='customer', kind='test'):
    with factory() as db:
        enqueue(db, key=key, lane=lane, kind=kind, payload={'secret': 'synthetic'})
        db.commit()


def rows(factory):
    with factory() as db: return list(db.scalars(select(WorkItem).order_by(WorkItem.id)))


def test_duplicate_receipts_and_process_restart(session_factory):
    add(session_factory); add(session_factory)
    calls = []
    worker = Worker(session_factory, {'test': lambda db, job: calls.append(job.key)})
    assert worker.run_once()
    assert not Worker(session_factory, worker.handlers).run_once()
    assert calls == ['a']
    assert rows(session_factory)[0].payload == {}


def test_failed_turn_rolls_back_outbox_and_retries_in_order(session_factory):
    add(session_factory); add(session_factory, 'b')
    attempts = []
    def handler(db, job):
        enqueue(db, key='reply:' + job.key, lane='out', kind='send', payload={})
        attempts.append(job.key)
        if len(attempts) == 1: raise TimeoutError('private provider body')
    clock = [utcnow()]
    worker = Worker(session_factory, {'test': handler, 'send': lambda *_: None}, now_fn=lambda: clock[0])
    worker.run_once()
    assert len(rows(session_factory)) == 2
    assert rows(session_factory)[0].last_error == 'TimeoutError'
    assert not worker.run_once()  # Later customer message cannot jump the retry.
    clock[0] += timedelta(seconds=3)
    assert worker.run_once(); assert worker.run_once()
    assert attempts == ['a', 'a', 'b']


def test_dead_letter_blocks_lane_and_can_be_retried(session_factory):
    add(session_factory); add(session_factory, 'b'); add(session_factory, 'c', 'other')
    def fail(*_): raise RuntimeError()
    worker = Worker(session_factory, {'test': fail}, max_attempts=1)
    worker.run_once()
    assert rows(session_factory)[0].status == 'dead'
    assert worker.run_once()  # Unrelated customer is not blocked.
    assert not worker.run_once()
    with session_factory() as db:
        retry_dead(db, rows(session_factory)[0].id); db.commit()
    worker.handlers['test'] = lambda *_: None
    assert worker.run_once(); assert worker.run_once()


class RecordingAgent:
    def __init__(self): self.calls = 0; self.fail = False
    def respond(self, ctx, text, provider_message_id=None, **kwargs):
        self.calls += 1
        ctx.messages.record(conversation_id=ctx.conversation_id, direction='inbound',
                            content=text, now=ctx.now(), provider_message_id=provider_message_id)
        if self.fail: raise RuntimeError('synthetic failure after writing inbound')
        return AgentTurn(reply='Here are the rental options.')


def app_for(factory, tmp_path, agent=None, transport=None, checker=None):
    client = WhatsAppClient(SETTINGS, transport=transport or (lambda p: {'messages':[{'id':'wamid.SENT'}]}),
                            pacer=Pacer(Pacing(enabled=False)))
    return create_app(session_factory=factory, agent_factory=lambda: agent or RecordingAgent(),
        settings=SETTINGS, client=client, durable=True,
        document_store=PrivateMediaStore(tmp_path/'private'), media_downloader=lambda _: (PDF, 'application/pdf'),
        document_checker=checker)


def current_payload(text='hi', message_id='wamid.HELLO'):
    payload=text_payload(text, message_id=message_id)
    payload['entry'][0]['changes'][0]['value']['messages'][0]['timestamp']=str(int(utcnow().timestamp()))
    return payload


def test_webhook_persists_before_ack_and_new_worker_delivers(session_factory,tmp_path):
    sent=[]; agent=RecordingAgent()
    app=app_for(session_factory,tmp_path,agent,lambda p: sent.append(p) or {'messages':[{'id':'wamid.SENT'}]})
    with TestClient(app) as web:
        assert post(web,current_payload()).status_code==200
        assert post(web,current_payload()).status_code==200
    assert agent.calls==0 and sent==[]
    assert len(rows(session_factory))==1
    restarted=app_for(session_factory,tmp_path,agent,lambda p: sent.append(p) or {'messages':[{'id':'wamid.SENT'}]})
    assert restarted.state.worker.run_once()
    assert agent.calls==1 and sent==[]
    while restarted.state.worker.run_once(): pass
    assert len(sent)==1 and sent[0]['text']['body']=='Here are the rental options.'


def test_crashed_agent_does_not_leave_inbound_deduplication_record(session_factory,tmp_path):
    agent=RecordingAgent();agent.fail=True
    app=app_for(session_factory,tmp_path,agent)
    with TestClient(app) as web:post(web,current_payload())
    app.state.worker.run_once()
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Message))==0
        job=db.scalar(select(WorkItem));job.available_at=utcnow();db.commit()
    agent.fail=False
    app.state.worker.run_once()
    assert agent.calls==2
    assert rows(session_factory)[0].status=='done'


def test_outbound_failure_does_not_regenerate_ai_response(session_factory,tmp_path):
    agent=RecordingAgent()
    def transport(p): raise TimeoutError('outage')
    app=app_for(session_factory,tmp_path,agent,transport)
    with TestClient(app) as web:post(web,current_payload())
    app.state.worker.run_once();app.state.worker.run_once()
    assert agent.calls==1
    assert rows(session_factory)[1].attempts==1
    app2=app_for(session_factory,tmp_path,agent)
    with session_factory() as db:
        job=db.get(WorkItem,rows(session_factory)[1].id);job.available_at=utcnow();db.commit()
    assert app2.state.worker.run_once()
    assert agent.calls==1 and rows(session_factory)[1].status=='done'


def test_expired_window_holds_reply_until_new_message(session_factory,tmp_path):
    sent=[]
    app=app_for(session_factory,tmp_path,transport=lambda p:sent.append(p) or {'messages':[]})
    payload=current_payload()
    payload['entry'][0]['changes'][0]['value']['messages'][0]['timestamp']=str(int((utcnow()-timedelta(days=2)).timestamp()))
    with TestClient(app) as web:post(web,payload)
    app.state.worker.run_once();app.state.worker.run_once()
    assert sent==[] and rows(session_factory)[1].status=='waiting_window'
    with TestClient(app) as web:post(web,current_payload('hello again','wamid.NEW'))
    # First attempted old reply may still be held before new inbound is processed.
    for _ in range(6): app.state.worker.run_once()
    assert any(j.status=='done' and j.kind=='inbound' for j in rows(session_factory))


def test_attachment_and_outbox_commit_together(session_factory,tmp_path):
    app=app_for(session_factory,tmp_path)
    with TestClient(app) as web:post(web,attachment())
    assert app.state.worker.run_once()
    with session_factory() as db:
        assert db.scalar(select(CustomerDocument)).status=='pending_review'
    assert len(rows(session_factory))==2


def test_readiness_requires_worker_and_flags_dead_jobs(session_factory,tmp_path):
    from rental_agent.store.models import WorkerHeartbeat
    app=app_for(session_factory,tmp_path)
    with TestClient(app) as web:
        assert web.get('/ready').status_code==503
        with session_factory() as db:
            db.add(WorkerHeartbeat(name='whatsapp',seen_at=utcnow()));db.commit()
        assert web.get('/ready').status_code==200
        add(session_factory)
        with session_factory() as db:
            db.scalar(select(WorkItem)).status='dead';db.commit()
        assert web.get('/ready').status_code==503


def test_scheduled_maintenance_runs_without_customer_message(session_factory,tmp_path):
    app=app_for(session_factory,tmp_path)
    add(session_factory,key='maintenance:sample',lane='maintenance',kind='maintenance')
    assert app.state.worker.run_once()
    assert rows(session_factory)[0].status=='done'


def test_gemini_document_result_flows_to_customer_outbox(session_factory,tmp_path):
    from rental_agent.services.document_checks import DocumentChecker
    from tests.test_automated_documents import Reader
    app=app_for(session_factory,tmp_path,checker=DocumentChecker(Reader(),match_key='test'))
    with TestClient(app) as web:post(web,attachment())
    app.state.worker.run_once()
    with session_factory() as db:
        assert db.scalar(select(CustomerDocument)).status=='checks_passed'
    out=rows(session_factory)[1].payload['text']['body']
    assert 'passed the automated' in out and 'staff review' not in out


def test_real_agent_provider_outage_is_retried_transactionally(session_factory,tmp_path):
    from rental_agent.agent.loop import Agent
    from rental_agent.agent.settings import AgentSettings
    from rental_agent.agent.providers.errors import ProviderUnavailable
    from tests.fake_anthropic import FakeClient, says
    llm=FakeClient(script=[ProviderUnavailable('gemini','timeout'),says('What dates would you like?')])
    agent=Agent(llm,AgentSettings(extraction_enabled=False))
    app=app_for(session_factory,tmp_path,agent)
    with TestClient(app) as web:post(web,current_payload('I need a rental car'))
    app.state.worker.run_once()
    assert rows(session_factory)[0].status=='pending'
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Message))==0
        db.scalar(select(WorkItem)).available_at=utcnow();db.commit()
    app.state.worker.run_once()
    assert rows(session_factory)[0].status=='done'


def test_durable_download_outage_is_retryable(session_factory,tmp_path):
    from rental_agent.whatsapp.media import TransientMediaError
    tries=[]
    def download(_):
        tries.append(1)
        if len(tries)==1:raise TransientMediaError('synthetic timeout')
        return PDF,'application/pdf'
    app=create_app(session_factory=session_factory,agent_factory=lambda:RecordingAgent(),
        settings=SETTINGS,durable=True,document_store=PrivateMediaStore(tmp_path/'private'),
        media_downloader=download)
    with TestClient(app) as web:post(web,attachment())
    app.state.worker.run_once()
    assert rows(session_factory)[0].status=='pending'
    with session_factory() as db:
        assert db.scalar(select(CustomerDocument)) is None
        db.scalar(select(WorkItem)).available_at=utcnow();db.commit()
    app.state.worker.run_once()
    assert rows(session_factory)[0].status=='done'


def test_webhook_rejects_oversized_payload_without_enqueue(session_factory,tmp_path):
    app=app_for(session_factory,tmp_path)
    with TestClient(app) as web:
        result=web.post('/webhook',content=b'x'*(2*1024*1024+1))
    assert result.status_code==413 and rows(session_factory)==[]


def test_provider_retry_delay_starts_after_failure(session_factory):
    from rental_agent.agent.providers.errors import ProviderUnavailable
    clock = [utcnow()]
    initial = clock[0]
    with session_factory() as db:
        enqueue(db,key='rate-limited',lane='customer',kind='inbound',payload={},now=initial)
        db.commit()
    def fail(db, job):
        clock[0] += timedelta(seconds=15)
        exc = ProviderUnavailable('rate_limit (HTTP 429)')
        exc.retry_after = 45
        raise exc
    worker = Worker(session_factory,{'inbound':fail},now_fn=lambda:clock[0])
    worker.run_once()
    job = rows(session_factory)[0]
    assert job.available_at == initial + timedelta(seconds=60)
    assert job.last_error == 'rate_limit (HTTP 429)'
    assert not worker.run_once()
