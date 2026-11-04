import datetime
import json
import logging
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
import telemetry
import hotfixmgr


class TelemetryTests(unittest.TestCase):
    def tearDown(self):
        telemetry.stop_telemetry()

    def test_build_and_debug_gates(self):
        self.assertTrue(telemetry.release_version('v6.3.3-beta'))
        self.assertTrue(telemetry.release_version('6.3.3'))
        self.assertTrue(telemetry.release_version('v1.2'))
        for value in ('', 'main', 'fever', None, 'debug-6.3.3'):
            self.assertFalse(telemetry.release_version(value))
        self.assertFalse(telemetry.start_telemetry(True, 'main'))
        self.assertFalse(telemetry.start_telemetry(False, 'v6.3.3-beta'))
        with patch.object(telemetry, 'Telemetry') as constructor:
            self.assertTrue(telemetry.start_telemetry(True, 'v6.3.3-beta'))
            constructor.assert_called_once_with('v6.3.3-beta')

    def test_cli_and_saved_setting_with_disable_restart(self):
        with patch.object(telemetry.genv, 'get', side_effect=lambda key, default=None: key == telemetry.SETTING):
            with patch.object(telemetry.genv, 'set'):
                args = SimpleNamespace(debug=False)
                self.assertTrue(telemetry.configure(args))
                self.assertTrue(args.debug)
        self.assertEqual(hotfixmgr._restart_debug_args(['app.py', '--debug', '--test'], False), ['app.py', '--test'])
        self.assertEqual(hotfixmgr._restart_debug_args(['app.py', '--test'], True), ['app.py', '--test', '--debug'])

    def test_same_body_and_no_notices(self):
        # Capture the actual Loguru pipeline and verify identical text reaches
        # file and the async envelope, including an untouched HTTP error body.
        with tempfile.TemporaryDirectory() as folder:
            import os
            previous = os.getcwd()
            os.chdir(folder)
            try:
                import logutil
                sent = []
                ready = threading.Event()
                def transport(data):
                    sent.append(data)
                    ready.set()
                worker = telemetry.Telemetry('v6.3.3-beta', transport)
                worker.batch_window = 0.1
                with telemetry._lock:
                    telemetry._current = worker
                text = 'HTTP 401 response={"token":"literal-error", "detail":"失败"}'
                logutil.logger.warning(text)
                try:
                    raise RuntimeError('synthetic-exception-detail')
                except RuntimeError:
                    logutil.logger.exception('synthetic-exception-message')
                logutil.user_notice('NOTICE-MUST-NOT-BE-UPLOADED')
                self.assertTrue(ready.wait(3))
                worker.close()
                local = pathlib.Path('log.txt').read_text()
                bodies = [item['body'] for payload in sent for item in json.loads(payload.splitlines()[2])['items']]
                self.assertIn(text, local)
                self.assertIn(text, ''.join(bodies))
                self.assertIn('synthetic-exception-detail', local)
                self.assertEqual(local, ''.join(bodies))
                self.assertNotIn('NOTICE-MUST-NOT-BE-UPLOADED', local)
                self.assertNotIn('NOTICE-MUST-NOT-BE-UPLOADED', ''.join(bodies))
                logging.getLogger('hotfixmgr').warning('standard-logging-example')
                self.assertIn('standard-logging-example', pathlib.Path('log.txt').read_text())
            finally:
                os.chdir(previous)

    def test_distributed_records_batch_and_shutdown_flushes(self):
        sent = []
        worker = telemetry.Telemetry('v6.3.3-beta', sent.append)
        worker.batch_window = 0.4
        def record(text):
            return {'message': text, 'level': SimpleNamespace(name='INFO'),
                    'time': datetime.datetime.now(), 'name': 'test', 'line': 3}
        worker.submit(record('first'), 'first')
        time.sleep(0.1)
        worker.submit(record('second'), 'second')
        time.sleep(0.1)
        self.assertEqual(sent, [])
        worker.close(flush=True)
        self.assertEqual(len(sent), 1)
        self.assertEqual([x['body'] for x in json.loads(sent[0].splitlines()[2])['items']],
                         ['first', 'second'])

    def test_disable_discards_pending_batch(self):
        sent = []
        worker = telemetry.Telemetry('v6.3.3-beta', sent.append)
        worker.submit({'message': 'pending', 'level': SimpleNamespace(name='INFO'),
                       'time': datetime.datetime.now(), 'name': 'test', 'line': 3}, 'pending')
        time.sleep(0.05)
        worker.close()
        worker.thread.join(timeout=1)
        self.assertEqual(sent, [])

    def test_full_batches_respect_429_backoff(self):
        sent = []
        def transport(data):
            sent.append((time.monotonic(), data))
            return 429 if len(sent) == 1 else 200
        worker = telemetry.Telemetry('v6.3.3-beta', transport)
        worker.batch_window = 0.02
        worker.rate_limit_backoff = 0.25
        worker.send_interval = 0.1
        def record(n):
            return {'message': str(n), 'level': SimpleNamespace(name='INFO'),
                    'time': datetime.datetime.now(), 'name': 'test', 'line': 3}
        for n in range(64):
            worker.submit(record(n), str(n))
        deadline = time.monotonic() + 2
        while len(sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.close()
        self.assertEqual(len(sent), 2)
        self.assertGreaterEqual(sent[1][0] - sent[0][0], 0.23)
        first = [x['body'] for x in json.loads(sent[0][1].splitlines()[2])['items']]
        retry = [x['body'] for x in json.loads(sent[1][1].splitlines()[2])['items']]
        self.assertEqual(first, retry)
        self.assertEqual(telemetry._retry_after('0', 30), 1)
        self.assertEqual(telemetry._retry_after('500', 30), 120)
        self.assertEqual(telemetry._retry_after('invalid', 30), 30)

    def test_sender_uses_direct_fixed_https_and_no_redirects(self):
        captured = {}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, n): return b''
            status = 200
        class Opener:
            def open(self, request, timeout):
                captured.update(request=request, timeout=timeout)
                return Response()
        def fake_opener(*handlers):
            captured['handlers'] = handlers
            return Opener()
        with patch.object(telemetry.urllib.request, 'build_opener', side_effect=fake_opener):
            telemetry._send(b'test')
        self.assertEqual(captured['request'].full_url, 'https://idv-telemetry.keygen.eu.org/api/1/envelope/')
        self.assertEqual(captured['timeout'], 3)
        self.assertTrue(any(isinstance(h, telemetry._NoRedirect) for h in captured['handlers']))
        proxy = next(h for h in captured['handlers'] if isinstance(h, telemetry.urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})


if __name__ == '__main__':
    unittest.main()
