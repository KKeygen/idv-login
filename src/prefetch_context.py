"""A silent renewal's deadline follows its HTTP requests on that worker only."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('account_prefetch_deadline', default=None)


def in_prefetch():
    return _deadline.get() is not None


@contextmanager
def prefetch_scope(deadline):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def install_request_deadline():
    import requests
    original_send = requests.Session.send

    def send(session, request, **kwargs):
        deadline = _deadline.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout('Account prefetch deadline reached')
            configured = kwargs.get('timeout')
            if isinstance(configured, tuple):
                kwargs['timeout'] = tuple(min(value, remaining) if value is not None else remaining
                                          for value in configured)
            else:
                kwargs['timeout'] = min(configured, remaining) if configured is not None else remaining
        return original_send(session, request, **kwargs)

    requests.Session.send = send
