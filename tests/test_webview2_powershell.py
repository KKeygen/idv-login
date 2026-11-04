import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
import webview2_setup
import prepare_webview2_loader


class PowerShellEnvironmentTests(unittest.TestCase):
    def test_loader_uses_windows_default_module_path_and_keeps_signature_check(self):
        with patch.dict(os.environ, {'PSMODULEPATH': 'incompatible', 'SystemRoot': 'C:/Windows'}):
            with patch.object(prepare_webview2_loader.subprocess, 'run') as run:
                prepare_webview2_loader.verify_signature(Path('loader.dll'))
        args, kwargs = run.call_args
        self.assertFalse(any(k.upper() == 'PSMODULEPATH' for k in kwargs['env']))
        self.assertIn('Get-AuthenticodeSignature', args[0][-1])
        self.assertIn('Microsoft Corporation', args[0][-1])
        self.assertTrue(kwargs['check'])

    def test_loader_surfaces_verification_failure(self):
        error = subprocess.CalledProcessError(1, 'powershell', stderr=b'signature error')
        with patch.object(prepare_webview2_loader.subprocess, 'run', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'signature error'):
                prepare_webview2_loader.verify_signature(Path('loader.dll'))

    def test_runtime_bootstrapper_uses_windows_default_module_path(self):
        with patch.dict(os.environ, {'PSMODULEPATH': 'incompatible', 'SystemRoot': 'C:/Windows'}):
            with patch.object(subprocess, 'CREATE_NO_WINDOW', 0, create=True):
                with patch.object(webview2_setup.subprocess, 'run') as run:
                    run.return_value.returncode = 0
                    webview2_setup._verify_bootstrapper(Path('installer.exe'))
        args, kwargs = run.call_args
        self.assertFalse(any(k.upper() == 'PSMODULEPATH' for k in kwargs['env']))
        self.assertIn('Get-AuthenticodeSignature', args[0][-1])
        self.assertIn('Microsoft Corporation', args[0][-1])


if __name__ == '__main__':
    unittest.main()
