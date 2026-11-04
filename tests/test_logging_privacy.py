"""Privacy boundary between console instructions and diagnostic logs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LoggingPrivacyTests(unittest.TestCase):
    def test_notice_is_not_written_to_file(self):
        src = Path(__file__).resolve().parents[1] / 'src'
        script = '''from logutil import setup_logger, user_notice, safe_exception
import requests
setup_logger().info("account_id=123 account_name=Alice")
setup_logger().error("error response body: sample_failure")
user_notice("instruction_for_user_only", level="WARNING")
try:
    response = requests.Response()
    response.status_code = 403
    response._content = b"body_diagnostic"
    raise requests.HTTPError("credential_in_url", response=response)
except requests.HTTPError:
    safe_exception(setup_logger(), "request failed")
'''
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=str(src))
            proc = subprocess.run([sys.executable, '-c', script], cwd=directory,
                                  env=env, capture_output=True, text=True, check=True)
            log = (Path(directory) / 'log.txt').read_text(encoding='utf-8')
        self.assertIn('instruction_for_user_only', proc.stdout)
        self.assertNotIn('instruction_for_user_only', log)
        self.assertIn('account_id=123 account_name=Alice', log)
        self.assertIn('error response body: sample_failure', log)
        self.assertIn('HTTP status=403 response=body_diagnostic', log)
        self.assertNotIn('credential_in_url', log)

    def test_safe_exception_supports_standard_logging(self):
        src = Path(__file__).resolve().parents[1] / 'src'
        script = '''import logging, requests
from logutil import safe_exception
logger = logging.getLogger("test-standard-logger")
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.ERROR)
try:
    response = requests.Response()
    response.status_code = 500
    response._content = b"upstream_error"
    raise requests.HTTPError("secret-query", response=response)
except requests.HTTPError:
    safe_exception(logger, "operation failed")
'''
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.run([sys.executable, '-c', script], cwd=directory,
                                  env=dict(os.environ, PYTHONPATH=str(src)),
                                  capture_output=True, text=True, check=True)
        self.assertIn('operation failed', proc.stderr)
        self.assertIn('HTTP status=500 response=upstream_error', proc.stderr)
        self.assertNotIn('secret-query', proc.stderr)


if __name__ == '__main__':
    unittest.main()
