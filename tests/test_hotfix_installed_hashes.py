"""Synthetic module files and configuration only; no download or app restart."""
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import hotfixmgr


class InstalledHashTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.target = self.tmp / 'fixture.pyc'
        self.target.write_bytes(b'original installation')
        self.source = b'fixture_value = 42\n'
        self.item = dict(target_module='fixture', target_commit='new',
                         need_hotfix_version=['fixture-version'],
                         source_sha256=hashlib.sha256(self.source).hexdigest())
        self.data = {'VERSION': 'fixture-version', 'FP_WORKDIR': str(self.tmp)}
        self.env = Mock()
        self.env.get.side_effect = lambda key, default=None: copy.deepcopy(self.data.get(key, default))
        self.env.get_from_file.side_effect = self.env.get.side_effect
        self.env.set.side_effect = self.save
        self.env.update_cached.side_effect = lambda values: self.data.update(copy.deepcopy(values))
        self.stack.enter_context(patch.object(hotfixmgr, 'genv', self.env))
        self.stack.enter_context(patch.object(hotfixmgr, '_is_macos_pyinstaller', return_value=False))
        self.resolve = self.stack.enter_context(patch.object(hotfixmgr, '_resolve_module_target_path',
            return_value=(str(self.target), 'pyc')))
        self.download = self.stack.enter_context(patch.object(hotfixmgr, '_get_verified_hotfix',
            return_value=(True, self.source, '')))
        self.restart = self.stack.enter_context(patch.object(hotfixmgr, 'restart_self'))
        self.stack.enter_context(patch.object(hotfixmgr.time, 'sleep'))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.addCleanup(lambda: setattr(hotfixmgr, 'prompt_active', False))
        self.addCleanup(lambda: setattr(hotfixmgr, 'prompt_items', []))

    def save(self, key, value, cached=False, *, require_saved=False):
        self.data[key] = copy.deepcopy(value)

    def run_hotfix(self, items=None):
        hotfixmgr.handle_if_needed(types.SimpleNamespace(
            get_hotfixes=lambda: [self.item] if items is None else items,
            signatures_required=True))

    def install(self):
        ok, error = hotfixmgr._apply_one(self.item, require_hashes=True)
        self.assertTrue(ok, error)
        hid = hotfixmgr._make_id(self.item)
        self.data['hotfix_records'][hid]['status'] = 'applied'
        self.download.reset_mock()
        return hid

    def test_saves_actual_compiled_file_hash_with_required_persistence(self):
        hid = self.install()
        record = self.data['hotfix_records'][hid]
        self.assertEqual(record['installed_sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertNotEqual(record['installed_sha256'], self.item['source_sha256'])
        self.assertTrue(self.env.set.call_args.kwargs['require_saved'])

    def test_unchanged_installed_module_skips_download_and_restart(self):
        self.install()
        self.run_hotfix()
        self.download.assert_not_called()
        self.restart.assert_not_called()

    def test_reinstall_reapplies_once_then_matches(self):
        self.install()
        self.target.write_bytes(b'reinstalled old module')
        self.run_hotfix()
        self.download.assert_called_once()
        self.restart.assert_called_once()
        self.download.reset_mock()
        self.restart.reset_mock()
        self.run_hotfix()
        self.download.assert_not_called()
        self.restart.assert_not_called()

    def test_old_record_without_hash_is_reapplied_not_trusted(self):
        hid = self.install()
        del self.data['hotfix_records'][hid]['installed_sha256']
        self.run_hotfix()
        self.download.assert_called_once()
        self.assertTrue(self.data['hotfix_records'][hid]['installed_sha256'])

    def test_removed_or_other_version_hotfix_is_not_reapplied(self):
        self.install()
        self.target.write_bytes(b'replaced')
        self.run_hotfix([])
        self.run_hotfix([dict(self.item, need_hotfix_version=['different-version'])])
        self.download.assert_not_called()
        self.restart.assert_not_called()

    def test_explicit_skip_is_preserved_even_if_file_changed(self):
        hid = self.install()
        self.data['hotfix_skipped'] = [hid]
        self.target.write_bytes(b'replaced')
        self.run_hotfix()
        self.download.assert_not_called()

    def test_last_cloud_entry_wins_without_reapplying_superseded_hash(self):
        self.install()
        old = dict(self.item, target_commit='old')
        self.data['hotfix_records'][hotfixmgr._make_id(old)] = dict(
            self.data['hotfix_records'][hotfixmgr._make_id(self.item)],
            target_commit='old', installed_sha256='0' * 64)
        self.run_hotfix([old, self.item])
        self.download.assert_not_called()
        self.target.write_bytes(b'replaced')
        self.run_hotfix([old, self.item])
        self.download.assert_called_once()
        self.assertEqual(self.download.call_args.args[0]['target_commit'], 'new')

    def test_last_explicitly_skipped_entry_does_not_replay_older_patch(self):
        hid = self.install()
        self.data['hotfix_skipped'] = [hid]
        self.run_hotfix([dict(self.item, target_commit='old'), self.item])
        self.download.assert_not_called()

    def test_resolves_current_module_not_saved_previous_installation(self):
        self.install()
        moved = self.tmp / 'new-installation.pyc'
        moved.write_bytes(b'old module at a new location')
        self.resolve.return_value = str(moved), 'pyc'
        self.run_hotfix()
        self.download.assert_called_once()
        self.assertEqual(self.data['hotfix_records'][hotfixmgr._make_id(self.item)]['target_path'], str(moved))

    def test_missing_module_does_not_match_hash(self):
        hid = self.install()
        self.target.unlink()
        self.assertFalse(hotfixmgr._installed_file_matches(self.data['hotfix_records'][hid]))

    def test_stale_true_probe_cannot_hide_write_failure(self):
        self.data['hotfix_probed'] = True
        self.env.set.side_effect = None
        self.env.set.return_value = False
        self.assertFalse(hotfixmgr.probe_cache_write_once())
        self.env.set.assert_called_once_with('hotfix_probed', True, True, require_saved=True)

    def test_hash_persistence_failure_does_not_report_application_success(self):
        self.env.set.side_effect = None
        self.env.set.return_value = False
        ok, error = hotfixmgr._apply_one(self.item, require_hashes=True)
        self.assertFalse(ok)
        self.assertIn('保存热更新状态失败', error)
        self.assertNotIn('hotfix_records', self.data)
        self.restart.assert_not_called()

    def test_reinstalled_pending_module_does_not_restore_old_backup(self):
        hid = self.install()
        self.data.update(hotfix_pending_validate=[hid], last_run_state='crash')
        self.data['hotfix_records'][hid]['status'] = 'pending_validate'
        self.target.write_bytes(b'reinstalled old module')
        with patch.object(hotfixmgr, '_rollback_one') as rollback:
            hotfixmgr.pre_start_check_and_rollback_if_needed()
        rollback.assert_not_called()
        self.restart.assert_not_called()
        self.assertEqual(self.data['hotfix_pending_validate'], [])
        self.assertEqual(self.data['hotfix_records'][hid]['status'], 'needs_reapply')
        self.run_hotfix([])
        self.download.assert_not_called()
        self.run_hotfix()
        self.download.assert_called_once()

    def test_unchanged_pending_module_still_rolls_back_after_crash(self):
        hid = self.install()
        self.data.update(hotfix_pending_validate=[hid], last_run_state='crash')
        self.data['hotfix_records'][hid]['status'] = 'pending_validate'
        with patch.object(hotfixmgr, '_rollback_one', return_value=(True, 'fixture')) as rollback:
            hotfixmgr.pre_start_check_and_rollback_if_needed()
        rollback.assert_called_once_with(hid)
        self.assertIn(hid, self.data['hotfix_skipped'])

    def test_overlay_records_hash_of_actual_source_file(self):
        with patch.object(hotfixmgr, '_is_macos_pyinstaller', return_value=True), \
             patch.object(hotfixmgr, '_overlay_root', return_value=str(self.tmp / 'overlay')):
            hid = self.install()
        record = self.data['hotfix_records'][hid]
        self.assertEqual(record['target_kind'], 'overlay_py')
        self.assertEqual(record['installed_sha256'], self.item['source_sha256'])
        self.assertTrue(hotfixmgr._installed_file_matches(record))


if __name__ == '__main__':
    unittest.main()
