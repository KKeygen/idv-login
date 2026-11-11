"""Offline lifecycle regressions; no real accounts, Windows locks or network IO."""
import ast
import base64
import copy
from concurrent.futures import Future
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch


SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))


def load_definitions(filename, names, namespace):
    """Use the production definitions without starting their UI/runtime imports."""
    tree = ast.parse((SRC / filename).read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC / filename), 'exec'), namespace)


def short_game(game):
    return game.split('-')[-1]


LOGGER = Mock()
COMMON = dict(base64=base64, copy=copy, json=json, time=time, Path=Path,
              Future=Future, threading=threading, getShortGameId=short_game,
              cmp_game_id=lambda a, b: short_game(a) == short_game(b),
              genv=types.SimpleNamespace(set=lambda *args: None),
              setup_logger=lambda: LOGGER)
from urllib.parse import unquote
COMMON['unquote'] = unquote
load_definitions('channel_cache.py', {'packet_identity', 'unisdk_expires_at',
                                    'credential_expires_at'}, COMMON)
load_definitions('channelmgr.py', {'channel'}, COMMON)
load_definitions('mpay_db_sync.py', {'MpayDBSync', 'credentials_from_packet',
                                   'packet_from_credentials'}, COMMON)
Channel, MpayDBSync = COMMON['channel'], COMMON['MpayDBSync']


def packet(channel, game, generation):
    auth = {'gameid': game, 'login_channel': channel, 'sdkuid': 'fixture-sdkuid',
            'timestamp': str(int(time.time() * 1000)), 'sessionid': 'fixture-session'}
    return {'token': f'fixture-token-{generation}', 'user_id': 'fixture-user',
            'login_channel': channel, 'app_channel': channel, 'pay_channel': channel,
            'udid': 'fixture-device', 'jf_game_id': game, 'sdk_version': 'fixture-version',
            'extra_unisdk_data': json.dumps({'SAUTH_JSON': base64.b64encode(
                json.dumps(auth).encode()).decode()})}


class RefreshableChannel(Channel):
    def __init__(self, name, generation=0, failure=None):
        super().__init__({'login_channel': name, 'code': 'fixture'}, user_info={},
                         ext_info={}, device_info={}, name='fixture-name')
        self.game_id = 'h55'
        self.generation = generation
        self.failure = failure
        self.chosen_account_id = 'fixture-choice'
        self.oppo_open_account = {}

    @classmethod
    def from_dict(cls, data):
        return cls(data['channel_name'], data['generation'], data['failure'])

    def get_uniSdk_data(self, game_id='', on_complete=None, *, interactive=True):
        if interactive:
            raise AssertionError('Launch refresh must not open a login dialog')
        if self.failure == 'error':
            raise ValueError('fixture renewal failed')
        if self.failure == 'cancel':
            return None
        self.generation += 1
        return packet(self.channel_name, short_game(game_id), self.generation)

    def report_login_failure(self, *args, **kwargs):
        pass


class MpayLaunchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture-g-h55-64-mpay.db'
        self.path.touch()
        self.records = []
        self.selected_games = []
        self.calls = []
        self.rows = {}
        self.sync = MpayDBSync.__new__(MpayDBSync)
        self.sync.lock = threading.RLock()
        self.sync.stopping = False
        self.sync.projected = set()
        self.sync._order_states = {}
        self.sync._relogins = {}
        self.sync.identity = 'fixture-machine'
        self.sync.logger = LOGGER
        self.sync.manager = types.SimpleNamespace(
            channels=self.records, query_channel=lambda uid: next(
                (r for r in self.records if r.uuid == uid), None), save_records=Mock())
        self.sync.paths = lambda game: [self.path] if short_game(game) == 'h55' else []
        self.sync.game_ids = lambda: ['h55']
        self.sync.reconcile_game_changes = Mock()
        self.sync._track_head = Mock(return_value=None)
        self.sync._restore_head = Mock()
        self.sync._save_heads = Mock()
        self.sync.operate = self.operate

        test = self

        class Policy:
            def __init__(self, manager):
                pass

            def select(self, games=None):
                test.selected_games.append(games)
                return {'h55': list(test.records)}

            def account_label(self, record, expired=False):
                return record.name + (' expired' if expired else '')

        self.policy_module = types.ModuleType('account_list_policy')
        self.policy_module.AccountListPolicy = Policy
        modules = patch.dict(sys.modules, {'account_list_policy': self.policy_module})
        modules.start()
        self.addCleanup(modules.stop)

    def operate(self, path, operation, **args):
        # Spy on the host/primitive boundary. The Wasm implementation is not
        # replaced or modified by this fix; this model checks the host never
        # asks it to delete/reinsert an existing account during a refresh.
        key = tuple(args[field] for field in ('game_id', 'login_channel', 'sdkuid'))
        self.calls.append((operation, key, copy.deepcopy(args)))
        if operation == 'put_account':
            created = key not in self.rows
            row = self.rows.setdefault(key, {})
            row.update(copy.deepcopy(args))
            return created
        if operation == 'rename_account':
            self.rows[key]['name'] = args['name']
        elif operation == 'delete_account':
            self.rows.pop(key, None)
        else:
            raise AssertionError(operation)

    def account(self, name='huawei', failure=None):
        record = RefreshableChannel(name, failure=failure)
        record.cache_unisdk(packet(name, 'h55', 0))
        record.cache_unisdk(packet(name, 'g37', 0))
        self.records.append(record)
        return record

    def test_every_manual_channel_refreshes_at_startup_and_each_relaunch(self):
        # Read the real catalog so new channels cannot silently miss this check.
        tree = ast.parse((SRC / 'const.py').read_text(encoding='utf-8'))
        catalog = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'manual_login_channels'
                               for t in node.targets))
        for item in catalog:
            self.account(item['channel'])
        other_game = {r.uuid: copy.deepcopy(r.unisdk_cache['g37']) for r in self.records}
        for generation, refresh in enumerate((self.sync.refresh_startup,
                lambda: self.sync.refresh_before_game('aec-fixture-g-h55'),
                lambda: self.sync.refresh_before_game('h55')), 1):
            refresh()
            for record in self.records:
                with self.subTest(channel=record.channel_name, launch=generation):
                    self.assertEqual(record.generation, generation)
                    self.assertEqual(record.cached_unisdk('h55')['token'],
                                     f'fixture-token-{generation}')
                    self.assertEqual(record.unisdk_cache['g37'], other_game[record.uuid])
                    key = record.sdk_identity('h55')
                    self.assertEqual(self.rows[key]['credentials']['token'],
                                     f'fixture-token-{generation}')
                    self.assertEqual(self.rows[key]['position'], 'preserve')
        self.assertEqual(self.selected_games, [None, ['h55'], ['h55']])
        self.assertEqual(self.sync.manager.save_records.call_count, 3 * len(catalog))
        self.assertNotIn('delete_account', [op for op, _, _ in self.calls])

    def test_dirty_existing_row_is_updated_without_becoming_owned(self):
        record = self.account()
        key = record.sdk_identity('h55')
        original_row = {'credentials': {'token': 'fixture-dirty-token'}, 'native_field': 'keep'}
        self.rows[key] = original_row
        self.sync.refresh_before_game('h55')
        self.sync.refresh_before_game('h55')
        self.assertIs(self.rows[key], original_row)
        self.assertEqual(original_row['credentials']['token'], 'fixture-token-2')
        self.assertEqual(original_row['native_field'], 'keep')
        self.assertIn((self.path, key, False), self.sync.projected)
        self.assertNotIn('delete_account', [op for op, k, _ in self.calls if k == key])
        self.sync.shutdown()
        self.assertIn(key, self.rows)  # Existing native/leftover rows are not ours to delete.

    def test_new_row_remains_owned_across_refreshes_and_is_removed_on_exit(self):
        record = self.account()
        key = record.sdk_identity('h55')
        self.sync.refresh_before_game('h55')
        self.sync.refresh_before_game('h55')
        self.assertIn((self.path, key, True), self.sync.projected)
        self.assertNotIn('delete_account', [op for op, _, _ in self.calls])
        self.sync.shutdown()
        self.assertNotIn(key, self.rows)
        self.assertEqual([op for op, k, _ in self.calls if k == key],
                         ['put_account', 'put_account', 'delete_account'])

    def test_failed_refresh_preserves_existing_database_credentials(self):
        for failure in ('cancel', 'error'):
            with self.subTest(failure=failure):
                self.records.clear()
                self.rows.clear()
                self.calls.clear()
                self.sync.projected.clear()
                record = self.account(failure=failure)
                key = record.sdk_identity('h55')
                original = {'token': 'fixture-existing-token'}
                self.rows[key] = {'credentials': original}
                self.sync._remember_put(self.path, key, True)
                self.sync.refresh_before_game('h55')
                self.assertIsNone(record.cached_unisdk('h55'))
                self.assertIs(self.rows[key]['credentials'], original)
                self.assertEqual([op for op, k, _ in self.calls if k == key], ['rename_account'])

    def test_one_failed_channel_does_not_block_the_other_refresh(self):
        failed = self.account('huawei', failure='error')
        healthy = self.account('bilibili_sdk')
        self.sync.refresh_before_game('h55')
        self.assertIsNone(failed.cached_unisdk('h55'))
        self.assertEqual(healthy.cached_unisdk('h55')['token'], 'fixture-token-1')
        self.assertEqual(self.rows[healthy.sdk_identity('h55')]['credentials']['token'],
                         'fixture-token-1')

    def test_scan_record_reuses_captured_credentials_without_channel_login(self):
        fresh = packet('bilibili_sdk', 'h55', 'scan')
        credentials = COMMON['credentials_from_packet'](fresh, 'h55')[1]
        record = Channel({'login_channel': 'bilibili_sdk', 'code': 'fixture-scan'},
                         user_info={'id': 'fixture-user', **credentials},
                         ext_info=credentials['pc_ext_info'], device_info={})
        record.request_unisdk = Mock(side_effect=AssertionError('Scan cannot renew silently'))
        self.records.append(record)
        self.sync.refresh_before_game('h55')
        self.sync.refresh_before_game('h55')
        record.request_unisdk.assert_not_called()
        self.assertEqual(self.rows[record.sdk_identity('h55')]['credentials'], credentials)


if __name__ == '__main__':
    unittest.main()
