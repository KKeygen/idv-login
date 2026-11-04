import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import hotfixmgr


class HotfixHashesTests(unittest.TestCase):
    def test_strict_requires_source_hash_but_legacy_can_omit(self):
        self.assertEqual(hotfixmgr._hotfix_source_hash({}, False), '')
        for item in ({}, {'source_sha256': 'bad'}, {'source_sha256': None}):
            with self.assertRaises(ValueError): hotfixmgr._hotfix_source_hash(item, True)

    def test_only_source_digest_is_used_for_download(self):
        data = b'example = 1\n'
        item = {'source_sha256': hashlib.sha256(data).hexdigest()}
        with mock.patch.object(hotfixmgr, '_download_text', return_value=(True, data, '')) as download:
            ok, content, error = hotfixmgr._get_verified_hotfix(item, 'first', ['second'], True)
        self.assertTrue(ok, error)
        self.assertEqual(content, data)
        self.assertEqual(download.call_args.args[2], item['source_sha256'])

    def test_bad_mirror_retries_next(self):
        data = b'example = 1\n'; expected = hashlib.sha256(data).hexdigest()
        class Reply:
            status_code = 200
            def __init__(self, content): self.content = content
        session = mock.Mock()
        session.get.side_effect = [Reply(b'altered'), Reply(data)]
        with mock.patch.object(hotfixmgr.requests, 'Session', return_value=session):
            ok, result, _ = hotfixmgr._download_text('first', ['second'], expected)
        self.assertTrue(ok)
        self.assertEqual(result, data)
        self.assertEqual(session.get.call_count, 2)

    def test_overlay_and_normal_reject_missing_hashes_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'module.py'; target.write_text('old=1')
            item = {'target_module': 'module', 'target_commit': 'abc'}
            with mock.patch.object(hotfixmgr, '_is_macos_pyinstaller', return_value=False), \
                 mock.patch.object(hotfixmgr, '_resolve_module_target_path', return_value=(str(target), 'py')):
                ok, _ = hotfixmgr._apply_one(item, require_hashes=True)
            self.assertFalse(ok)
            self.assertEqual(target.read_text(), 'old=1')
            with mock.patch.object(hotfixmgr, '_is_macos_pyinstaller', return_value=True), \
                 mock.patch.object(hotfixmgr, '_overlay_root', return_value=tmp):
                ok, _ = hotfixmgr._apply_one(item, require_hashes=True)
            self.assertFalse(ok)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['module.py'])


if __name__ == '__main__': unittest.main()
