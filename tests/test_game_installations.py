import os
import sys
import tempfile
import unittest
import logging
import types
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Keep these model tests independent from the optional Qt/runtime stack.
app_state = types.ModuleType("app_state")
app_state.toast = lambda *args, **kwargs: None
sys.modules["app_state"] = app_state

envmgr = types.ModuleType("envmgr")
_genv_store = {}


def _genv_get(key, default=None):
    return _genv_store.get(key, default)


def _genv_set(key, value, cached=False):
    _genv_store[key] = value


envmgr.genv = types.SimpleNamespace(get=_genv_get, set=_genv_set)
sys.modules["envmgr"] = envmgr

logutil = types.ModuleType("logutil")
logutil.setup_logger = lambda: logging.getLogger("game-installation-tests")
sys.modules["logutil"] = logutil

cloud_res = types.ModuleType("cloudRes")
cloud_res.CloudRes = type(
    "CloudRes",
    (),
    {
        "is_convert_to_normal": lambda self, game_id: False,
        "get_start_argument": lambda self, game_id: "",
    },
)
sys.modules["cloudRes"] = cloud_res

channel_handler = types.ModuleType("channelHandler")
channel_utils = types.ModuleType("channelHandler.channelUtils")
channel_utils.getShortGameId = lambda game_id: str(game_id or "").split("-")[-1]
channel_utils.cmp_game_id = lambda left, right: (
    channel_utils.getShortGameId(left) == channel_utils.getShortGameId(right)
)
sys.modules["channelHandler"] = channel_handler
sys.modules["channelHandler.channelUtils"] = channel_utils

xxhash = types.ModuleType("xxhash")
xxhash.xxh64 = lambda: None
sys.modules["xxhash"] = xxhash

from gamemgr import Game, GameInstallation, GameManager


