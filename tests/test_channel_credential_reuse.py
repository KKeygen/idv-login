"""Offline A→B adapter regressions. All credentials below are synthetic."""
import ast
import base64
import copy
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


SRC = Path(__file__).resolve().parents[1] / "src" / "channelHandler"
NOW = 1000


def adapter(path, name, methods, **extra):
    """Execute only the production methods under test, never app/GUI imports."""
    tree = ast.parse((SRC / path).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    cls.bases = [ast.Name(id="Base", ctx=ast.Load())]
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls]
    ns = dict(Base=object, time=types.SimpleNamespace(time=lambda: NOW), json=json,
              getShortGameId=lambda game: game, genv=types.SimpleNamespace(set=Mock()),
              safe_exception=Mock())
    ns.update(extra)
    exec(compile(ast.fix_missing_locations(tree), str(SRC / path), "exec"), ns)
    return ns[name], ns


class OppoTests(unittest.TestCase):
    def record(self):
        cls, _ = adapter("oppoChannelHandler.py", "oppoChannel", ("_login_gamesdk",))
        rec = cls()
        rec._pick_secondary_token = Mock(return_value="fixture-htms")
        rec.refresh_before_use = Mock()
        return rec

    @staticmethod
    def result(code="200", ticket="fixture-ticket"):
        return types.SimpleNamespace(code=code, ticket=ticket)

    def test_valid_htms_issues_b_without_refreshing_a(self):
        rec = self.record()
        client = types.SimpleNamespace(user_login=Mock(return_value=self.result()))
        token, result = rec._login_gamesdk(client, "fixture.pkg")
        self.assertEqual(token, "fixture-htms")
        self.assertEqual(result.ticket, "fixture-ticket")
        rec.refresh_before_use.assert_not_called()
        client.user_login.assert_called_once()

    def test_rejected_htms_refreshes_once_and_uses_new_token(self):
        rec = self.record()
        rec._pick_secondary_token.side_effect = ["fixture-old", "fixture-new"]
        client = types.SimpleNamespace(user_login=Mock(side_effect=[self.result("401", ""), self.result()]))
        token, _ = rec._login_gamesdk(client, "fixture.pkg")
        self.assertEqual(token, "fixture-new")
        rec.refresh_before_use.assert_called_once()
        self.assertEqual([c.kwargs["secondary_token"] for c in client.user_login.call_args_list],
                         ["fixture-old", "fixture-new"])

    def test_missing_htms_refreshes_before_first_game_request(self):
        rec = self.record()
        rec._pick_secondary_token.side_effect = ["", "fixture-new"]
        client = types.SimpleNamespace(user_login=Mock(return_value=self.result()))
        rec._login_gamesdk(client, "fixture.pkg")
        rec.refresh_before_use.assert_called_once()
        client.user_login.assert_called_once_with(pkg_name="fixture.pkg", secondary_token="fixture-new")

    def test_transport_error_does_not_needlessly_refresh_a(self):
        rec = self.record()
        client = types.SimpleNamespace(user_login=Mock(side_effect=OSError("fixture-network")))
        with self.assertRaises(OSError):
            rec._login_gamesdk(client, "fixture.pkg")
        rec.refresh_before_use.assert_not_called()

    def test_failed_refresh_cannot_continue_with_old_htms(self):
        rec = self.record()
        rec.refresh_before_use.side_effect = RuntimeError("fixture-refresh-failed")
        client = types.SimpleNamespace(user_login=Mock(return_value=self.result("401", "")))
        with self.assertRaises(RuntimeError):
            rec._login_gamesdk(client, "fixture.pkg")
        client.user_login.assert_called_once()

    def test_actual_refresh_success_false_raises(self):
        client = types.SimpleNamespace(session=types.SimpleNamespace(session_ticket="fixture-session"),
                                       token_refresh=Mock(return_value={"success": False}))
        module = types.ModuleType("channelHandler.oppoOpenAccount.client")
        module.OppoOpenAccountClient = Mock(return_value=client)
        module.DEFAULT_BASE_URL = "https://example.invalid/"
        cls, _ = adapter("oppoChannelHandler.py", "oppoChannel", ("refresh_before_use",),
                         prefer_device_id_from_login_resp=lambda *a: None, DEFAULT_CONSTS=None,
                         _now_ms=lambda: NOW * 1000)
        rec = cls()
        rec._ensure_authorized = Mock(return_value=True)
        rec._extract_login_tokens = Mock(return_value={key: "fixture" for key in (
            "refresh_token", "ssoid", "primary_token", "refresh_ticket", "access_token", "secondary_token_map")})
        rec.loginResp, rec.oppo_open_account = {}, {}
        with patch.dict(sys.modules, {module.__name__: module}), self.assertRaisesRegex(RuntimeError, "token_refresh"):
            rec.refresh_before_use()


