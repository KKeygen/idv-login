"""Offline tests of the real Honor protocol class, with no Qt/UI/network startup."""
import ast
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET


SOURCE = Path(__file__).resolve().parents[1] / "src/channelHandler/honorLogin/account.py"


class Logger:
    def __init__(self):
        self.messages = []

    def error(self, message, *args):
        self.messages.append(message.format(*args))

    warning = error
    debug = error


def load_protocol(logger):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             or isinstance(node, ast.ClassDef) and node.name == "HonorAccountSession"]
    namespace = {
        "base64": base64, "hashlib": hashlib, "random": random, "time": time,
        "uuid": uuid, "ET": ET, "setup_logger": lambda: logger,
        "requests": types.SimpleNamespace(Session=lambda: object()),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["HonorAccountSession"]


class HonorAccountResponseTests(unittest.TestCase):
    def setUp(self):
        self.logger = Logger()
        self.client_class = load_protocol(self.logger)
        self.client = self.client_class({"deviceId": "fixture-device"})
        self.client.qr_info = {"qrCode": "fixture-qr", "qrSiteID": "1"}

    def exchange(self, body, scan=None):
        self.client._post = lambda *args, **kwargs: types.SimpleNamespace(
            content=body, status_code=200)
        return self.client.exchange(scan or {"userID": "fixture-scan-user"}, {})

    def test_preserves_entire_xml_byte_for_byte_and_compatibility_fields(self):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\r\n'
            '<LoginByQrCodeRsp resultCode="0" unknownAttribute="kept">'
            '<serviceToken>fixture-st</serviceToken><userID>fixture-user</userID>'
            '<nickName>测试 &amp; 名称</nickName><expireTime unit="seconds">86400</expireTime>'
            '<future><value a="1">first</value><value a="2">second</value></future>'
            '<vendor:extra xmlns:vendor="urn:fixture">kept</vendor:extra>'
            '</LoginByQrCodeRsp>'
        ).encode("utf-8")
        before = time.time()
        result = self.exchange(body)
        raw = result["accountExchangeResponse"]
        self.assertEqual(base64.b64decode(raw["bodyBase64"], validate=True), body)
        self.assertEqual(raw["httpStatus"], 200)
        self.assertEqual(raw["format"], "xml")
        self.assertEqual(raw["endpoint"], "/AccountServer/IDM/loginByQrCode")
        self.assertGreaterEqual(raw["capturedAtUnix"], before)
        self.assertLessEqual(raw["capturedAtUnix"], time.time())
        self.assertEqual({key: result[key] for key in (
            "serviceToken", "siteId", "deviceId", "deviceType", "userId", "nickName")}, {
                "serviceToken": "fixture-st", "siteId": "1", "deviceId": "fixture-device",
                "deviceType": "6", "userId": "fixture-user", "nickName": "测试 & 名称",
            })
        self.assertEqual(self.logger.messages, [])

    def test_json_round_trip_preserves_raw_response(self):
        body = b'<Rsp resultCode="0"><serviceToken>fixture-st</serviceToken></Rsp>'
        result = self.exchange(body)
        restored = json.loads(json.dumps({"accountSession": result}))["accountSession"]
        self.assertEqual(restored, result)
        self.assertEqual(base64.b64decode(restored["accountExchangeResponse"]["bodyBase64"]), body)

    def test_does_not_invent_expiry_metadata(self):
        result = self.exchange(b'<Rsp><serviceToken>fixture-st</serviceToken></Rsp>')
        self.assertEqual(set(result), {
            "serviceToken", "siteId", "deviceId", "deviceType", "userId", "nickName",
            "accountExchangeResponse",
        })
        self.assertNotIn("expireTime", result["accountExchangeResponse"])

    def test_scan_user_fallback_remains_compatible(self):
        result = self.exchange(b'<Rsp><serviceToken>fixture-st</serviceToken></Rsp>')
        self.assertEqual(result["userId"], "fixture-scan-user")

    def test_business_rejection_does_not_log_raw_credentials(self):
        result = self.exchange(
            b'<Rsp resultCode="1"><serviceToken>fixture-secret</serviceToken></Rsp>')
        self.assertIsNone(result)
        self.assertTrue(self.logger.messages)
        self.assertNotIn("fixture-secret", "\n".join(self.logger.messages))
        self.assertNotIn("<Rsp", "\n".join(self.logger.messages))

    def test_missing_token_is_still_rejected_without_raw_logging(self):
        result = self.exchange(b'<Rsp resultCode="0"><privateField>fixture-private</privateField></Rsp>')
        self.assertIsNone(result)
        self.assertNotIn("fixture-private", "\n".join(self.logger.messages))

    def test_malformed_xml_is_rejected_without_credential_traceback(self):
        result = self.exchange(b'<Rsp><serviceToken>fixture-secret</broken>')
        self.assertIsNone(result)
        self.assertTrue(self.logger.messages)
        self.assertNotIn("fixture-secret", "\n".join(self.logger.messages))

    def test_real_manager_save_and_handler_restore_preserve_full_response(self):
        body = b'<Rsp><serviceToken>fixture-st</serviceToken><unknown>kept</unknown></Rsp>'
        session = self.exchange(body)
        src = SOURCE.parents[2]
        handler_tree = ast.parse((src / "channelHandler/honorChannelHandler.py").read_text(encoding="utf-8"))
        handler = next(node for node in handler_tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "honorChannel")
        handler.body = [node for node in handler.body if isinstance(node, ast.FunctionDef)
                        and node.name in ("__init__", "from_dict")]
        class BaseChannel:
            def __init__(self, login_info, *args, **kwargs):
                self.login_info = login_info
                self.channel_name = login_info["login_channel"]
                self.name = "fixture-account"
            def before_save(self):
                pass
        namespace = {
            "time": time, "channelmgr": types.SimpleNamespace(channel=BaseChannel),
            "setup_logger": lambda: self.logger, "getShortGameId": lambda game: game,
            "CloudRes": lambda: types.SimpleNamespace(get_channelData=lambda *args: {"honor_sdk": {}}),
            "HONOR_APK_VER_NAME": "fixture-version",
            "HonorLogin": lambda config, token, saved, game: types.SimpleNamespace(accountSession=dict(saved)),
        }
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "honorChannelHandler.py", "exec"), namespace)
        record_class = namespace["honorChannel"]
        record = record_class({"login_channel": "honor_sdk"}, accountSession=session, game_id="fixture-game")

        manager_tree = ast.parse((src / "channelmgr.py").read_text(encoding="utf-8"))
        manager = next(node for node in manager_tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "ChannelManager")
        save = next(node for node in manager.body if isinstance(node, ast.FunctionDef)
                    and node.name == "save_records")
        spec = importlib.util.spec_from_file_location("secure_write", src / "secure_write.py")
        secure_write = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(secure_write)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            save_namespace = {"json": json, "genv": {"FP_CHANNEL_RECORD": str(path)}}
            exec(compile(ast.Module(body=[save], type_ignores=[]), "channelmgr.py", "exec"), save_namespace)
            with patch.dict(sys.modules, {"secure_write": secure_write}):
                save_namespace["save_records"](types.SimpleNamespace(channels=[record], logger=self.logger))
            serialized = json.loads(path.read_text(encoding="utf-8"))[0]
            restored = record_class.from_dict(serialized)
            self.assertEqual(restored.accountSession, session)
            self.assertEqual(restored.honorLogin.accountSession, session)
            self.assertEqual(base64.b64decode(restored.accountSession["accountExchangeResponse"]["bodyBase64"]), body)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_saved_raw_response_is_not_sent_in_silent_login(self):
        raw = {"bodyBase64": "fixture-private-response"}
        client = self.client_class({
            "serviceToken": "fixture-st", "deviceId": "fixture-device",
            "userId": "fixture-user", "accountExchangeResponse": raw,
        })
        calls = []
        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return types.SimpleNamespace(json=lambda: {"code": "fixture-code"})
        client._post = post
        code = client.authorization_code({"package_name": "fixture.game", "app_id": "fixture-app"})
        self.assertEqual(code, "fixture-code")
        self.assertNotIn("fixture-private-response", repr(calls))
        self.assertEqual(client.saved["accountExchangeResponse"], raw)

    def test_old_sessions_without_raw_response_still_work(self):
        client = self.client_class({"serviceToken": "fixture-st"})
        client._post = lambda *args, **kwargs: types.SimpleNamespace(json=lambda: {"code": "fixture-code"})
        self.assertEqual(client.authorization_code({"package_name": "fixture.game", "app_id": "fixture-app"}),
                         "fixture-code")
        self.assertNotIn("accountExchangeResponse", client.saved)


if __name__ == "__main__":
    unittest.main()
