"""A shared deadline for all model calls in one customer turn."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

deadline = ContextVar("rental_turn_deadline", default=None)


def remaining(default=15.0):
    end = deadline.get()
    return max(0.0, end - time.monotonic()) if end is not None else default


@contextmanager
def turn_budget(seconds):
    end = time.monotonic() + seconds
    parent = deadline.get()
    token = deadline.set(min(end, parent) if parent is not None else end)
    try:
        yield
    finally:
        deadline.reset(token)