class HuaweiTests(unittest.TestCase):
    def record(self):
        issue = Mock(return_value={"access_token": "fixture-new", "expires_in": 600})
        cls, _ = adapter("huaLogin/huaChannel.py", "HuaweiLogin",
                         ("ensure_game_token", "invalidate_game_token"), silent_token=issue)
        rec = cls()
        rec.serviceToken, rec.accessToken = "fixture-st", "fixture-at"
        rec.expiredTime = NOW + 100
        rec._device_id = lambda: "fixture-device"
        rec._at_binding = (rec.serviceToken, "h55", "fixture.pkg", "fixture-app", "fixture-device")
        rec.logger = Mock()
        return rec, issue, {"package_name": "fixture.pkg", "app_id": "fixture-app"}

    def test_refreshing_b_preserves_valid_bound_upstream_token(self):
        rec, issue, config = self.record()
        self.assertTrue(rec.ensure_game_token(config, "h55"))
        issue.assert_not_called()

    def test_expired_intermediate_token_is_renewed_not_account_st(self):
        rec, issue, config = self.record()
        rec.expiredTime = NOW + 59
        self.assertTrue(rec.ensure_game_token(config, "h55"))
        issue.assert_called_once()
        self.assertEqual(rec.serviceToken, "fixture-st")
        self.assertEqual(rec.expiredTime, NOW + 600)

    def test_different_game_cannot_reuse_bound_intermediate_token(self):
        rec, issue, config = self.record()
        self.assertTrue(rec.ensure_game_token(config, "g37"))
        issue.assert_called_once()


class LenovoTests(unittest.TestCase):
    def test_st_deadline_reuses_packet_through_actual_common_policy(self):
        tree = ast.parse((SRC.parent / "channel_cache.py").read_text())
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ("packet_auth", "packet_identity", "unisdk_expires_at", "credential_expires_at")]
        cache = dict(base64=base64, json=json, getShortGameId=lambda game: game, unquote=lambda value: value)
        exec(compile(tree, "channel_cache.py", "exec"), cache)
        base, _ = adapter("../channelmgr.py", "channel", ("cached_unisdk",),
                          copy=copy, packet_identity=cache["packet_identity"],
                          unisdk_expires_at=cache["unisdk_expires_at"])
        cls, _ = adapter("lenovoChannelHandler.py", "lenovoChannel", ("cached_unisdk",),
                         Base=base, st_is_valid=lambda saved: saved["st_issued_at"] <= NOW < saved["st_issued_at"] + saved["st_ttl"])
        rec = cls()
        rec.channel_name, rec.uuid, rec.expired_games = "lenovo_open", "fixture-record", []
        rec.lenovoSession = {"st_issued_at": NOW - 10, "st_ttl": 100, "tgt_ttl": 10000}
        auth = dict(gameid="h55", login_channel="lenovo_open", sdkuid="0", sessionid="fixture-st")
        packet = dict(login_channel="lenovo_open", extra_unisdk_data=json.dumps({
            "SAUTH_JSON": base64.b64encode(json.dumps(auth).encode()).decode()}))
        deadline = cache["credential_expires_at"](rec)
        self.assertEqual(deadline, NOW + 90)
        rec.unisdk_cache = {"h55": {"packet": packet, "expires_at": deadline}}
        self.assertEqual(rec.cached_unisdk("h55", for_login=True), packet)

    def test_cached_st_honors_its_service_ttl_and_shared_policy(self):
        class Base:
            def cached_unisdk(self, game, *, for_login=False):
                return (game, for_login)
        cls, _ = adapter("lenovoChannelHandler.py", "lenovoChannel", ("cached_unisdk",),
                         Base=Base, st_is_valid=lambda saved: saved["st_issued_at"] <= NOW < saved["st_issued_at"] + saved["st_ttl"])
        rec = cls()
        rec.lenovoSession = {"st_issued_at": NOW - 10, "st_ttl": 20}
        self.assertEqual(rec.cached_unisdk("h55", for_login=True), ("h55", True))
        rec.lenovoSession["st_ttl"] = 5
        self.assertIsNone(rec.cached_unisdk("h55", for_login=True))


