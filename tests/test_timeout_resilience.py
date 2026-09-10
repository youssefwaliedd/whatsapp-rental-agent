"""Bounded optional work and nonblocking browser reads, without live services."""
from concurrent.futures import Future
from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from rental_agent.agent.loop import Agent, _Pending
from rental_agent.agent.settings import AgentSettings
from rental_agent.agent.providers import budget
from rental_agent.services.document_checks import GeminiDocumentReader
from rental_agent.store.models import Customer
from rental_agent.webchat.app import create_app
from tests.conftest import FROZEN_NOW, REFERENCE_DATE
from tests.fake_anthropic import FakeClient, says
from tests.test_webchat import StubAgent


def test_optional_extraction_wait_is_bounded(monkeypatch):
    waits=[]
    class SlowFuture(Future):
        def result(self,timeout=None):
            waits.append(timeout)
            raise TimeoutError('still extracting')
    future=SlowFuture();pending=_Pending(future=future)
    with budget.turn_budget(30):pending.join()
    assert waits==[0.25]
    assert future.cancelled() and pending.joined
    assert pending.error.startswith('TimeoutError')


def test_slow_extraction_does_not_hold_up_an_answer(booking_ctx,monkeypatch):
    from rental_agent.agent import loop
    future=Future()
    monkeypatch.setattr(loop._EXTRACTION_POOL,'submit',lambda *_:future)
    bot=Agent(FakeClient(script=[says('Which car would you like?')]),AgentSettings(extraction_enabled=True))
    turn=bot.respond(booking_ctx,'hello')
    assert turn.reply=='Which car would you like?'
    assert turn.extraction_error.startswith('TimeoutError')
    assert future.cancelled()


def test_nested_budget_cannot_extend_parent_deadline(monkeypatch):
    monkeypatch.setattr(budget.time,'monotonic',lambda:100)
    with budget.turn_budget(5):
        with budget.turn_budget(30):assert budget.remaining()==5
    assert budget.deadline.get() is None


def test_browser_refresh_does_not_write_while_chat_holds_sqlite_lock(session_factory):
    from sqlalchemy import select
    app=create_app(session_factory=session_factory,agent_factory=StubAgent,
        now_fn=lambda:FROZEN_NOW,reference_date=REFERENCE_DATE)
    web=TestClient(app)
    assert web.get('/api/state').status_code==200
    assert web.get('/api/learning').status_code==200
    with session_factory() as writer:
        customer=writer.scalar(select(Customer))
        customer.last_seen_at=FROZEN_NOW+timedelta(seconds=10)
        writer.flush()
        assert web.get('/api/state').status_code==200
        assert web.get('/api/learning').status_code==200
        writer.rollback()


def test_document_reader_retries_with_bounded_http_calls(monkeypatch):
    from rental_agent.agent.providers import gemini
    clock=[100.0];configs=[]
    monkeypatch.setattr(gemini.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(gemini.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    def generate(**kwargs):
        configs.append(kwargs['config'])
        if len(configs)==1:raise RuntimeError('503 UNAVAILABLE')
        return SimpleNamespace(text=json.dumps({'document_type':'passport','readable':True,'full_name':'Sample Driver','expiry_date':'2035-01-01','confidence':.98}))
    reader=GeminiDocumentReader(SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    assert reader.extract(b'synthetic bytes','application/pdf').document_type=='passport'
    assert len(configs)==2
    assert all(c.http_options.timeout<=15000 and c.http_options.retry_options.attempts==1 for c in configs)
