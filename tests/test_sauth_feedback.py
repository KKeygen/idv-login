"""Synthetic error responses and offscreen Qt only; no channel/game access."""
import ast
import base64
import json
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))

import sauth_feedback as feedback
import app_state
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox


class DetailsTests(unittest.TestCase):
    def test_plain_and_base64_utf8_messages_and_separate_codes(self):
        for msg in ('渠道登录过期', base64.b64encode('渠道登录过期'.encode()).decode()):
            text = feedback.failure_details(200, {'code': 403, 'subcode': 10, 'msg': msg})
            self.assertIn('HTTP: 200', text)
            self.assertIn('code: 403', text)
            self.assertIn('subcode: 10', text)
            self.assertIn('渠道登录过期', text)

    def test_unparseable_or_nontext_responses_do_not_dump_payload(self):
        self.assertNotIn('secret', feedback.failure_details(502, '<html>secret</html>'))
        text = feedback.failure_details(None, {'msg': {'token': 'secret'}, 'status': '%%not-base64%%'})
        self.assertIn('%%not-base64%%', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('HTTP:', text)

    def test_error_message_preserved_without_extra_redaction_or_truncation(self):
        msg = 'fixture error token=synthetic-test-value '
        msg += 'Z' * 40
        text = feedback.failure_details(403, {'code': '403', 'msg': base64.b64encode(msg.encode()).decode(),
            'token': 'not-an-error-field'})
        self.assertIn(msg, text)
        self.assertIn('code: 403', text)
        self.assertNotIn('not-an-error-field', text)

    def test_local_failure_is_labelled_not_mistaken_for_remote_403(self):
        text = feedback.failure_details(200, {'code': 401, 'subcode': 1}, local=True)
        self.assertIn('本地拦截', text)
        self.assertIn('未发送到认证服务器', text)
        self.assertNotIn('403', text)

    def test_proxy_passes_error_details_and_keeps_existing_logging(self):
        tree = ast.parse((SRC / 'mitm_addon.py').read_text())
        owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IDVLoginAddon')
        fn = next(n for n in owner.body if isinstance(n, ast.FunctionDef) and n.name == '_check_uni_sauth_response')
        manager = Mock()
        namespace = dict(json=json, app_state=types.SimpleNamespace(channels_helper=manager),
            http=types.SimpleNamespace(HTTPFlow=object), getShortGameId=lambda x:x)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'mitm_addon.py', 'exec'), namespace)
        owner = types.SimpleNamespace(logger=Mock(), _pending_tool_auth=set())
        request = dict(gameid='h55', login_channel='oppo', sdkuid='fixture', sessionid='fixture-secret')
        response = types.SimpleNamespace(status_code=200, content=json.dumps({
            'code': 403, 'subcode': 10, 'msg': 'fixture error expired'}).encode())
        flow = types.SimpleNamespace(metadata={}, request=types.SimpleNamespace(content=json.dumps(request).encode()),
                                     response=response)
        namespace['_check_uni_sauth_response'](owner, flow)
        data, success, details = manager.record_sauth.call_args.args
        self.assertEqual(data, request)
        self.assertFalse(success)
        self.assertIn('HTTP: 200', details)
        self.assertIn('code: 403', details)
        self.assertIn('fixture error expired', details)
        self.assertNotIn('fixture-secret', details)
        self.assertNotIn('fixture-secret', str(owner.logger.mock_calls))
        self.assertNotIn('expired', str(owner.logger.mock_calls))


class QtDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        app_state.app = cls.app
        app_state._ensure_invoker()

    def tearDown(self):
        for dialog in list(feedback._dialogs.values()):
            dialog.close()
        QTest.qWait(5)

    def test_default_no_enter_and_escape_never_relogin(self):
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
            confirmed = Mock()
            feedback.confirm_relogin('fixture', 'code: 403', confirmed)
            QTest.qWait(5)
            dialog = feedback._dialogs[('confirm', 'fixture')]
            self.assertIs(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.No))
            self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
            self.assertIn('登录过期，是否重新登录？', dialog.text())
            self.assertIn(30000, [t.interval() for t in dialog.findChildren(QTimer)])
            QTest.keyClick(dialog, key)
            QTest.qWait(5)
            confirmed.assert_not_called()
            if key == Qt.Key.Key_Return:
                self.assertNotIn(('confirm', 'fixture'), feedback._dialogs)
            else:
                dialog.close()

    def test_timeout_and_window_close_do_not_confirm(self):
        confirmed = Mock()
        feedback.show_message('timeout', 'fixture', confirmed, timeout_ms=20)
        QTest.qWait(40)
        self.assertNotIn('timeout', feedback._dialogs)
        confirmed.assert_not_called()
        feedback.show_message('close', 'fixture', confirmed)
        QTest.qWait(5)
        feedback._dialogs['close'].close()
        confirmed.assert_not_called()

    def test_only_yes_confirms_once_and_duplicate_prompt_is_suppressed(self):
        first, duplicate = Mock(), Mock()
        feedback.show_message('same-account', 'fixture', first)
        feedback.show_message('same-account', 'fixture', duplicate)
        QTest.qWait(5)
        self.assertEqual(len(feedback._dialogs), 1)
        feedback._dialogs['same-account'].button(QMessageBox.StandardButton.Yes).click()
        first.assert_called_once()
        duplicate.assert_not_called()
        self.assertEqual(feedback._dialogs, {})

    def test_worker_dispatches_qt_to_main_thread_without_blocking(self):
        thread = threading.Thread(target=lambda: feedback.show_message('thread', 'fixture', Mock()))
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        QTest.qWait(10)
        self.assertEqual(feedback._dialogs['thread'].thread(), self.app.thread())

    def test_recovery_success_has_requested_text_and_auto_close(self):
        feedback.relogin_succeeded()
        QTest.qWait(5)
        dialog = feedback._dialogs['relogin-success']
        self.assertEqual(dialog.text(), '重新登录成功，请你重新打开游戏')
        self.assertIn(15000, [t.interval() for t in dialog.findChildren(QTimer)])
        self.assertEqual(dialog.standardButtons(), QMessageBox.StandardButton.Ok)

    def test_existing_auto_quit_waits_for_success_notice_once(self):
        quit_app = Mock()
        self.assertFalse(feedback.after_relogin_notice(quit_app))
        feedback.relogin_succeeded()
        QTest.qWait(5)
        self.assertTrue(feedback.after_relogin_notice(quit_app))
        self.assertTrue(feedback.after_relogin_notice(quit_app))
        quit_app.assert_not_called()
        feedback._dialogs['relogin-success'].close()
        quit_app.assert_called_once()


if __name__ == '__main__':
    unittest.main()
