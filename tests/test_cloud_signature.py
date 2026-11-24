import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
import cloud_signature

# Fixed, publicly known test scalar; never use for production signing.
_TEST_KEY = ec.derive_private_key(1, ec.SECP256R1())
_TEST_PUBLIC = _TEST_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign(data):
    parsed = json.loads(data)
    canonical = json.dumps(parsed, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    signature = _TEST_KEY.sign(cloud_signature.SIGNATURE_DOMAIN + canonical, ec.ECDSA(hashes.SHA256()))
    parsed['_signature'] = {'alg': 'ECDSA-P256-SHA256-DER', 'value': base64.b64encode(signature).decode()}
    return json.dumps(parsed, ensure_ascii=False).encode('utf-8')


class CloudSignatureTests(unittest.TestCase):
    def test_config_signature_and_strict_json(self):
        payload = b'{"lastModified":1,"hotfix":[]}'
        signed = sign(payload)
        self.assertTrue(cloud_signature.verify_cloud_config(signed, _TEST_PUBLIC))
        self.assertFalse(cloud_signature.verify_cloud_config(signed.replace(b'"lastModified": 1', b'"lastModified": 2'), _TEST_PUBLIC))
        self.assertFalse(cloud_signature.verify_cloud_config(payload, _TEST_PUBLIC))
        duplicate = signed[:-1] + b', "lastModified": 1}'
        self.assertFalse(cloud_signature.verify_cloud_config(duplicate, _TEST_PUBLIC))
        self.assertFalse(cloud_signature.verify_cloud_config(b'{"v":NaN}', _TEST_PUBLIC))
        self.assertTrue(cloud_signature.verify_cloud_config(sign(b'{"float":1.25,"small":1e-7}'), _TEST_PUBLIC))

    def test_canonical_numeric_unicode_vector(self):
        data = (b'{"\\u4e2d":-0.0,"n":1e-7,"a":[1.25,1e20,100000000000000000000],'
                b'"_signature":{"alg":"ECDSA-P256-SHA256-DER","value":"AA=="}}')
        _, canonical = cloud_signature.canonical_cloud_config(data)
        self.assertEqual(canonical, '{"a":[1.25,1e+20,100000000000000000000],"n":1e-07,"中":-0.0}'.encode())
        for invalid in (b'{"_signature":{},"a":NaN}', b'{"_signature":{},"a":1e999}',
                        b'{"_signature":{},"nested":{"a":1,"a":2}}'):
            self.assertFalse(cloud_signature.verify_cloud_config(invalid, _TEST_PUBLIC))

    def test_verified_cache_survives_restart_and_rejects_tampering(self):
        channel_utils = types.ModuleType('channelHandler.channelUtils')
        channel_utils.cmp_game_id = lambda left, right: left == right
        dynamic = types.ModuleType('gamecatalog')
        dynamic.DynamicGameCatalog = lambda **kwargs: object()
        with mock.patch.dict(sys.modules, {'channelHandler.channelUtils': channel_utils, 'gamecatalog': dynamic}):
            import cloudRes
            class Response:
                def __init__(self, content): self.content = content
                def raise_for_status(self): pass
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(cloud_signature, 'CLOUD_CONFIG_PUBLIC_KEY_PEM', _TEST_PUBLIC):
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False
                cloud = cloudRes.CloudRes(['https://example.invalid/cloud.json'], directory)
                payload = b'{"lastModified":1,"hotfix":[]}'
                cloud.session.get = lambda url, **kw: Response(sign(payload))
                cloud.update_cache_if_needed()
                self.assertEqual(cloud.local_data['lastModified'], 1)
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False
                again = cloudRes.CloudRes([], directory)
                self.assertEqual(again.local_data['lastModified'], 1)
                cache = Path(directory) / 'cache.signed.json'
                raw = json.loads(cache.read_text())
                raw['payload'] = base64.b64encode(b'{"lastModified":2}').decode()
                cache.write_text(json.dumps(raw))
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False
                rejected = cloudRes.CloudRes([], directory)
                self.assertEqual(rejected.local_data, {})
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False


    def test_http_304_without_signed_cache_does_not_use_legacy_cache(self):
        channel_utils = types.ModuleType('channelHandler.channelUtils')
        channel_utils.cmp_game_id = lambda left, right: left == right
        dynamic = types.ModuleType('gamecatalog')
        dynamic.DynamicGameCatalog = lambda **kwargs: object()
        with mock.patch.dict(sys.modules, {'channelHandler.channelUtils': channel_utils, 'gamecatalog': dynamic}):
            import cloudRes
            class Response:
                status_code = 304
                content = b''
                def raise_for_status(self): pass
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(cloud_signature, 'CLOUD_CONFIG_PUBLIC_KEY_PEM', _TEST_PUBLIC):
                Path(directory, 'cache.json').write_text('{"lastModified":999,"hostile":true}')
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False
                cloud = cloudRes.CloudRes(['a', 'b'], directory)
                cloud.session.get = mock.Mock(return_value=Response())
                cloud.update_cache_if_needed()
                self.assertEqual(cloud.local_data, {})
                self.assertEqual(cloud.session.get.call_count, 2)
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False

    def test_stale_or_invalid_mirror_falls_through_to_newer_signed_content(self):
        channel_utils = types.ModuleType('channelHandler.channelUtils')
        channel_utils.cmp_game_id = lambda left, right: left == right
        dynamic = types.ModuleType('gamecatalog')
        dynamic.DynamicGameCatalog = lambda **kwargs: object()
        with mock.patch.dict(sys.modules, {'channelHandler.channelUtils': channel_utils, 'gamecatalog': dynamic}):
            import cloudRes
            class Response:
                def __init__(self, content): self.content = content
                def raise_for_status(self): pass
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(cloud_signature, 'CLOUD_CONFIG_PUBLIC_KEY_PEM', _TEST_PUBLIC):
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False
                cloud = cloudRes.CloudRes(['a'], directory)
                cloud.session.get = lambda *a, **k: Response(sign(b'{"lastModified":2,"value":"current"}'))
                cloud.update_cache_if_needed()
                cloud.urls = ['old', 'tampered', 'same_version_different', 'new']
                payloads = [sign(b'{"lastModified":1}'), b'{"lastModified":3}',
                            sign(b'{"lastModified":2,"value":"other"}'),
                            sign(b'{"lastModified":3,"value":"new"}')]
                cloud.session.get = mock.Mock(side_effect=[Response(x) for x in payloads])
                cloud.update_cache_if_needed()
                self.assertEqual(cloud.local_data['value'], 'new')
                self.assertEqual(cloud.session.get.call_count, 4)
                cloudRes.CloudRes._instance = None
                cloudRes.CloudRes._initialized = False



if __name__ == '__main__':
    unittest.main()
