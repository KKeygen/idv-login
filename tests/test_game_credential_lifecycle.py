"""Offline credential lifecycle tests; no Qt, game process, DB or channel network."""
import ast
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import random
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import requests

SRC = Path(__file__).resolve().parents[1] / 'src'


def packet(channel='huawei', game='h55', token='game-token', uid='account'):
    auth = dict(gameid=game, login_channel=channel, sdkuid=uid, sessionid=token,
                timestamp=str(int(time.time() * 1000)))
    return dict(user_id=uid, token=base64.b64encode(token.encode()).decode(),
                login_channel=channel, app_channel=channel, pay_channel=channel,
                udid='fixture-device', sdk_version='fixture-sdk', jf_game_id=game,
                extra_unisdk_data=json.dumps({
                    'SAUTH_JSON': base64.b64encode(json.dumps(auth).encode()).decode(),
                    'SAUTH_STR': 'fixture-complete-signed-packet'}))


def source_classes(filename, names, namespace):
    tree = ast.parse((SRC / filename).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return [namespace[name] for name in names]


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = types.ModuleType('app_state')
        self.app.toast = Mock()
        self.app.run_on_main_thread = lambda callback: callback()
        self.app.ui_mgr = Mock()
        self.app.fever_bridge = None
        utils = types.ModuleType('channelHandler.channelUtils')
        utils.getShortGameId = lambda game: game.split('-g-')[-1]
        utils.cmp_game_id = lambda a, b: utils.getShortGameId(a) == utils.getShortGameId(b)
        self.modules = patch.dict(sys.modules, {
            'app_state': self.app, 'channelHandler.channelUtils': utils,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        for name in ('channel_cache', 'prefetch_context', 'sauth_feedback'):
            spec = importlib.util.spec_from_file_location(name, SRC / f'{name}.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[name] = module
            setattr(self, name, module)
        self.sauth_feedback.confirm_relogin = Mock()
        self.sauth_feedback.show_message = Mock()
        self.sauth_feedback.relogin_succeeded = Mock()
        self.logger = Mock()
        self.namespace = dict(os=os, copy=copy, json=json, random=random, time=time,
                              requests=requests, threading=threading,
                              genv=Mock(), setup_logger=lambda: self.logger,
                              getShortGameId=utils.getShortGameId, cmp_game_id=utils.cmp_game_id,
                              **{name: getattr(self.channel_cache, name) for name in (
                                  'packet_auth', 'packet_identity', 'credential_expires_at',
                                  'exception_summary', 'unisdk_expires_at')})
        self.Base, self.Manager = source_classes('channelmgr.py', ('channel', 'ChannelManager'), self.namespace)
        base = self.Base

        class Fixture(base):
            def __init__(record, channel='huawei'):
                super().__init__({'login_channel': channel, 'code': 'fixture'})
                record.game_id = 'h55'
                record.builds = []
                record.fail = False

            def get_uniSdk_data(record, game_id='', on_complete=None, *, interactive=True):
                record.builds.append(game_id)
                result = False if record.fail else packet(record.channel_name, game_id, f'new-{len(record.builds)}')
                if on_complete:
                    on_complete(result)
                    return None
                return result

        self.Record = Fixture
        self.manager = object.__new__(self.Manager)
        self.manager.channels = []
        self.manager.logger = self.logger
        self.manager.db_sync = None
        self.manager.save_records = Mock()
        self.manager._credential_stop = threading.Event()
        self.manager._credential_thread = None
        self.manager._credential_lock = threading.RLock()
        self.manager.game_helper = types.SimpleNamespace(update_running_state=Mock(),
            games={'h55': types.SimpleNamespace(game_id='h55', is_running=True)})
        policy = types.ModuleType('account_list_policy')
        policy.AccountListPolicy = lambda manager: types.SimpleNamespace(
            select=lambda games: {game: list(manager.channels) for game in games},
            canonical_uuid=lambda *_: None, include_logged_in=Mock())
        policy.native_scan_channels = lambda *_: set()
        sys.modules['account_list_policy'] = policy

    def record(self, channel='huawei', *, expires=None):
        record = self.Record(channel)
        record.cache_unisdk(packet(channel), expires)
        self.manager.channels.append(record)
        return record

    def auth(self, record, game='h55'):
        return self.channel_cache.packet_auth(record.unisdk_cache[game]['packet'])

    def test_known_fresh_b_reused_without_issuing_or_refreshing_a(self):
        record = self.record(expires=time.time() + 200)
        first = record.request_unisdk('h55')
        second = self.manager.prepare_game_credential(record, 'h55')
        self.assertEqual(first, second)
        self.assertEqual(record.builds, [])

    def test_unknown_renewable_b_issues_but_direct_token_unknown_reuses(self):
        for name in ('oppo', 'nearme_vivo', 'xiaomi_app', 'baidu', '4399com'):
            record = self.record(name)
            record.request_unisdk('h55', interactive=False)
            self.assertEqual(record.builds, ['h55'], name)
        for name in ('bilibili_sdk', 'guopan', 'myapp'):
            record = self.record(name)
            record.request_unisdk('h55')
            self.assertEqual(record.builds, [], name)

    def test_unknown_packet_remains_available_for_complete_db_projection(self):
        record = self.record('nearme_vivo')
        self.assertIsNone(record.cached_unisdk('h55', for_login=True))
        self.assertIsNotNone(record.cached_unisdk('h55'))
        self.assertIsNone(self.channel_cache.unisdk_expires_at(record.unisdk_cache['h55']))

    def test_near_expiry_and_explicit_force_issue(self):
        record = self.record(expires=time.time() + 2)
        record.request_unisdk('h55')
        record.request_unisdk('h55', force=True)
        self.assertEqual(record.builds, ['h55', 'h55'])

    def test_failure_only_expires_requested_game_even_without_previous_b(self):
        record = self.record('oppo', expires=time.time() + 200)
        record.fail = True
        self.assertIsNone(self.manager.prepare_game_credential(record, 'g37'))
        self.assertTrue(record.is_login_expired('g37'))
        self.assertFalse(record.is_login_expired('h55'))
        self.app.toast.assert_not_called()
        self.manager.save_records.assert_called()
        self.assertEqual(self.app.ui_mgr.method_calls, [])

    def test_startup_preparation_never_requests_interactive_relogin(self):
        record = self.record('oppo')
        record.request_unisdk = Mock(return_value=False)
        self.assertIsNone(self.manager.prepare_game_credential(record, 'h55'))
        record.request_unisdk.assert_called_once_with('h55', interactive=False, force=False)
        self.assertTrue(record.is_login_expired('h55'))
        self.assertEqual(self.app.ui_mgr.method_calls, [])
        self.app.toast.assert_not_called()

    def test_explicit_failure_cleared_only_by_new_b_for_same_game(self):
        record = self.record()
        record.expire_unisdk('h55')
        record.expire_unisdk('g37')
        record.request_unisdk('h55', interactive=False)
        self.assertEqual(record.expired_games, ['g37'])

    def test_cancel_does_not_expire_game(self):
        record = self.record()
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(record.request_unisdk('h55', interactive=False, cancel_event=cancel))
        self.assertFalse(record.is_login_expired('h55'))

    def test_silent_issue_cannot_switch_sdk_account(self):
        record = self.record()
        def issue(*args, **kwargs):
            record.observe_sdkuid('different-account', 'h55')
            return packet(uid='different-account')
        record.get_uniSdk_data = issue
        self.assertIsNone(self.manager.prepare_game_credential(record, 'h55', force=True))
        self.assertEqual(self.auth(record)['sdkuid'], 'account')
        self.assertTrue(record.is_login_expired('h55'))

    def test_sauth_matches_exact_saved_request_token_for_every_channel(self):
        for name in ('huawei', 'nearme_vivo', 'oppo', 'bilibili_sdk'):
            record = self.record(name)
            current = self.auth(record)
            self.assertEqual(self.manager._sauth_matches(current, 'h55'), [record])
            self.assertEqual(self.manager._sauth_matches(dict(current, sessionid='old'), 'h55'), [])

    def test_huawei_signed_timestamp_is_part_of_same_credential(self):
        record = self.record()
        self.assertEqual(self.manager._sauth_matches(dict(self.auth(record), timestamp='1'), 'h55'), [])

    def test_lenovo_uid_zero_does_not_attribute_unknown_request(self):
        record = self.record('lenovo_open')
        record.cache_unisdk(packet('lenovo_open', uid='0'))
        self.manager.db_sync = Mock()
        self.assertFalse(self.manager.record_sauth(dict(self.auth(record), sessionid='other-account'), False))
        self.manager.db_sync.refresh_record.assert_not_called()

    def test_old_sauth_failure_does_not_expire_new_b_or_write_until_confirmation(self):
        record = self.record()
        self.manager.db_sync = Mock()
        self.assertTrue(self.manager.record_sauth(dict(self.auth(record), sessionid='old'), False))
        self.assertFalse(record.is_login_expired('h55'))
        self.manager.db_sync.refresh_record.assert_not_called()
        self.assertIn('账号已更新', self.sauth_feedback.confirm_relogin.call_args.kwargs['note'])
        self.sauth_feedback.confirm_relogin.call_args.args[2]()
        self.manager.db_sync.refresh_record.assert_called_once_with(record, 'h55')
        self.app.toast.assert_not_called()

    def test_first_current_sauth_failure_confirms_before_any_recovery(self):
        record = self.record()
        with patch.object(threading, 'Thread') as worker:
            self.assertTrue(self.manager.record_sauth(self.auth(record), False, 'code: 403\nfixture error'))
            worker.assert_not_called()
            self.assertEqual(record.builds, [])
            self.sauth_feedback.confirm_relogin.call_args.args[2]()
        self.assertTrue(record.is_login_expired('h55'))
        self.assertIn('403', self.sauth_feedback.confirm_relogin.call_args.args[1])
        self.app.toast.assert_not_called()
        worker.return_value.start.assert_called_once()

    def test_periodic_refresh_is_four_minutes_and_only_saved_huawei_vivo_games(self):
        hua, vivo = self.record(), self.record('nearme_vivo')
        other = self.record('oppo')
        self.manager.prepare_game_credential = Mock(return_value=packet())
        self.manager.refresh_short_lived_credentials()
        self.assertEqual(self.manager.prepare_game_credential.call_args_list, [
            unittest.mock.call(hua, 'h55', force=True), unittest.mock.call(vivo, 'h55', force=True)])
        self.assertEqual(other.builds, [])
        stop = self.manager._credential_stop = Mock()
        stop.wait.return_value = True
        with patch.object(threading, 'Thread') as worker:
            self.manager.start_credential_refresh()
            worker.call_args.kwargs['target']()
        stop.wait.assert_called_once_with(240)

    def test_periodic_refresh_can_recover_previously_failed_game(self):
        record = self.record()
        record.expire_unisdk('h55')
        self.manager.refresh_short_lived_credentials()
        self.assertEqual(record.builds, ['h55'])
        self.assertFalse(record.is_login_expired('h55'))
        self.app.toast.assert_not_called()

    def test_many_background_failures_only_mark_and_rename_without_toasts_or_sauth(self):
        records = [self.record(), self.record('nearme_vivo')]
        for record in records:
            record.fail = True
        self.manager.db_sync = Mock()
        self.manager.record_sauth = Mock()
        self.manager.refresh_short_lived_credentials()
        self.manager.refresh_short_lived_credentials()
        self.assertTrue(all(record.is_login_expired('h55') for record in records))
        self.assertEqual(self.manager.db_sync.rename_record.call_count, 4)
        self.manager.record_sauth.assert_not_called()
        self.app.toast.assert_not_called()
        self.assertEqual(self.app.ui_mgr.method_calls, [])

    def test_background_database_failure_stays_silent(self):
        self.record()
        self.manager.db_sync = Mock()
        self.manager.db_sync.refresh_record.side_effect = OSError('fixture locked')
        self.manager.refresh_short_lived_credentials()
        self.app.toast.assert_not_called()

    def test_issuance_and_save_exceptions_do_not_stop_other_background_accounts(self):
        failed, healthy = self.record(), self.record('nearme_vivo')
        failed.get_uniSdk_data = Mock(side_effect=ValueError('fixture issuance failure'))
        self.manager.save_records.side_effect = [OSError('fixture save failure'), None]
        self.manager.db_sync = Mock()
        self.manager.refresh_short_lived_credentials()
        self.assertTrue(failed.is_login_expired('h55'))
        self.assertEqual(healthy.builds, ['h55'])
        self.manager.db_sync.rename_record.assert_called_once_with(failed.uuid, failed.name)
        self.manager.db_sync.refresh_record.assert_called_once_with(healthy, 'h55')
        self.app.toast.assert_not_called()
        self.sauth_feedback.confirm_relogin.assert_not_called()

    def test_selected_invalid_native_account_prompts_on_attempt_without_server_403(self):
        record = self.record()
        auth = self.auth(record)
        record.fail = True
        self.manager.prepare_game_credential(record, 'h55', force=True)
        self.app.toast.assert_not_called()
        Sync, = source_classes('mpay_db_sync.py', ('MpayDBSync',), dict(self.namespace))
        sync = object.__new__(Sync)
        sync.manager = self.manager
        sync.projected = {('fixture.db', ('h55', 'huawei', 'account'), False)}
        self.manager.confirm_sauth_relogin = Mock()
        self.manager.db_sync = sync
        # Merely determining the action must not display anything yet.
        self.assertTrue(callable(sync.intercept_auth(auth)))
        self.manager.confirm_sauth_relogin.assert_not_called()
        self.app.toast.assert_not_called()

        class Response:
            @staticmethod
            def make(status, content, headers):
                return types.SimpleNamespace(status_code=status, content=content, headers=headers)

        self.app.channels_helper = self.manager
        namespace = dict(self.namespace, app_state=self.app,
                         http=types.SimpleNamespace(HTTPFlow=object, Response=Response))
        tree = ast.parse((SRC / 'mitm_addon.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'IDVLoginAddon')
        methods = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('request', '_cancel_login', '_check_uni_sauth_response')]
        for method in methods:
            method.decorator_list = []
        exec(compile(ast.Module(body=methods, type_ignores=[]), 'mitm_addon.py', 'exec'), namespace)
        owner = types.SimpleNamespace(target_domains={'auth.example'}, auth_status_domain='auth.example',
            _pending_tool_auth=set(), _cancel_login=namespace['_cancel_login'])
        flow = types.SimpleNamespace(metadata={}, response=None, request=types.SimpleNamespace(
            pretty_host='auth.example', path='/sdk/uni_sauth', content=json.dumps(auth).encode()))
        namespace['request'](owner, flow)
        self.assertEqual(flow.response.status_code, 200)
        self.assertEqual(json.loads(flow.response.content)['code'], 401)
        self.assertTrue(flow.metadata['idv_local_auth_cancel'])
        self.manager.confirm_sauth_relogin.assert_called_once()
        self.assertEqual(self.manager.confirm_sauth_relogin.call_args.args[0], auth)
        self.assertIn('登录已过期', self.manager.confirm_sauth_relogin.call_args.args[1])
        self.assertIn('code: 401', self.manager.confirm_sauth_relogin.call_args.args[1])
        self.app.toast.assert_not_called()
        self.manager.record_sauth = Mock()
        namespace['_check_uni_sauth_response'](owner, flow)
        self.manager.record_sauth.assert_not_called()
        self.manager.confirm_sauth_relogin.assert_called_once()
        self.app.toast.assert_not_called()

    def test_native_account_label_marks_invalid_without_changing_saved_name(self):
        record = self.record()
        record.name = 'fixture user'
        Policy, = source_classes('account_list_policy.py', ('AccountListPolicy',),
                                 dict(self.namespace, re=__import__('re')))
        policy = object.__new__(Policy)
        policy.manager = types.SimpleNamespace(_manual_channel_name=lambda *_: 'huawei')
        self.assertEqual(policy.account_label(record, expired=True), '华为 fixture user（已过期）')
        self.assertEqual(record.name, 'fixture user')

    def test_periodic_refresh_skips_closed_games_and_unselected_accounts(self):
        chosen, unselected = self.record(), self.record('nearme_vivo')
        chosen.cache_unisdk(packet(game='g37'))
        self.manager.prepare_game_credential = Mock(return_value=packet())
        self.manager.game_helper.games['h55'].is_running = False
        self.manager.refresh_short_lived_credentials()
        self.manager.prepare_game_credential.assert_not_called()
        self.manager.game_helper.games['h55'].is_running = True
        policy = sys.modules['account_list_policy']
        select = Mock(return_value={'h55': [chosen]})
        policy.AccountListPolicy = lambda manager: types.SimpleNamespace(select=select)
        self.manager.refresh_short_lived_credentials()
        select.assert_called_once_with({'h55'})
        self.manager.prepare_game_credential.assert_called_once_with(chosen, 'h55', force=True)
        self.assertEqual(unselected.builds, [])

    def test_periodic_refresh_reads_game_model_but_skips_missing_b(self):
        chosen = self.record()
        self.manager.game_helper.games['g37'] = types.SimpleNamespace(game_id='g37', is_running=True)
        self.manager.prepare_game_credential = Mock(return_value=packet())
        self.manager.refresh_short_lived_credentials()
        self.manager.prepare_game_credential.assert_called_once_with(chosen, 'h55', force=True)

    def test_saved_expiry_state_and_service_deadline_survive_restore(self):
        record = self.record('oppo', expires=time.time() + 200)
        record.expire_unisdk('g37')
        restored = self.Record('oppo').restore_common(json.loads(json.dumps(vars(record))))
        self.assertTrue(restored.is_login_expired('g37'))
        self.assertFalse(restored.is_login_expired('h55'))
        self.assertIsNotNone(restored.cached_unisdk('h55', for_login=True))

    def test_lenovo_b_deadline_comes_from_issued_st_not_tgt(self):
        record = self.Record('lenovo_open')
        record.lenovoSession = dict(st_issued_at=100, st_ttl=20, tgt_issued_at=100, tgt_ttl=1000)
        self.assertEqual(self.channel_cache.credential_expires_at(record), 120)

    def test_native_write_projects_whole_packet_and_failure_only_renames(self):
        namespace = dict(self.namespace, Path=Path, base64=base64)
        Sync, credentials = source_classes('mpay_db_sync.py', ('MpayDBSync', 'credentials_from_packet'), namespace)
        policy = types.ModuleType('account_list_policy')
        policy.AccountListPolicy = lambda manager: types.SimpleNamespace(
            account_label=lambda record, expired=False: record.name + ('[失效]' if expired else ''))
        sync = object.__new__(Sync)
        sync.manager = self.manager
        sync.identity = 'fixture-machine'
        sync.projected = set()
        sync.operate = Mock(return_value=True)
        sync._remember_put = lambda path, key, created: sync.projected.add((path, key, created))
        record = self.record('oppo')
        path = Path('fixture-mpay.db')
        with patch.dict(sys.modules, {'account_list_policy': policy}):
            key = sync._write_record(path, record, 'h55', 'preserve')
            args = sync.operate.call_args.kwargs
            self.assertEqual(args['credentials'], credentials(record.unisdk_cache['h55']['packet'], 'h55')[1])
            self.assertEqual(args['credentials']['pc_ext_info']['src_app_channel'], 'oppo')
            self.assertEqual(args['credentials']['pc_ext_info']['src_pay_channel'], 'oppo')
            self.assertEqual(args['create']['login_channel'], 'oppo')
            record.expire_unisdk('h55')
            sync.operate.reset_mock()
            self.assertIsNone(sync._write_record(path, record, 'h55', 'preserve'))
            self.assertEqual(sync.operate.call_args.args, (path, 'rename_account'))
            self.assertIn('失效', sync.operate.call_args.kwargs['name'])
            self.assertIn((path, key, True), sync.projected)
            # A new process has no projection tracking but must still mark the
            # existing native row expired, without inserting stale credentials.
            sync.projected.clear()
            sync.operate.reset_mock()
            sync.operate.side_effect = [
                {'accounts': [dict(game_id=key[0], login_channel=key[1], sdkuid=key[2])], 'errors': []},
                False,
            ]
            self.assertIsNone(sync._write_record(path, record, 'h55', 'preserve'))
            self.assertEqual([call.args[1] for call in sync.operate.call_args_list], ['list_accounts', 'rename_account'])
            self.assertIn((path, key, False), sync.projected)

    def test_web_issue_failure_updates_persisted_state_native_label_and_toast(self):
        record = self.record()
        self.manager.db_sync = Mock()
        self.manager._credential_failure(record, 'h55', 'test', ValueError('fixture'))
        self.assertTrue(record.is_login_expired('h55'))
        self.manager.save_records.assert_called_once()
        self.manager.db_sync.rename_record.assert_called_once_with(record.uuid, record.name)
        self.assertIn('已标为过期', self.app.toast.call_args.args[0])

    def test_startup_and_launch_use_identical_shared_preparation(self):
        namespace = dict(self.namespace, Path=Path)
        Sync, = source_classes('mpay_db_sync.py', ('MpayDBSync',), namespace)
        sync = object.__new__(Sync)
        sync.manager = self.manager
        sync.stopping = False
        record = self.record(expires=time.time() + 200)
        sync._prepare_selected({'h55': [record]})
        sync._prepare_selected({'h55': [record]})
        self.assertEqual(record.builds, [])

    def test_late_sauth_recovery_cannot_issue_over_newer_saved_token(self):
        record = self.record()
        old = self.auth(record)
        record.cache_unisdk(packet(token='newer'))
        self.manager._recover_sauth(record, 'h55', old)
        self.assertEqual(record.builds, [])
        self.assertEqual(self.auth(record)['sessionid'], 'newer')

    def test_recovery_reports_written_database_and_native_ui_guidance(self):
        record = self.record()
        old = self.auth(record)
        record.expire_unisdk('h55')
        self.manager.db_sync = Mock()
        self.manager.db_sync.refresh_record.return_value = 1
        self.manager._recover_sauth(record, 'h55', old)
        self.assertFalse(record.is_login_expired('h55'))
        self.manager.db_sync.refresh_record.assert_called_once_with(record, 'h55')
        self.sauth_feedback.relogin_succeeded.assert_called_once()
        self.app.toast.assert_not_called()

    def test_confirming_old_prompt_cannot_relogin_updated_or_deleted_record(self):
        for delete in (False, True):
            record = self.record()
            self.manager.confirm_sauth_relogin(self.auth(record), 'code: 403')
            confirm = self.sauth_feedback.confirm_relogin.call_args.args[2]
            if delete:
                self.manager.channels.remove(record)
            else:
                record.cache_unisdk(packet(token='newer'))
            with patch.object(threading, 'Thread') as worker:
                confirm()
                worker.assert_not_called()
            self.assertEqual(record.builds, [])

    def test_unknown_request_opens_management_only_after_confirmation_without_guessing(self):
        record = self.record('lenovo_open')
        record.cache_unisdk(packet('lenovo_open', uid='0'))
        self.manager.confirm_sauth_relogin(dict(self.auth(record), sessionid='unrelated'))
        self.assertEqual(self.app.ui_mgr.method_calls, [])
        self.sauth_feedback.confirm_relogin.call_args.args[2]()
        self.app.ui_mgr.open_for_game.assert_called_once_with('h55', 'accounts')
        self.assertFalse(record.is_login_expired('h55'))
        self.assertEqual(record.builds, [])

    def test_scan_source_guides_native_scan_and_waits_for_matching_success(self):
        saved = packet('oppo')
        record = self.Base({'login_channel': 'oppo', 'code': 'scan-fixture'},
            user_info={'id': 'mpay-fixture', 'token': 'fixture', 'login_channel': 'oppo',
                       'pc_ext_info': {'extra_unisdk_data': saved['extra_unisdk_data']}},
            ext_info={'from_game_id': 'h55'})
        self.manager.channels.append(record)
        auth = self.channel_cache.packet_auth(saved)
        self.assertTrue(self.manager.record_sauth(auth, False))
        self.assertNotIn(record.sdk_identity('h55'), getattr(self.manager, '_scan_relogins', set()))
        self.assertIn('扫码', self.sauth_feedback.confirm_relogin.call_args.kwargs['note'])
        with patch.object(threading, 'Thread') as worker:
            self.sauth_feedback.confirm_relogin.call_args.args[2]()
            worker.assert_not_called()
        self.assertEqual(self.app.ui_mgr.method_calls, [])
        self.assertIn('游戏登录窗口重新扫码', self.sauth_feedback.show_message.call_args.args[1])
        self.sauth_feedback.relogin_succeeded.assert_not_called()
        self.assertFalse(self.manager.record_sauth(dict(auth, sdkuid='different'), True))
        self.sauth_feedback.relogin_succeeded.assert_not_called()
        self.assertTrue(self.manager.record_sauth(auth, True))
        self.sauth_feedback.relogin_succeeded.assert_called_once()
        self.assertEqual(self.manager._scan_relogins, set())

    def test_scan_exchange_updates_same_record_but_success_waits_for_sauth(self):
        old = packet('oppo')
        record = self.Base({'login_channel': 'oppo', 'code': 'scan-fixture'},
            user_info={'id': 'mpay-fixture', 'token': 'fixture', 'login_channel': 'oppo',
                       'pc_ext_info': {'extra_unisdk_data': old['extra_unisdk_data']}},
            ext_info={'from_game_id': 'h55'}, name='saved name')
        self.manager.channels.append(record)
        self.manager._scan_relogins = {record.sdk_identity('h55')}
        self.manager._manual_channel_name = Mock(return_value='')
        self.manager.db_sync = Mock()
        new = packet('oppo', token='rescan-token')
        exchange = {'user': {'id': 'new-mpay-id', 'login_channel': 'oppo', 'token': new['token'],
                           'pc_ext_info': {'extra_unisdk_data': new['extra_unisdk_data']}}}
        self.assertTrue(self.manager.import_from_scan({'login_channel': 'oppo', 'code': 'new-scan'}, exchange, 'h55'))
        replacement = self.manager.channels[0]
        self.assertIsNot(replacement, record)
        self.assertEqual((replacement.uuid, replacement.name), (record.uuid, record.name))
        self.manager.db_sync.refresh_record.assert_called_once_with(replacement, 'h55')
        self.sauth_feedback.relogin_succeeded.assert_not_called()
        self.manager.record_sauth(self.channel_cache.packet_auth(old), True)
        self.sauth_feedback.relogin_succeeded.assert_not_called()
        self.manager.record_sauth(self.channel_cache.packet_auth(new), True)
        self.sauth_feedback.relogin_succeeded.assert_called_once()

    def test_recovery_database_failure_does_not_claim_success(self):
        record = self.record()
        old = self.auth(record)
        record.expire_unisdk('h55')
        self.manager.db_sync = Mock()
        self.manager.db_sync.refresh_record.side_effect = OSError('fixture')
        self.manager._recover_sauth(record, 'h55', old)
        self.sauth_feedback.relogin_succeeded.assert_not_called()

    def test_confirmed_manual_failure_targets_web_relogin_not_generic_accounts(self):
        record = self.record()
        record.fail = True
        record.expire_unisdk('h55')
        self.manager.db_sync = Mock()
        self.manager._recover_sauth(record, 'h55', self.auth(record))
        self.manager.db_sync._relogin_expired.assert_called_once_with(record, 'h55', try_silent=False)
        self.app.ui_mgr.open_for_game.assert_not_called()
        self.sauth_feedback.relogin_succeeded.assert_not_called()

    def test_silent_context_no_longer_monkeypatches_requests_or_sets_deadlines(self):
        original_send = requests.Session.send
        with self.prefetch_context.prefetch_scope():
            self.assertTrue(self.prefetch_context.in_prefetch())
        self.assertFalse(self.prefetch_context.in_prefetch())
        self.assertIs(requests.Session.send, original_send)


if __name__ == '__main__':
    unittest.main()
