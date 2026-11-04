import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import cloud_config_signer as signer


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class CloudSignerTest(unittest.TestCase):
    def test_prepare_sign_verify_and_reject_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            run('git', 'init', '-q', str(repo))
            run('git', 'config', 'user.name', 'Test', cwd=repo)
            run('git', 'config', 'user.email', 'test@example.invalid', cwd=repo)
            (repo / 'src').mkdir()
            source = b'raise RuntimeError("must never run")\r\n'
            (repo / 'src' / 'module.py').write_bytes(source)
            run('git', 'add', '.', cwd=repo)
            run('git', 'commit', '-qm', 'source', cwd=repo)
            commit = run('git', 'rev-parse', 'HEAD', cwd=repo).stdout.decode().strip()
            config = {'version': '1', 'lastModified': 1, 'fraction': 1.25, 'unicode': '\u4e2d\u6587',
                      'hotfix': [{'target_module': 'module', 'target_commit': commit}]}
            (repo / 'assets').mkdir()
            (repo / 'assets' / 'cloudRes.json').write_text(json.dumps(config), encoding='utf-8')
            run('git', 'add', '.', cwd=repo)
            run('git', 'commit', '-qm', 'candidate', cwd=repo)
            out = root / 'candidate'
            signer.main  # import must not run candidate source
            baseline = root / 'baseline.json'
            baseline.write_text(json.dumps(config), encoding='utf-8')
            stale = type('Args', (), {'repo': str(repo), 'fetch': False, 'commit': 'HEAD',
                                     'baseline': str(baseline), 'output': str(root / 'stale')})
            with self.assertRaisesRegex(ValueError, 'must exceed baseline'):
                signer.prepare(stale)
            self.assertFalse((root / 'stale').exists())
            args = type('Args', (), {'repo': str(repo), 'fetch': False, 'commit': 'HEAD',
                                     'baseline': None, 'output': str(out)})
            signer.prepare(args)
            candidate = signer.read_config((out / 'cloudRes.unsigned.json').read_bytes())
            self.assertEqual(candidate['hotfix'][0]['source_sha256'], hashlib.sha256(source).hexdigest())
            self.assertNotIn('marshal_sha256', candidate['hotfix'][0])
            key, pub = root / 'test-key.pem', root / 'test-public.pem'
            run('/usr/bin/openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(key))
            run('/usr/bin/openssl', 'pkey', '-in', str(key), '-pubout', '-out', str(pub))
            mock_signer = root / 'mock-signer'
            mock_signer.write_text('#!/bin/sh\n/usr/bin/openssl dgst -sha256 -sign "$TEST_SIGN_KEY" -out "$4.der" "$3" || exit 1\n/usr/bin/openssl base64 -A -in "$4.der" > "$4"\nprintf "\\n" >> "$4"\n')
            mock_signer.chmod(0o700)
            state = json.loads((out / 'candidate.json').read_text())
            options = type('Args', (), {'candidate': str(out), 'signer': str(mock_signer), 'key_handle': str(key), 'public_key': str(pub)})
            with patch.dict(os.environ, {'TEST_SIGN_KEY': str(key)}), patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=state['signing_sha256']):
                signer.sign(options)
            signer.check(options)
            signed = signer.read_config((out / 'cloudRes.json').read_bytes())
            self.assertEqual(signed['_signature']['alg'], 'ECDSA-P256-SHA256-DER')
            signed['fraction'] = 1.5
            (out / 'cloudRes.json').write_text(json.dumps(signed), encoding='utf-8')
            with self.assertRaises(ValueError):
                signer.check(options)

    def test_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for data in [b'{"hotfix":[],"x":1,"x":2}', b'{"hotfix":[],"x":NaN}',
                     b'{"hotfix":[],"x":1e999}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                signer.read_config(data)


if __name__ == '__main__':
    unittest.main()
