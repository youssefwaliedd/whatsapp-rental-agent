"""Database setup — SQLite or PostgreSQL, chosen by URL.

SQLite is the development default: a file, no server, perfect for tests and for
playing with the simulator.

**PostgreSQL is required in production**, and that is a measurement rather than
a preference. `tests/concurrency_check.py` fires ten simultaneous customers at
the webhook: on SQLite they are answered one at a time, because a turn holds its
write transaction from the first insert until commit and that spans the model
call. Ten customers with a five-second turn means the last waits nearly a
minute. Before `busy_timeout` was added it was worse — half of them were dropped
outright. Postgres writers do not block each other, which is the whole point.

Set `DATABASE_URL` to switch:

    DATABASE_URL=postgresql+psycopg://user@localhost/rental_agent
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
    """Where the data lives.

    An explicit `path` always wins, so tests and the simulator keep their own
    files regardless of what is configured. Otherwise `DATABASE_URL` decides,
    and only then does the SQLite default apply.
    """
    if path == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    if path is None:
        configured = os.getenv("DATABASE_URL")
        if configured:
            return configured
    return f"sqlite+pysqlite:///{Path(path or os.getenv('DEMO_DB_PATH') or DEFAULT_DB_PATH)}"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


#: How long a blocked writer waits before giving up, in milliseconds. Generous
#: on purpose — losing a customer's message is far worse than a slow reply.
BUSY_TIMEOUT_MS = 10_000


def create_db_engine(path: str | Path | None = None, echo: bool = False) -> Engine:
    url = database_url(path)

    if not is_sqlite(url):
        # Postgres. A pool sized for the webhook's background workers, since
        # each in-flight turn holds a connection for its whole duration.
        return create_engine(
            url,
            echo=echo,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )

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
