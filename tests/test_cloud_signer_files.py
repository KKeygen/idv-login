"""File safety checks without creating a Secure Enclave key."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires macOS Swift')
class EnclaveFileSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name).resolve() / 'signer'
        source = Path(__file__).resolve().parents[1] / 'tools/cloud_sign_enclave.swift'
        subprocess.run(['swiftc', '-O', str(source), '-o', str(cls.binary)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_no_overwrite_symlink_or_insecure_handle(self):
        folder = Path(self.directory.name).resolve()
        handle = folder / 'handle'
        handle.write_bytes(b'invalid-test-handle')
        handle.chmod(0o600)
        link = folder / 'link'
        link.symlink_to(handle)
        def call(*args):
            return subprocess.run([str(self.binary), *map(str, args)], capture_output=True, text=True)
        # None of these checks creates a key: generate rejects an existing name
        # before entering the Secure Enclave.
        existing = call('generate', handle)
        if 'Secure Enclave unavailable' in existing.stderr:
            self.skipTest('Secure Enclave unavailable')
        self.assertNotEqual(existing.returncode, 0)
        self.assertIn('refusing to replace', existing.stderr)
        self.assertEqual(handle.read_bytes(), b'invalid-test-handle')
        self.assertIn('refusing to replace', call('generate', link).stderr)
        self.assertIn('could not open key handle', call('public', link).stderr)
        handle.chmod(0o644)
        self.assertIn('owner-only regular file', call('public', handle).stderr)
        handle.chmod(0o600)
        self.assertNotIn('owner-only regular file', call('public', handle).stderr)
        directory_link = folder / 'linked-dir'
        directory_link.symlink_to(folder, target_is_directory=True)
        self.assertIn('symbolic link', call('public', directory_link / 'handle').stderr)


if __name__ == '__main__':
    unittest.main()