class UCTests(unittest.TestCase):
    def record(self, expiry=0, expired=False):
        cls, _ = adapter("ucChannelHandler.py", "ucChannel",
                         ("_store_session", "_get_session_data", "_has_valid_sid", "get_uniSdk_data"))
        rec = cls()
        rec.ucSession = {"sid": "fixture-sid", "accountId": "fixture-user"}
        rec.sid_expire_time, rec.refreshToken = expiry, "fixture-refresh"
        rec.uuid, rec.game_id, rec.logger = "fixture-record", "h55", Mock()
        rec.observe_sdkuid = Mock()
        rec.is_login_expired = lambda game: expired
        rec._try_refresh = Mock(return_value=True)
        rec._build_unisdk_result = Mock(return_value={"fixture": "packet"})
        return rec

    def test_missing_timeout_is_unknown_not_twenty_four_hours(self):
        rec = self.record()
        self.assertTrue(rec._store_session({"sid": "fixture-new", "accountId": "fixture-user"}))
        self.assertEqual(rec.sid_expire_time, 0)

    def test_actual_timeout_sets_the_deadline(self):
        rec = self.record()
        self.assertTrue(rec._store_session({"data": {"sid": "fixture-new", "accountId": "fixture-user", "timeout": 123}}))
        self.assertEqual(rec.sid_expire_time, NOW + 123)

    def test_unknown_b_uses_refresh_but_dated_fresh_b_is_reused(self):
        for expiry in (0, NOW + 100):
            with self.subTest(expiry=expiry):
                rec = self.record(expiry)
                self.assertIsInstance(rec.get_uniSdk_data(interactive=False), dict)
                self.assertEqual(rec._try_refresh.call_count, 1 if expiry == 0 else 0)

    def test_explicit_sauth_failure_overrides_a_future_local_deadline(self):
        rec = self.record(NOW + 100, expired=True)
        rec.get_uniSdk_data(interactive=False)
        rec._try_refresh.assert_called_once()

    def test_failed_issuance_does_not_build_old_b(self):
        rec = self.record()
        rec._try_refresh.return_value = False
        self.assertIs(rec.get_uniSdk_data(interactive=False), False)
        rec._build_unisdk_result.assert_not_called()


