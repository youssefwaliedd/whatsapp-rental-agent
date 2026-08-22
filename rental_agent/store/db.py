"""Database setup.

SQLite for the prototype. The schema is deliberately Postgres-compatible so the
move to a real deployment is a URL change plus a migration tool, not a rewrite.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Counter

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "demo.db"

#: The specification's example reservation is DEMO-1042, so the first demo
#: booking made by the prototype gets exactly that reference.
RESERVATION_SEQUENCE_START = 1041
QUOTE_SEQUENCE_START = 500


def database_url(path: str | Path | None = None) -> str:
    if path == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    return f"sqlite+pysqlite:///{Path(path or os.getenv('DEMO_DB_PATH') or DEFAULT_DB_PATH)}"


#: How long a blocked writer waits before giving up, in milliseconds. Generous
#: on purpose — losing a customer's message is far worse than a slow reply.
BUSY_TIMEOUT_MS = 10_000


def create_db_engine(path: str | Path | None = None, echo: bool = False) -> Engine:
    url = database_url(path)
    engine = create_engine(
        url,
        echo=echo,
        # A single in-memory database shared across sessions, so tests can use
        # one engine without the schema vanishing between connections.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
        cursor = dbapi_connection.cursor()
        # Foreign keys are off by default in SQLite; without this the FK
        # declarations in models.py would be documentation only.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # SQLite allows one writer at a time. Without a busy timeout the second
        # writer does not queue — it fails immediately with "database is
        # locked", and on this system that means a customer's turn dies and
        # they get nothing.
        #
        # Measured before this line: 10 customers messaging simultaneously
        # produced 5 replies. A rental company's WhatsApp on a Friday evening
        # is exactly that shape of traffic.
        #
        # Waiting is the right behaviour: a turn already takes seconds, so a few
        # hundred milliseconds queueing for the write is invisible next to it.
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.close()

    return engine


def init_db(engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _seed_counters(factory)
    return factory


def _seed_counters(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        for name, start in (
            ("reservation", RESERVATION_SEQUENCE_START),
            ("quote", QUOTE_SEQUENCE_START),
        ):
            if session.get(Counter, name) is None:
                session.add(Counter(name=name, value=start))
        session.commit()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_db(engine: Engine) -> sessionmaker[Session]:
    """Drop everything and rebuild. Backs `/demo-reset`."""
    Base.metadata.drop_all(engine)
    return init_db(engine)
