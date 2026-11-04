"""Verify unhandled HTTP failures preserve response diagnostics without URL."""
import ast
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import requests


def load_hooks():
    path = Path(__file__).resolve().parents[1] / 'src' / 'main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'_safe_exception_summary', '_global_excepthook', '_threading_excepthook'}
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=functions, type_ignores=[])
    state = {'os': os, 'sys': sys, 'requests': requests, 'logger': None}
    exec(compile(module, str(path), 'exec'), state)
    return state


class CapturingLogger:
    def __init__(self): self.messages = []
    def critical(self, message): self.messages.append(message)


class UnhandledHTTPErrorTests(unittest.TestCase):
    def test_main_and_thread_hooks_keep_error_body_but_not_request_url(self):
        hooks = load_hooks()
        logger = CapturingLogger()
        hooks['logger'] = logger
        response = requests.Response()
        response.status_code = 403
        response._content = b'upstream diagnostic body'
        try:
            raise requests.HTTPError('https://example.invalid/?token=secret', response=response)
        except requests.HTTPError as error:
            hooks['_global_excepthook'](type(error), error, error.__traceback__)
            hooks['_threading_excepthook'](SimpleNamespace(
                exc_type=type(error), exc_value=error,
                exc_traceback=error.__traceback__, thread=SimpleNamespace(name='test')))
        self.assertEqual(len(logger.messages), 2)
        for message in logger.messages:
            self.assertIn('HTTP status=403 response=upstream diagnostic body', message)
            self.assertNotIn('token=secret', message)


if __name__ == '__main__':
    unittest.main()
