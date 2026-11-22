"""Offline tests for Honor OAuth redirect tolerance.

The Honor authorize page can return ``honorid://redirect_url/?code=...``
(trailing slash) or omit ``state`` entirely.  Both used to fail validation,
leaving the login window open with no diagnostic, so a successful interactive
authorization looked like a dead button.
"""
import ast
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/channelHandler/honorLogin/session.py"
REDIRECT = "honorid://redirect_url"
STATE = "fixture-state"


def load_pure():
    namespace = {}
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    exec(compile(tree, str(SOURCE), "exec"), namespace)
    return namespace


PURE = load_pure()


class RedirectToleranceTests(unittest.TestCase):
    def accepts(self, url, expected, state=STATE):
        self.assertEqual(PURE["valid_redirect"](url, state, REDIRECT), expected, url)

    def test_exact_redirect_is_accepted(self):
        self.accepts("honorid://redirect_url?code=fixture&state=fixture-state", True)

    def test_trailing_slash_is_accepted(self):
        self.accepts("honorid://redirect_url/?code=fixture&state=fixture-state", True)

    def test_absent_state_is_accepted(self):
        self.accepts("honorid://redirect_url?code=fixture", True)
        self.accepts("honorid://redirect_url/?code=fixture", True)

    def test_mismatched_state_is_still_rejected(self):
        self.accepts("honorid://redirect_url?code=fixture&state=other", False)

    def test_missing_code_is_rejected(self):
        self.accepts("honorid://redirect_url?state=fixture-state", False)
        self.accepts("honorid://redirect_url?code=&state=fixture-state", False)

    def test_error_and_duplicate_code_are_rejected(self):
        self.accepts("honorid://redirect_url?error=access_denied&state=fixture-state", False)
        self.accepts("honorid://redirect_url?code=a&code=b&state=fixture-state", False)

    def test_other_host_is_rejected(self):
        self.accepts("honorid://other?code=fixture&state=fixture-state", False)

    def test_expected_state_is_required(self):
        self.assertFalse(PURE["valid_redirect"]("honorid://redirect_url?code=fixture", "", REDIRECT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
