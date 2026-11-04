"""Cooperative wall-clock budget shared by nested crowd search operations."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('crowd_search_deadline', default=None)


class SearchTimeout(Exception):
    """The active crowd solve exhausted its wall-clock allowance."""


def check_deadline():
    deadline = _deadline.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise SearchTimeout()


@contextmanager
def search_budget(seconds):
    parent = _deadline.get()
    deadline = None if seconds is None else time.monotonic() + seconds
    if parent is not None:
        deadline = parent if deadline is None else min(parent, deadline)
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)
