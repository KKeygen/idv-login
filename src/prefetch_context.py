"""Keep silent credential preparation out of interactive login UI/state."""
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar('account_prefetch', default=False)


def in_prefetch():
    return _active.get()


@contextmanager
def prefetch_scope():
    token = _active.set(True)
    try:
        yield
    finally:
        _active.reset(token)