class OtherCredentialTests(unittest.TestCase):
    def test_vivo_b_refresh_uses_current_account_and_cookie_without_web_login(self):
        for platform in ("windows", "legacy"):
            with self.subTest(platform=platform):
                cls, _ = adapter(f"vivoChannelHandler_{platform}.py", "vivoChannel", ("_refresh_open_token",))
                rec = cls()
                rec.activeAccount = types.SimpleNamespace(subOpenId="fixture-sub", openToken="fixture-old")
                rec.vivoLogin = types.SimpleNamespace(loginSubAccount=Mock(return_value="fixture-new"),
                    webLogin=Mock(), cookies={"fixture": "cookie"}, cookieRecords=None)
                self.assertTrue(rec._refresh_open_token())
                rec.vivoLogin.loginSubAccount.assert_called_once_with("fixture-sub")
                rec.vivoLogin.webLogin.assert_not_called()
                self.assertEqual(rec.activeAccount.openToken, "fixture-new")

    def test_qq_code_flow_uses_real_expiry_or_unknown_on_both_platforms(self):
        for platform in ("windows", "legacy"):
            for ttl in (None, 12345):
                with self.subTest(platform=platform, ttl=ttl):
                    data = {"ret": 0, "accessToken": "fixture-at", "openID": "fixture-id"}
                    if ttl is not None:
                        data["expiresIn"] = ttl
                    response = types.SimpleNamespace(json=lambda: {"code": 0, "data": data})
                    cls, _ = adapter(f"qqLogin/qqChannel_{platform}.py", "QQLogin", ("_exchange_code",),
                                     requests=types.SimpleNamespace(post=Mock(return_value=response)),
                                     should_verify_ssl=lambda: True)
                    rec = cls()
                    rec.qq_appid, rec.logger = "fixture-app", Mock()
                    self.assertEqual(rec._exchange_code("fixture-code")["atk_expire"], ttl or 0)

    def test_qq_implicit_flow_uses_callback_expiry_not_default(self):
        for platform in ("windows", "legacy"):
            for ttl in (None, 12345):
                with self.subTest(platform=platform, ttl=ttl):
                    response = types.SimpleNamespace(status_code=200, json=lambda: {"ret": 0})
                    cls, _ = adapter(f"qqLogin/qqChannel_{platform}.py", "QQLogin", ("_verify_qq_login",),
                                     requests=types.SimpleNamespace(get=Mock(return_value=response)),
                                     should_verify_ssl=lambda: True,
                                     sig_helper=lambda **kw: types.SimpleNamespace(hexdigest=lambda: "fixture-sign"))
                    rec = cls()
                    rec.qq_appid, rec.logger, rec._handle_judge_login = "fixture-app", Mock(), Mock()
                    result = rec._verify_qq_login("fixture-at", "fixture-id", expires_in=ttl)
                    self.assertEqual(result["atk_expire"], ttl or 0)

    def test_qihoo_unknown_or_rejected_b_reissues_from_cookie(self):
        for expiry, rejected in ((0, False), (NOW + 1000, False), (NOW + 1000, True)):
            with self.subTest(expiry=expiry, rejected=rejected):
                cls, _ = adapter("qihooChannelHandler.py", "qihooChannel",
                                 ("_has_valid_token", "get_uniSdk_data"))
                rec = cls()
                rec.uuid, rec.game_id = "fixture-record", "h55"
                rec._credentials = lambda: {"qid": "fixture-id", "access_token": "fixture-at"}
                rec.token_expire_time, rec.qt_cookie = expiry, {"Q": "fixture-cookie"}
                rec.is_login_expired = lambda game: rejected
                rec.request_user_login = Mock(return_value=True)
                rec._build_unisdk_result = Mock(return_value={"fixture": "packet"})
                self.assertIsInstance(rec.get_uniSdk_data(interactive=False), dict)
                self.assertEqual(rec.request_user_login.call_count, int(expiry == 0 or rejected))

    def test_qq_unknown_lifetime_is_not_an_invented_deadline(self):
        cls, _ = adapter("qqChannelHandler.py", "qqChannel", ("is_token_valid",))
        rec = cls()
        rec.session = types.SimpleNamespace(atk="fixture-at", openid="fixture-id", atk_expire=0)
        rec.token_issued_at = 1
        self.assertTrue(rec.is_token_valid())
        rec.session.atk_expire = 50
        self.assertFalse(rec.is_token_valid())

    def test_bilibili_auto_login_missing_expiry_does_not_invent_thirty_days(self):
        cls, _ = adapter("bilibiliChannelHandler.py", "bilibiliChannel", ("validate_token_online",),
                         call_auto_login=Mock(return_value={"code": 0, "uid": "fixture-id"}))
        rec = cls()
        data = {"uid": "fixture-id", "access_key": "fixture-key", "expires": 1}
        rec._get_login_data = lambda: data
        rec.biliLogin = types.SimpleNamespace(game_id="fixture-game", app_key="fixture-app")
        rec.logger, rec.observe_sdkuid = Mock(), Mock()
        self.assertTrue(rec.validate_token_online())
        self.assertIsNone(data["expires"])

    def test_4399_issues_new_pair_and_preserves_refresh_token(self):
        cls, _ = adapter("m4399ChannelHandler.py", "m4399Channel",
                         ("_recover_existing_credential", "_accept_silent_login_resp"))
        rec = cls()
        rec.refreshToken = "fixture-refresh"
        rec._get_login_data = lambda: {"uid": "fixture-id", "state": "fixture-old"}
        response = {"result": {"uid": "fixture-id", "state": "fixture-new", "access_token": "fixture-new-at"}}
        rec.m4399Login = types.SimpleNamespace(refresh_by_token=Mock(return_value=(response, "0")),
                                               check_state=Mock(), refresh_by_state=Mock())
        rec.observe_sdkuid, rec._update_name, rec._persist_recovery_state = Mock(), Mock(), Mock()
        rec.is_token_valid = lambda: True
        rec._warn_recovery_timeline = Mock()
        self.assertTrue(rec._recover_existing_credential())
        rec.m4399Login.check_state.assert_not_called()
        self.assertEqual(rec.loginResp, response)
        self.assertEqual(rec.refreshToken, "fixture-refresh")


if __name__ == "__main__":
    unittest.main()
