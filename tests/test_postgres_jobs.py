"""Opt-in real PostgreSQL locking and connection-loss checks, isolated schema."""
import os
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text, select
from rental_agent.store.db import init_db
from rental_agent.store.models import WorkItem
from rental_agent.whatsapp.jobs import Worker, enqueue


@pytest.fixture
def pg_factory():
    url=os.getenv('TEST_POSTGRES_URL')
    if not url:pytest.skip('Set TEST_POSTGRES_URL for real PostgreSQL checks')
    schema='queue_test_'+uuid4().hex
    admin=create_engine(url)
    with admin.begin() as conn:conn.execute(text('CREATE SCHEMA '+schema))
    engine=create_engine(url,connect_args={'options':'-csearch_path='+schema})
    factory=init_db(engine)
    yield factory,admin
    engine.dispose()
    with admin.begin() as conn:conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))
    admin.dispose()


def add(factory,key,lane):
    with factory() as db:
        enqueue(db,key=key,lane=lane,kind='test',payload={});db.commit()


def test_postgres_workers_never_overtake_same_customer(pg_factory):
    factory,_=pg_factory
    add(factory,'a','one');add(factory,'b','one');add(factory,'c','two')
    entered=threading.Event();release=threading.Event();seen=[]
    def slow(db,job):
        seen.append(job.key);entered.set();assert release.wait(5)
    thread=threading.Thread(target=lambda:Worker(factory,{'test':slow}).run_once())
    thread.start();assert entered.wait(5)
    other=Worker(factory,{'test':lambda db,job:seen.append(job.key)})
    try:
        assert other.run_once();assert not other.run_once()
        assert seen==['a','c']
    finally:release.set();thread.join(5)
    assert other.run_once() and seen==['a','c','b']


def test_connection_loss_recovers_job_and_rolls_back_partial_outbox(pg_factory):
    factory,admin=pg_factory
    add(factory,'a','one')
    entered=threading.Event();release=threading.Event();pids=[];errors=[]
    def interrupted(db,job):
        pids.append(db.scalar(text('SELECT pg_backend_pid()')))
        enqueue(db,key='unsent-reply',lane='out',kind='send',payload={})
        entered.set();assert release.wait(5)
        db.execute(text('SELECT 1'))
    def run():
        try:Worker(factory,{'test':interrupted}).run_once()
        except Exception as exc:errors.append(type(exc).__name__)
    thread=threading.Thread(target=run);thread.start();assert entered.wait(5)
    try:
        with admin.begin() as conn:conn.execute(text('SELECT pg_terminate_backend(:pid)'),{'pid':pids[0]})
    finally:release.set();thread.join(5)
    with factory() as db:
        jobs=list(db.scalars(select(WorkItem)))
        assert len(jobs)==1 and jobs[0].status=='pending'
        # The retry may have backoff after the dropped connection.
        from rental_agent.whatsapp.jobs import utcnow
        jobs[0].available_at=utcnow();db.commit()
    assert Worker(factory,{'test':lambda *_:None}).run_once()
    with factory() as db:assert db.scalar(select(WorkItem)).status=='done'
