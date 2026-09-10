"""Transactional PostgreSQL inbox/outbox; SQLite supports single-worker tests.

A database row lock remains held throughout each job. A crash rolls back the
turn and releases the lock. Outbound delivery is at least once: Meta can accept
an HTTP request whose response is lost, so exact-once delivery is not claimed.
"""
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.orm import aliased

from ..store.models import WorkItem
from ..agent.providers.errors import ProviderUnavailable, safe_provider_error

active_session = ContextVar('work_session', default=None)


class WindowClosed(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def enqueue(session, *, key, lane, kind, payload, now=None):
    now = now or utcnow()
    # Database upsert also handles simultaneous webhook redeliveries.
    dialect = session.get_bind().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError('Durable jobs require PostgreSQL or SQLite')
    session.execute(insert(WorkItem).values(key=key, lane=lane, kind=kind,
        payload=payload, status='pending', attempts=0, available_at=now,
        created_at=now).on_conflict_do_nothing(index_elements=['key']))


class TurnSession:
    """Legacy turn checkpoints flush; the worker owns the only transaction."""
    def __init__(self, session): self.session = session
    def __getattr__(self, name): return getattr(self.session, name)
    def commit(self): self.session.flush()
    def close(self): pass
    def rollback(self):
        raise RuntimeError('Turn failed; worker must roll back the complete job')


def queue_payload(payload):
    session = active_session.get()
    if session is None:
        return None
    from .client import SendResult
    if payload.get('status') == 'read':
        return SendResult(ok=True)  # Ephemeral typing/read events are not durable work.
    key = 'out:' + uuid4().hex
    enqueue(session, key=key, lane='out:' + payload['to'], kind='outbound', payload=payload)
    return SendResult(ok=True, message_ids=[key])


class Worker:
    def __init__(self, factory, handlers, *, now_fn=utcnow, max_attempts=6):
        self.factory, self.handlers = factory, handlers
        self.now_fn, self.max_attempts = now_fn, max_attempts

    def run_once(self):
        now = self.now_fn()
        with self.factory() as session:
            earlier = aliased(WorkItem)
            job = session.scalar(select(WorkItem).where(
                WorkItem.status == 'pending', WorkItem.available_at <= now,
                ~exists(select(earlier.id).where(earlier.lane == WorkItem.lane,
                    earlier.id < WorkItem.id, earlier.status.in_(['pending', 'dead'])))
            ).order_by(WorkItem.id).with_for_update(skip_locked=True).limit(1))
            if job is None: return False
            job_id = job.id
            try:
                self.handlers[job.kind](session, job)
                job.status = 'done'
                job.payload = {}  # Avoid retaining copies of customer messages.
                job.last_error = None
                session.commit()
            except Exception as exc:
                session.rollback()
                # Acquire the same row again; another worker may have recovered it.
                job = session.get(WorkItem, job_id, with_for_update=True)
                if job.status != 'pending': return True
                if isinstance(exc, WindowClosed):
                    job.status = 'waiting_window'
                    session.commit()
                    return True
                job.attempts += 1
                job.last_error = safe_provider_error(exc) if isinstance(exc, ProviderUnavailable) else type(exc).__name__
                backoff = min(300, 2 ** job.attempts)
                if isinstance(exc, ProviderUnavailable) and exc.retry_after:
                    backoff = max(backoff, exc.retry_after)
                # Count from failure, not job start: a long request must not
                # consume its own recovery delay and trigger an immediate retry.
                job.available_at = self.now_fn() + timedelta(seconds=backoff)
                if job.attempts >= self.max_attempts: job.status = 'dead'
                session.commit()
            return True


def retry_dead(session, job_id, now=None):
    """Explicit operator recovery, without deleting the deduplication key."""
    session.execute(update(WorkItem).where(WorkItem.id == job_id,
        WorkItem.status == 'dead').values(status='pending', attempts=0,
        available_at=now or utcnow(), last_error=None))
