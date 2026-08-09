import base64
import json
import os
import sys
import types
import unittest


SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Genv:
    @staticmethod
    def set(*args, **kwargs):
        return None


def _install_import_stubs():
    channelmgr = types.ModuleType("channelmgr")
    channelmgr.channel = type("channel", (), {})
    sys.modules["channelmgr"] = channelmgr

    cloud_res = types.ModuleType("cloudRes")
    cloud_res.CloudRes = type("CloudRes", (), {})
    sys.modules["cloudRes"] = cloud_res

    envmgr = types.ModuleType("envmgr")
    envmgr.genv = _Genv()
    sys.modules["envmgr"] = envmgr

    app_state = types.ModuleType("app_state")
    app_state.fake_device = {"udid": "test-udid"}
    sys.modules["app_state"] = app_state

    logutil = types.ModuleType("logutil")
    logutil.setup_logger = lambda: _Logger()
    sys.modules["logutil"] = logutil

    channel_utils = types.ModuleType("channelHandler.channelUtils")
    channel_utils.getShortGameId = lambda game_id: game_id
    channel_utils.buildSAUTH = lambda *args, **kwargs: {"session": args[3]}
    channel_utils.postSignedData = lambda *args, **kwargs: {
        "unisdk_login_json": base64.b64encode(
            json.dumps({"username": "test-user"}).encode()
        ).decode()
    }
    sys.modules["channelHandler.channelUtils"] = channel_utils

    vivo_login = types.ModuleType("channelHandler.vivoLogin.vivoChannel")
    vivo_login.VivoLogin = type("VivoLogin", (), {})
    sys.modules["channelHandler.vivoLogin.vivoChannel"] = vivo_login

    qt_widgets = types.ModuleType("PyQt6.QtWidgets")
    for name in (
        "QApplication",
        "QDialog",
        "QDialogButtonBox",
        "QLabel",
        "QListWidget",
        "QVBoxLayout",
        "QWidget",
        "QCheckBox",
    ):
        setattr(qt_widgets, name, type(name, (), {}))
    pyqt6 = types.ModuleType("PyQt6")
    pyqt6.QtWidgets = qt_widgets
    sys.modules["PyQt6"] = pyqt6
    sys.modules["PyQt6.QtWidgets"] = qt_widgets


_install_import_stubs()
from channelHandler.vivoChannelHandler import vivoChannel  # noqa: E402


class _VivoLogin:
    def __init__(self, tokens, web_result=None):
        self.tokens = iter(tokens)
        self.web_result = web_result
        self.cookies = {"session": "cookie"}
        self.calls = []

    def loginSubAccount(self, sub_open_id):
        self.calls.append(sub_open_id)
        return next(self.tokens)

    def webLogin(self, cookies=None, on_complete=None):
        if on_complete is not None:
            on_complete(self.web_result)
            return None
        return self.web_result


class VivoTokenRefreshTests(unittest.TestCase):
    def _make_channel(self, tokens):
        channel = object.__new__(vivoChannel)
        channel.logger = _Logger()
        channel.uuid = "nearme_vivo-test"
        channel.name = "vivo-test"
        channel.channel_name = "nearme_vivo"
        channel.game_id = "g37"
        channel.session = object()
        channel.activeAccount = types.SimpleNamespace(
            subOpenId="sub-account", openToken="old-token"
        )
        channel.vivoLogin = _VivoLogin(tokens)
        channel.uniBody = None
        channel.uniData = None
        return channel

    def test_reuses_session_but_refreshes_open_token_before_build(self):
        channel = self._make_channel(["new-token"])

        result = channel.get_uniSdk_data()

        self.assertEqual(channel.vivoLogin.calls, ["sub-account"])
        self.assertEqual(channel.activeAccount.openToken, "new-token")
        self.assertEqual(
            base64.b64decode(result["token"]).decode(),
            "new-token",
        )

    def test_refresh_failure_stops_login_data_build(self):
        channel = self._make_channel([None])

        result = channel.get_uniSdk_data()

        self.assertIsNone(result)
        self.assertEqual(channel.activeAccount.openToken, "")
        self.assertFalse(channel.is_token_valid())

    def test_initial_union_use_failure_is_not_reported_as_success(self):
        channel = self._make_channel([None])
        channel.session = None
        channel.activeAccount = None
        channel.cookies = {}
        channel.chosenAccount = ""
        channel.vivoLogin.web_result = {
            "nickName": "main-account",
            "subAccounts": [
                {"nickName": "sub", "subOpenId": "sub-account"},
            ],
        }

        self.assertFalse(channel.request_user_login())
        self.assertFalse(channel.is_token_valid())


if __name__ == "__main__":
    unittest.main()
