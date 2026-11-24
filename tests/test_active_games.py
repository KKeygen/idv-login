"""Offline liveness tests: fabricated requests and PID lists, no game access."""
import ast
import ctypes
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
        with patch.object(active_games, 'running_process_ids') as query:
            self.assertEqual(self.active.running_game_ids(), set())
            query.assert_not_called()

    def test_sdk_reported_pid_presence_is_the_running_oracle(self):
        self.active.note_native_request('h55', '123')
        with patch.object(active_games, 'running_process_ids', return_value={123}) as query:
            self.assertEqual(self.active.running_game_ids(), {'h55'})
            query.assert_called_once_with()

    def test_closed_pid_is_removed(self):
        self.active.note_native_request('h55', 123)
        with patch.object(active_games, 'running_process_ids', return_value=set()):
            self.assertEqual(self.active.running_game_ids(), set())
            self.assertEqual(self.active._native, {})

    def test_multiopen_survives_until_last_known_instance_exits(self):
        for pid in (123, 124):
            self.active.note_native_request('h55', pid)
        with patch.object(active_games, 'running_process_ids', return_value={124}):
            self.assertEqual(self.active.running_game_ids(), {'h55'})
        with patch.object(active_games, 'running_process_ids', return_value=set()):
            self.assertEqual(self.active.running_game_ids(), set())

    def test_query_failure_does_not_assume_alive_or_forget_for_next_retry(self):
        self.active.note_native_request('h55', 123)
        with patch.object(active_games, 'running_process_ids', side_effect=OSError):
            self.assertEqual(self.active.running_game_ids(), set())
        with patch.object(active_games, 'running_process_ids', return_value={123}):
            self.assertEqual(self.active.running_game_ids(), {'h55'})

    def test_latest_sdk_report_controls_game_association(self):
        self.active.note_native_request('h55', 123)
        self.active.note_native_request('g37', 123)
        with patch.object(active_games, 'running_process_ids', return_value={123}):
            self.assertEqual(self.active.running_game_ids(), {'g37'})

    def test_own_hosted_pid_and_missing_identity_are_ignored(self):
        for game, pid in (('h55', os.getpid()), ('h55', ''), ('h55', 0), ('', 123)):
            self.active.note_native_request(game, pid)
        self.assertEqual(self.active._native, {})

    def test_new_game_request_arriving_during_query_is_not_deleted(self):
        self.active.note_native_request('h55', 123)
        def snapshot():
            self.active.note_native_request('g37', 123)
            return set()
        with patch.object(active_games, 'running_process_ids', side_effect=snapshot):
            self.assertEqual(self.active.running_game_ids(), set())
        self.assertEqual(self.active._native[123], 'g37')

    def test_enum_processes_only_returns_ids_and_grows_a_full_buffer(self):
        def enum(buffer, size, needed):
            count = ctypes.cast(needed, ctypes.POINTER(ctypes.c_uint32))
            if size == 4096:
                count.contents.value = size
            else:
                buffer[0], buffer[1] = 123, 124
                count.contents.value = 8
            return 1
        function = Mock(side_effect=enum)
        loader = Mock(return_value=types.SimpleNamespace(EnumProcesses=function))
        with patch.object(active_games.ctypes, 'WinDLL', loader, create=True), patch.object(active_games.sys, 'platform', 'win32'):
            self.assertEqual(active_games.running_process_ids(), {123, 124})
        loader.assert_called_once_with('psapi', use_last_error=True)
        self.assertEqual(function.call_count, 2)

    def test_enum_failure_raises_without_opening_a_process(self):
        loader = Mock(return_value=types.SimpleNamespace(EnumProcesses=Mock(return_value=0)))
        with patch.object(active_games.ctypes, 'WinDLL', loader, create=True), \
                patch.object(active_games.ctypes, 'WinError', return_value=OSError(), create=True), \
                patch.object(active_games.ctypes, 'get_last_error', return_value=5, create=True), \
                patch.object(active_games.sys, 'platform', 'win32'):
            with self.assertRaises(OSError):
                active_games.running_process_ids()

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
        manager = Mock()
        namespace = dict(http=types.SimpleNamespace(HTTPFlow=object),
            ROLE_NATIVE_GAME='native', ROLE_BRIDGED_GAME='bridged', ROLE_HOSTED_FEVER_MPAY='hosted',
            getShortGameId=lambda game: game.split('-g-')[-1], _request_values=lambda request: request.values)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'mitm_addon.py', 'exec'), namespace)
        owner = types.SimpleNamespace(target_domains={'mpay.example'}, oversea_domain='oversea.example',
            _remember_hosted_request=Mock(), game_helper=manager)
        request = types.SimpleNamespace(pretty_host='mpay.example', path='/mpay/api/qrcode/image',
            values={'game_id': 'fixture-g-h55', 'process_id': '123'})
        for role in ('native', 'hosted', 'bridged', 'real_fever'):
            owner._classify_mpay_request = lambda flow, role=role: role
            owner._re_device_users_post = Mock(match=lambda path: False)
            owner._re_handle_login = Mock(match=lambda path: False)
            namespace['request'](owner, types.SimpleNamespace(request=request))
        manager.note_sdk_process.assert_called_once_with('fixture-g-h55', '123')

    def test_launcher_api_exposes_shared_game_running_attribute(self):
        tree = ast.parse((SRC / 'local_handler.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and any(isinstance(method, ast.FunctionDef) and method.name == '_launcher_status' for method in node.body))
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_launcher_status')
        game = types.SimpleNamespace(is_running=False, installations={},
            get_distribution_options=lambda: [], _normalize_distribution_ids=lambda options: [],
            get_default_distribution=lambda: -1, get_installation=lambda: None)
        manager = types.SimpleNamespace(update_running_state=lambda: setattr(game, 'is_running', True),
            get_existing_game=lambda gid: game, get_game_or_temp=lambda gid: game, list_fever_games=lambda: [])
        namespace = dict(getShortGameId=lambda value: value,
            CloudRes=lambda: types.SimpleNamespace(dynamic_game_catalog=types.SimpleNamespace(get_game=lambda gid: {})),
            safe_exception=lambda *args: None)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'local_handler.py', 'exec'), namespace)
        owner = types.SimpleNamespace(game_helper=manager, logger=Mock(),
            _pick_launcher_fields=lambda fields: {}, _json_response=lambda status, body: body)
        with patch.dict(sys.modules, {'nativebridge': types.SimpleNamespace(NativeTaskRegistry=object),
                                      'gamemgr': types.SimpleNamespace(GameInstallation=object)}):
            result = namespace['_launcher_status'](owner, {'game_id': 'h55'}, None, 'GET')
        self.assertTrue(result['success'])
        self.assertTrue(result['game']['is_running'])


if __name__ == '__main__':
    unittest.main()
