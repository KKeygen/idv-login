import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
from cloudSync import CloudSyncManager


def record(name, login_time=100, name_time=None, token='old-token'):
    item = {'uuid': 'synthetic-account', 'name': name,
            'login_info': {'login_channel': 'synthetic', 'code': 'test'},
            'user_info': {'id': 'synthetic-user', 'token': token},
            'last_login_time': login_time}
    if name_time is not None:
        item['name_updated_at'] = name_time
    return item


class CloudSyncNameTests(unittest.TestCase):
    def setUp(self):
        self.sync = CloudSyncManager()

    def test_local_rename_survives_pull_without_losing_new_remote_credentials(self):
        local = record('renamed', name_time=200)
        remote = record('original', login_time=101, token='new-token')
        result = self.sync._merge_channels([local], [remote])[0]
        self.assertEqual(result['name'], 'renamed')
        self.assertEqual(result['name_updated_at'], 200)
        self.assertEqual(result['user_info']['token'], 'new-token')
        self.assertEqual(result['last_login_time'], 101)
        self.assertEqual(local, record('renamed', name_time=200))
        self.assertEqual(remote['name'], 'original')

    def test_rename_propagates_back_to_peer_with_newer_credentials(self):
        local = record('original', login_time=102, token='newest-token')
        remote = record('renamed', name_time=200)
        result = self.sync._merge_channels([local], [remote])[0]
        self.assertEqual(result['name'], 'renamed')
        self.assertEqual(result['user_info']['token'], 'newest-token')

    def test_equal_login_time_preserves_rename_in_both_directions(self):
        original = record('original')
        renamed = record('renamed', name_time=200)
        pulled = self.sync._merge_channels([renamed], [original])
        received = self.sync._merge_channels([original], pulled)
        self.assertEqual(pulled, received)
        self.assertEqual(received[0]['name'], 'renamed')

    def test_latest_rename_wins_and_legacy_merge_is_unchanged(self):
        result = self.sync._merge_channels(
            [record('older-name', name_time=200)],
            [record('newer-name', name_time=201)])[0]
        self.assertEqual(result['name'], 'newer-name')
        legacy = record('remote', token='remote-token')
        self.assertEqual(self.sync._merge_channels([record('local')], [legacy]), [legacy])

    def test_rename_metadata_survives_save_reload_and_scan_reimport(self):
        # Isolate login adapters and logging; execute the actual manager and file writes.
        log = types.ModuleType('logutil')
        log.setup_logger = lambda: Mock()
        utils = types.ModuleType('channelHandler.channelUtils')
        utils.cmp_game_id = lambda left, right: left == right
        spec = importlib.util.spec_from_file_location('channelmgr_name_test', SRC / 'channelmgr.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'logutil': log, 'channelHandler.channelUtils': utils}):
            spec.loader.exec_module(module)
        adapters = {}
        for filename, class_name in (
            ('miChannelHandler', 'miChannel'), ('huaChannelHandler', 'huaweiChannel'),
            ('vivoChannelHandler', 'vivoChannel'), ('wechatChannelHandler', 'wechatChannel'),
            ('oppoChannelHandler', 'oppoChannel'), ('bilibiliChannelHandler', 'bilibiliChannel'),
        ):
            adapter = types.ModuleType('channelHandler.' + filename)
            setattr(adapter, class_name, module.channel)
            adapters[adapter.__name__] = adapter
        state = types.ModuleType('app_state')
        state.toast = lambda *a, **k: None
        adapters['app_state'] = state
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, adapters):
            path = Path(tmp) / 'channels.json'
            path.write_text(json.dumps([record('original')]))
            values = {'FP_CHANNEL_RECORD': str(path)}
            env = types.SimpleNamespace(get=lambda key, default=None: values.get(key, default))
            with patch.object(module, 'genv', env), patch.object(module.time, 'time_ns', return_value=200):
                manager = module.ChannelManager()
                self.assertTrue(manager.rename('synthetic-account', 'renamed'))
                manager = module.ChannelManager()
                self.assertEqual(manager.channels[0].name_updated_at, 200)
                self.assertEqual(manager.channels[0].last_login_time, 100)
                self.assertTrue(manager.rename('synthetic-account', 'renamed-again'))
                self.assertEqual(manager.channels[0].name_updated_at, 201)
                manager.import_from_scan(
                    {'login_channel': 'synthetic', 'code': 'new-scan'},
                    {'user': {'id': 'synthetic-user', 'token': 'new-token'}})
                manager = module.ChannelManager()
                self.assertEqual(len(manager.channels), 1)
                self.assertEqual(manager.channels[0].name, 'renamed-again')
                self.assertEqual(manager.channels[0].name_updated_at, 201)
                self.assertEqual(manager.channels[0].user_info['token'], 'new-token')


if __name__ == '__main__':
    unittest.main()