class GameInstallationModelTests(unittest.TestCase):
    def _manager(self, game=None):
        manager = GameManager.__new__(GameManager)
        manager.games = {game.game_id: game} if game else {}
        manager._save_games = lambda: None
        manager.logger = getattr(game, "logger", None)
        return manager

    def test_legacy_record_migrates_to_one_stable_installation(self):
        legacy = {
            "game_id": "aec-example-g-h55",
            "name": "第五人格",
            "path": "D:/Games/dwrg.exe",
            "version": "v3_old",
            "default_distribution": 73,
        }
        game = Game.from_dict(legacy)
        self.assertEqual(len(game.installations), 1)
        installation = game.get_installation()
        self.assertEqual(installation.distribution_id, 73)
        self.assertEqual(installation.installed_version, "v3_old")

        reloaded = Game.from_dict(game.to_dict(), game.to_installation_state())
        self.assertEqual(reloaded.default_installation_id, game.default_installation_id)
        self.assertEqual(len(reloaded.installations), 1)

        saved = game.to_dict()
        self.assertEqual(saved["path"], "D:/Games/dwrg.exe")
        self.assertEqual(saved["version"], "v3_old")
        self.assertEqual(saved["default_distribution"], 73)
        self.assertNotIn("installations", saved)
        self.assertNotIn("default_installation_id", saved)
        self.assertNotIn("installation_state_v1", saved)
        self.assertEqual(game.to_installation_state()["schema_version"], 1)

    def test_downgraded_client_changes_are_merged_back_into_v1_state(self):
        game = Game("h55")
        first = game.add_installation("D:/Games/first.exe", 73, "v3_new")
        second = game.add_installation("D:/Games/second.exe", 134, "v3_fever")
        game.set_default_installation(first.installation_id)
        saved = game.to_dict()
        installation_state = game.to_installation_state()

        # Simulate an old release that only knows and rewrites legacy fields.
        saved["path"] = second.path
        saved["version"] = "v3_downgraded"
        saved["default_distribution"] = 134
        reloaded = Game.from_dict(saved, installation_state)

        self.assertEqual(reloaded.default_installation_id, second.installation_id)
        self.assertEqual(reloaded.path, second.path)
        self.assertEqual(reloaded.version, "v3_downgraded")
        self.assertEqual(reloaded.default_distribution, 134)
        self.assertEqual(len(reloaded.installations), 2)

    def test_downgraded_path_only_change_keeps_known_installation_metadata(self):
        game = Game("h55")
        first = game.add_installation("D:/Games/first.exe", 73, "v3_first")
        second = game.add_installation("D:/Games/second.exe", 134, "v3_second")
        game.set_default_installation(first.installation_id)
        saved = game.to_dict()
        installation_state = game.to_installation_state()

        # The old client changes only path; its version/distribution values are
        # stale projections of the former default installation.
        saved["path"] = second.path
        reloaded = Game.from_dict(saved, installation_state)

        self.assertEqual(reloaded.default_installation_id, second.installation_id)
        self.assertEqual(reloaded.version, "v3_second")
        self.assertEqual(reloaded.default_distribution, 134)

    def test_development_flat_installation_state_is_still_readable(self):
        current = Game("h55")
        installation = current.add_installation("D:/Games/dwrg.exe", 73, "v3_beta")
        flat = current.to_dict()
        state = current.to_installation_state()
        flat["installations"] = state["installations"]
        flat["default_installation_id"] = state["default_installation_id"]

        reloaded = Game.from_dict(flat)
        self.assertEqual(reloaded.default_installation_id, installation.installation_id)
        self.assertEqual(reloaded.version, "v3_beta")

    def test_sidecar_survives_old_client_rewrite_and_restores_all_installations(self):
        _genv_store.clear()
        manager = self._manager()
        game = manager.get_game("h55")
        first = game.add_installation("D:/Games/first.exe", 73, "v3_first")
        second = game.add_installation("D:/Games/second.exe", 134, "v3_second")
        game.set_default_installation(first.installation_id)
        manager._save_games = GameManager._save_games.__get__(manager, GameManager)
        manager._save_games()

        # An old client rewrites only game_settings and cannot see the sidecar.
        legacy = _genv_store[GameManager.GAMES_CACHE_KEY]["h55"]
        legacy["path"] = second.path
        legacy["version"] = "v3_old_client"
        legacy["default_distribution"] = 134

        reloaded = GameManager()
        restored = reloaded.get_existing_game("h55")
        self.assertEqual(len(restored.installations), 2)
        self.assertEqual(restored.default_installation_id, second.installation_id)
        self.assertEqual(restored.version, "v3_old_client")
        self.assertEqual(
            len(_genv_store[GameManager.INSTALLATIONS_CACHE_KEY]["h55"]["installations"]),
            2,
        )

    def test_two_distributions_keep_independent_paths_and_versions(self):
        game = Game("h55")
        first = game.add_installation(
            "D:/Games/new/dwrg.exe", 73, "v3_new", source="download"
        )
        second = game.add_installation(
            "D:/Fever/dwrg.exe", 134, "v3_fever", source="fever"
        )

        self.assertEqual(len(game.installations), 2)
        self.assertEqual(first.path, "D:/Games/new/dwrg.exe")
        self.assertEqual(first.installed_version, "v3_new")
        self.assertEqual(second.path, "D:/Fever/dwrg.exe")
        self.assertEqual(second.installed_version, "v3_fever")
        self.assertEqual(game.default_installation_id, second.installation_id)

    def test_successful_start_returns_true(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = os.path.join(temp_dir, "game.exe")
            Path(executable).touch()
            game = Game("h55")
            installation = game.add_installation(executable, 73)
            app_state.proxy_mgr = None
            with mock.patch("gamemgr.subprocess.Popen") as popen:
                self.assertTrue(game.start(installation.installation_id))
            popen.assert_called_once()

    def test_reselecting_new_valid_path_adds_manual_installation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "first.exe")
            second_path = os.path.join(temp_dir, "second.exe")
            Path(first_path).touch()
            Path(second_path).touch()

            game = Game("h55")
            first = game.add_installation(first_path, 73, source="download")
            manager = self._manager(game)

            self.assertTrue(manager.set_game_path("h55", second_path))
            self.assertEqual(len(game.installations), 2)
            selected = game.get_installation()
            self.assertEqual(selected.path, second_path)
            self.assertEqual(selected.source, "manual")
            self.assertEqual(first.distribution_id, 73)

            self.assertTrue(manager.set_game_path("h55", first_path))
            self.assertEqual(len(game.installations), 2)
            self.assertEqual(game.default_installation_id, first.installation_id)

    def test_setting_path_for_game_without_any_record_creates_manual_installation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = os.path.join(temp_dir, "new-game.exe")
            Path(executable).touch()
            manager = self._manager()

            self.assertTrue(manager.set_game_path("new-game", executable))
            game = manager.get_existing_game("new-game")
            self.assertIsNotNone(game)
            self.assertEqual(len(game.installations), 1)
            self.assertEqual(game.get_installation().path, executable)
            self.assertEqual(game.get_installation().source, "manual")

    def test_unrecorded_path_does_not_inherit_stale_distribution_or_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = os.path.join(temp_dir, "missing.exe")
            replacement_path = os.path.join(temp_dir, "replacement.exe")
            Path(replacement_path).touch()

            game = Game("h55")
            installation = game.add_installation(
                missing_path, 73, "v3_old", source="download"
            )
            manager = self._manager(game)

            self.assertTrue(manager.set_game_path("h55", replacement_path))
            self.assertEqual(len(game.installations), 1)
            self.assertNotIn(installation.installation_id, game.installations)
            replacement = game.get_installation()
            self.assertEqual(replacement.path, replacement_path)
            self.assertEqual(replacement.distribution_id, -1)
            self.assertEqual(replacement.installed_version, "")
            self.assertEqual(replacement.source, "manual")

    def test_marker_restores_download_identity_when_path_is_reselected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = os.path.join(temp_dir, "dwrg.exe")
            Path(executable).touch()
            original = GameInstallation(
                installation_id="install-73",
                path=executable,
                distribution_id=73,
                installed_version="v3_current",
                source="download",
                content_id=434,
                startup_path="dwrg.exe",
            )
            self.assertTrue(original.write_marker("h55"))

            game = Game("h55")
            manager = self._manager(game)
            self.assertTrue(manager.set_game_path("h55", executable))
            restored = game.get_installation()
            self.assertEqual(restored.installation_id, "install-73")
            self.assertEqual(restored.distribution_id, 73)
            self.assertEqual(restored.installed_version, "v3_current")
            self.assertEqual(restored.content_id, 434)

    def test_fever_import_adds_installation_instead_of_overwriting(self):
        game = Game("h55")
        existing = game.add_installation("D:/Standalone/dwrg.exe", 73, source="download")
        manager = self._manager(game)
        manager.list_fever_games = lambda: [{
            "game_id": "aec-example-g-h55",
            "display_name": "第五人格",
            "path": "D:/Fever/dwrg.exe",
            "distribution_id": 134,
        }]
        old_toast = app_state.toast
        app_state.toast = lambda *args, **kwargs: None
        try:
            imported = manager.import_fever_game(
                "h55", distribution_id=134, path="D:/Fever/dwrg.exe"
            )
        finally:
            app_state.toast = old_toast

        self.assertEqual(imported, "h55")
        self.assertEqual(len(game.installations), 2)
        fever = game.get_installation()
        self.assertEqual(fever.distribution_id, 134)
        self.assertEqual(fever.source, "fever")
        self.assertEqual(existing.path, "D:/Standalone/dwrg.exe")


if __name__ == "__main__":
    unittest.main()
