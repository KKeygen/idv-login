import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from telemetry import collect_proxy_state


class _Key:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Registry:
    HKEY_CURRENT_USER = object()

    def __init__(self, environment=None, internet=None, denied=()):
        self.environment = environment or {}
        self.internet = internet or {}
        self.denied = set(denied)

    def OpenKey(self, _root, subkey):
        name = 'env' if subkey == 'Environment' else 'internet'
        if name in self.denied:
            raise PermissionError('test-only')
        return _Key(self.environment if name == 'env' else self.internet)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1


class ProxyDiagnosticsTests(unittest.TestCase):
    def test_non_windows_only_reports_external_environment(self):
        state = collect_proxy_state(10717, platform='darwin', environ={})
        self.assertEqual(state, {'external_env_proxy': False})

    def test_exact_own_endpoint_is_not_external(self):
        env = {'http_proxy': 'http://127.0.0.1:10717',
               'HTTPS_PROXY': 'localhost:10717'}
        state = collect_proxy_state(10717, platform='win32', environ=env,
                                    registry=_Registry())
        self.assertEqual(state, {'windows_system_proxy': False, 'external_env_proxy': False})

    def test_substring_and_credentials_do_not_match_own_endpoint(self):
        for value in ('http://127.0.0.1:107170', 'http://127.0.0.1.example:10717',
                      'http://user:secret@localhost:10717', 'localhost:10718',
                      'file://localhost:10717'):
            with self.subTest(value=value):
                state = collect_proxy_state(10717, platform='linux', environ={'HTTP_PROXY': value})
                self.assertEqual(state, {'external_env_proxy': True})
                self.assertNotIn(value, repr(state))

    def test_windows_user_environment_and_system_proxy(self):
        registry = _Registry(environment={'HTTPS_PROXY': 'http://different:8080'},
                             internet={'ProxyEnable': 1})
        state = collect_proxy_state(10717, platform='win32', environ={}, registry=registry)
        self.assertEqual(state, {'windows_system_proxy': True, 'external_env_proxy': True})
        self.assertNotIn('different', repr(state))

    def test_auto_config_is_system_proxy_even_if_proxy_disabled(self):
        registry = _Registry(internet={'ProxyEnable': 0, 'AutoConfigURL': 'https://example.invalid/pac'})
        self.assertEqual(collect_proxy_state(10717, platform='win32', environ={}, registry=registry),
                         {'windows_system_proxy': True, 'external_env_proxy': False})

    def test_windows_auto_detect_is_system_proxy(self):
        registry = _Registry(internet={'ProxyEnable': 0, 'AutoDetect': 1})
        self.assertTrue(collect_proxy_state(10717, platform='win32', environ={},
                                            registry=registry)['windows_system_proxy'])

    def test_unreadable_registry_is_unknown_not_false(self):
        registry = _Registry(denied=('env', 'internet'))
        state = collect_proxy_state(10717, platform='win32', environ={}, registry=registry)
        self.assertEqual(state, {'windows_system_proxy': 'unknown', 'external_env_proxy': 'unknown'})
        state = collect_proxy_state(10717, platform='win32',
                                    environ={'HTTP_PROXY': 'http://external:1234'}, registry=registry)
        self.assertEqual(state, {'windows_system_proxy': 'unknown', 'external_env_proxy': True})


if __name__ == '__main__':
    unittest.main()
