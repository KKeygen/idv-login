import os
import sys
import tempfile
import unittest

import requests


SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from gamecatalog import DynamicGameCatalog


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses=None):
        self.responses = {
            key: list(value) for key, value in (responses or {}).items()
        }
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        queued = self.responses.get(url, [])
        if not queued:
            raise AssertionError(f"Unexpected request: {url}")
        return queued.pop(0)


def game_config_payload():
    return {
        "success": True,
        "data": {
            "gameConfigList": [
                {
                    "gameid": "h55china",
                    "jf_gameid": "h55",
                    "mpay_appid": "aecfrt3rmaaaaajl-g-h55",
                    "app_name": "第五人格",
                    "iconimg": "https://example.test/h55.png",
                    "client_log_key": "static-public-log-key",
                }
            ]
        },
    }


def homepage_payload():
    return {
        "code": 200,
        "data": {
            "recommends": [
                {
                    "app_data": {
                        "app_id": 73,
                        "app_type": 1,
                        "display_name": "第五人格",
                        "logo": "https://example.test/launcher-h55.png",
                    }
                },
                {
                    "app_data": {
                        "app_id": 108,
                        "app_type": 3,
                        "display_name": "阴阳师",
                        "goods_image": "https://example.test/g37-cover.png",
                    }
                },
                {
                    "app_data": {
                        "app_id": 93,
                        "app_type": 2,
                        "display_name": "网易云音乐",
                    }
                },
            ]
        },
    }


def app_detail_payload():
    return {
        "code": 200,
        "data": {
            "app_id": 73,
            "game_id": "h55",
            "display_name": "第五人格",
            "logo": "https://example.test/h55-detail.png",
        },
    }


class DynamicGameCatalogTests(unittest.TestCase):
    def make_responses(self, source_headers=None):
        headers = source_headers or {}
        return {
            DynamicGameCatalog.GAME_CONFIG_URL: [
                FakeResponse(game_config_payload(), headers=headers)
            ],
            DynamicGameCatalog.HOMEPAGE_URL: [
                FakeResponse(homepage_payload(), headers=headers)
            ],
            DynamicGameCatalog.APP_DETAIL_URL.format(73): [
                FakeResponse(app_detail_payload())
            ],
            DynamicGameCatalog.APP_DETAIL_URL.format(108): [
                FakeResponse({"code": 200, "data": {
                    "app_id": 108,
                    "game_id": "g37",
                    "display_name": "阴阳师",
                    "main_image": "https://example.test/g37-main.png",
                }})
            ],
        }

    def test_maps_launcher_game_id_to_cloud_config_and_download_distribution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = FakeSession(self.make_responses())
            catalog = DynamicGameCatalog(temp_dir, session=session)

            self.assertTrue(catalog.refresh(force=True))

            game = catalog.get_game("h55")
            self.assertEqual(game["game_id"], "aecfrt3rmaaaaajl-g-h55")
            self.assertEqual(game["download_distributions"], [73])
            self.assertEqual(
                catalog.resolve_cloud_game_id("h55"),
                "aecfrt3rmaaaaajl-g-h55",
            )
            self.assertEqual(
                catalog.get_cloud_config("h55")["mpay_appid"],
                "aecfrt3rmaaaaajl-g-h55",
            )
            self.assertEqual(
                catalog.get_feature("aecfrt3rmaaaaajl-g-h55")[
                    "download_distributions"
                ],
                [73],
            )
            native_game = catalog.get_game("g37")
            self.assertEqual(native_game["platform_type"], "native_pc")
            self.assertEqual(native_game["download_distributions"], [])
            self.assertEqual(native_game["launcher"]["main_image"], "https://example.test/g37-main.png")
            self.assertEqual(len(catalog.get_games()), 2)

            config_call = next(
                call for call in session.calls
                if call[0] == DynamicGameCatalog.GAME_CONFIG_URL
            )
            self.assertEqual(config_call[1]["headers"], {"X-Game": "base"})
            homepage_call = next(
                call for call in session.calls
                if call[0] == DynamicGameCatalog.HOMEPAGE_URL
            )
            self.assertEqual(homepage_call[1]["headers"], {})
            self.assertTrue(
                os.path.exists(os.path.join(temp_dir, "dynamic_game_catalog.json"))
            )

    def test_fresh_local_cache_avoids_network_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_session = FakeSession(self.make_responses())
            first = DynamicGameCatalog(temp_dir, session=first_session)
            first.refresh(force=True)

            cached_session = FakeSession()
            cached = DynamicGameCatalog(
                temp_dir,
                session=cached_session,
                refresh_interval=24 * 60 * 60,
            )

            self.assertFalse(cached.refresh())
            self.assertEqual(cached_session.calls, [])
            self.assertEqual(cached.get_game("h55")["download_distributions"], [73])

    def test_stale_cache_uses_http_validators_and_reuses_parsed_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_headers = {
                "ETag": '"catalog-v1"',
                "Last-Modified": "Sun, 19 Jul 2026 00:00:00 GMT",
            }
            first = DynamicGameCatalog(
                temp_dir,
                session=FakeSession(self.make_responses(source_headers)),
                refresh_interval=0,
            )
            first.refresh(force=True)

            second_session = FakeSession({
                DynamicGameCatalog.GAME_CONFIG_URL: [FakeResponse(status_code=304)],
                DynamicGameCatalog.HOMEPAGE_URL: [FakeResponse(status_code=304)],
            })
            second = DynamicGameCatalog(
                temp_dir, session=second_session, refresh_interval=0
            )

            self.assertFalse(second.refresh(force=True))
            self.assertEqual(len(second_session.calls), 2)
            for _, kwargs in second_session.calls:
                self.assertEqual(kwargs["headers"]["If-None-Match"], '"catalog-v1"')
                self.assertEqual(
                    kwargs["headers"]["If-Modified-Since"],
                    "Sun, 19 Jul 2026 00:00:00 GMT",
                )
            self.assertEqual(second.get_game("h55")["download_distributions"], [73])

    def test_volatile_response_envelope_does_not_invalidate_catalog(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_config = game_config_payload()
            first_config.update({"time": 1, "trace_id": "first"})
            first_responses = self.make_responses()
            first_responses[DynamicGameCatalog.GAME_CONFIG_URL] = [
                FakeResponse(first_config)
            ]
            first = DynamicGameCatalog(
                temp_dir,
                session=FakeSession(first_responses),
                refresh_interval=0,
            )
            first.refresh(force=True)

            second_config = game_config_payload()
            second_config.update({"time": 2, "trace_id": "second"})
            second_session = FakeSession({
                DynamicGameCatalog.GAME_CONFIG_URL: [FakeResponse(second_config)],
                DynamicGameCatalog.HOMEPAGE_URL: [FakeResponse(homepage_payload())],
            })
            second = DynamicGameCatalog(
                temp_dir, session=second_session, refresh_interval=0
            )

            self.assertFalse(second.refresh(force=True))
            self.assertEqual(len(second_session.calls), 2)


if __name__ == "__main__":
    unittest.main()
