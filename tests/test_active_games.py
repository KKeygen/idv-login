"""Offline liveness tests: fabricated requests and WMI metadata, no game access."""
import ast
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

SRC = Path(__file__).resolve().parents[1] / 'src'
spec = importlib.util.spec_from_file_location('active_games', SRC / 'active_games.py')
active_games = importlib.util.module_from_spec(spec)
spec.loader.exec_module(active_games)


class ActiveGamesTests(unittest.TestCase):
    def setUp(self):
        self.active = active_games.ActiveGames(Mock())

    def test_no_request_means_no_game_and_no_system_query(self):
        with patch.object(active_games, 'process_creation_times') as query:
            self.assertEqual(self.active.running_game_ids(), set())
            query.assert_not_called()

    def test_known_pid_created_before_sdk_request_is_running(self):
        self.active.note_native_request('h55', '123', 100)
        with patch.object(active_games, 'process_creation_times', return_value={123: 90}) as query:
            self.assertEqual(self.active.running_game_ids(), {'h55'})
            query.assert_called_once_with({123})

    def test_closed_or_reused_pid_is_removed(self):
        for metadata in ({}, {123: 101}):
            self.active.note_native_request('h55', 123, 100)
            with patch.object(active_games, 'process_creation_times', return_value=metadata):
                self.assertEqual(self.active.running_game_ids(), set())
                self.assertEqual(self.active._native, {})

    def test_multiopen_survives_until_last_known_instance_exits(self):
        for pid in (123, 124):
            self.active.note_native_request('h55', pid, 100)
        with patch.object(active_games, 'process_creation_times', return_value={124: 90}):
            self.assertEqual(self.active.running_game_ids(), {'h55'})
        with patch.object(active_games, 'process_creation_times', return_value={}):
            self.assertEqual(self.active.running_game_ids(), set())

    def test_query_failure_does_not_assume_alive_or_forget_for_next_retry(self):
        self.active.note_native_request('h55', 123, 100)
        with patch.object(active_games, 'process_creation_times', side_effect=OSError):
            self.assertEqual(self.active.running_game_ids(), set())
        with patch.object(active_games, 'process_creation_times', return_value={123: 90}):
            self.assertEqual(self.active.running_game_ids(), {'h55'})

    def test_new_request_for_reused_pid_identifies_new_real_game(self):
        self.active.note_native_request('h55', 123, 100)
        self.active.note_native_request('g37', 123, 200)
        with patch.object(active_games, 'process_creation_times', return_value={123: 150}):
            self.assertEqual(self.active.running_game_ids(), {'g37'})

    def test_own_hosted_pid_and_missing_identity_are_ignored(self):
        for game, pid in (('h55', os.getpid()), ('h55', ''), ('h55', 0), ('', 123)):
            self.active.note_native_request(game, pid, 100)
        self.assertEqual(self.active._native, {})

    def test_request_arriving_during_query_is_not_deleted(self):
        self.active.note_native_request('h55', 123, 100)
        def snapshot(pids):
            self.active.note_native_request('h55', 123, 200)
            return {123: 150}
        with patch.object(active_games, 'process_creation_times', side_effect=snapshot):
            self.assertEqual(self.active.running_game_ids(), set())
        self.assertEqual(self.active._native[('h55', 123)], 200)

    def test_wmi_timestamp_has_explicit_timezone_and_rejects_unknown(self):
        expected = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(active_games._creation_time('20261116080000.000000+480'), expected)
        self.assertEqual(active_games._creation_time('20261115200000.000000-240'), expected)
        with self.assertRaises(ValueError):
            active_games._creation_time('20261116080000.******+***')

    def test_wmi_only_requests_pid_and_creation_date_using_existing_com_dependency(self):
        com = types.ModuleType('comtypes')
        com.CoInitialize = Mock()
        com.CoUninitialize = Mock()
        client = types.ModuleType('comtypes.client')
        service = Mock()
        service.ExecQuery.return_value = [types.SimpleNamespace(
            ProcessId=123, CreationDate='20261116000000.000000+000')]
        locator = Mock()
        locator.ConnectServer.return_value = service
        client.CreateObject = Mock(return_value=locator)
        with patch.dict(sys.modules, {'comtypes': com, 'comtypes.client': client}), patch.object(active_games.sys, 'platform', 'win32'):
            result = active_games.process_creation_times({123})
        self.assertEqual(set(result), {123})
        service.ExecQuery.assert_called_once_with(
            'SELECT ProcessId, CreationDate FROM Win32_Process WHERE ProcessId=123')
        com.CoUninitialize.assert_called_once()

    def test_fever_uses_live_active_and_consumed_sessions_not_launch_intent(self):
        tree = ast.parse((SRC / 'fever_bridge.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'FeverBridge')
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('_registered_sessions_locked', 'running_game_ids')]
        namespace = {}
        exec(compile(ast.Module(body=methods, type_ignores=[]), 'fever_bridge.py', 'exec'), namespace)
        bridge = types.SimpleNamespace(_lock=threading.RLock(),
            _sessions={'a': {'short_game_id': 'h55', 'alive': False}},
            _consumed_sessions={'b': {'short_game_id': 'g37', 'alive': True}},
            _bridge_launch_intents={'h55', 'g37', 'never_started'},
            _is_session_sender_alive=lambda session: session['alive'])
        bridge._registered_sessions_locked = types.MethodType(namespace['_registered_sessions_locked'], bridge)
        self.assertEqual(namespace['running_game_ids'](bridge), {'g37'})

    def test_proxy_registers_only_native_requests_before_mutating_them(self):
        tree = ast.parse((SRC / 'mitm_addon.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and any(isinstance(method, ast.FunctionDef) and method.name == 'request' for method in node.body))
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'request')
        active = Mock()
        namespace = dict(http=types.SimpleNamespace(HTTPFlow=object),
            app_state=types.SimpleNamespace(channels_helper=types.SimpleNamespace(active_games=active)),
            ROLE_NATIVE_GAME='native', ROLE_BRIDGED_GAME='bridged', ROLE_HOSTED_FEVER_MPAY='hosted',
            getShortGameId=lambda game: game.split('-g-')[-1], _request_values=lambda request: request.values)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'mitm_addon.py', 'exec'), namespace)
        owner = types.SimpleNamespace(target_domains={'mpay.example'}, oversea_domain='oversea.example',
            _remember_hosted_request=Mock())
        request = types.SimpleNamespace(pretty_host='mpay.example', path='/mpay/api/qrcode/image',
            values={'game_id': 'fixture-g-h55', 'process_id': '123'}, timestamp_start=100)
        for role in ('native', 'hosted', 'bridged', 'real_fever'):
            owner._classify_mpay_request = lambda flow, role=role: role
            owner._re_device_users_post = Mock(match=lambda path: False)
            owner._re_handle_login = Mock(match=lambda path: False)
            namespace['request'](owner, types.SimpleNamespace(request=request))
        active.note_native_request.assert_called_once_with('h55', '123', 100)


if __name__ == '__main__':
    unittest.main()
