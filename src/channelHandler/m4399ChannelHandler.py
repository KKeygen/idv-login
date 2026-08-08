# coding=UTF-8
"""4399（游戏盒）渠道处理器。

凭证恢复顺序：
1. oauth-check 验证当前业务 state；
2. 无效/无法验证时，用 callback URL 持久化的 refresh_token 调
   oauth-getinfobyrefresh.html；
3. 仍失败时，用当前业务 state 调 oauth.html + source=4399 + refresh=1；
4. 仍失败时重新打开 WebView 让用户登录。

refresh_token 只从成功 callback URL 获取；两个无交互刷新接口成功响应都不含
refresh_token，所以刷新 loginResp 时必须保留已有 refreshToken。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, List, Optional

import app_state
import channelmgr
from cloudRes import CloudRes
from envmgr import genv
from logutil import setup_logger
from channelHandler.channelUtils import buildSAUTH, getShortGameId, postSignedData
from channelHandler.m4399Login.m4399Channel import M4399Login


DEFAULT_GAME_KEY = "*****"
DEFAULT_SDK_VER = "3.16.0"
DEFAULT_BID = "com.netease.dwrg.m4399"
DEFAULT_CANAL = "4399com"


class m4399Channel(channelmgr.channel):
    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        game_id: str = "",
        loginResp: Optional[Dict[str, Any]] = None,
        uuid: str = "",
        cookies: Optional[Dict[str, str]] = None,
        cookieRecords: Optional[List[Dict[str, Any]]] = None,
        refreshToken: str = "",
        oauthDevice: Optional[Dict[str, Any]] = None,
        callbackExpiredAt: Optional[int] = None,
        callbackExpiresIn: Optional[int] = None,
        lastWebLoginTime: int = 0,
        lastRefreshTokenRotateTime: int = 0,
        lastStateRefreshTime: int = 0,
    ) -> None:
        super().__init__(
            login_info,
            user_info,
            ext_info,
            device_info,
            create_time,
            last_login_time,
            name,
            uuid=uuid,
        )
        self.logger = setup_logger()
        self.crossGames = False
        self.game_id = game_id
        self.loginResp: Optional[Dict[str, Any]] = loginResp

        # WebView 观测数据。cookie 值持久化，但日志永不打印值。
        self.cookies: Dict[str, str] = dict(cookies or {})
        self.cookieRecords: List[Dict[str, Any]] = [
            dict(item) for item in (cookieRecords or []) if isinstance(item, dict)
        ]

        # refreshToken 是 callback URL query 独有字段，不能从 refresh response 覆盖。
        self.refreshToken = str(refreshToken or "")
        self.oauthDevice: Dict[str, Any] = dict(oauthDevice or {})
        self.callbackExpiredAt = self._int_or_none(callbackExpiredAt)
        self.callbackExpiresIn = self._int_or_none(callbackExpiresIn)

        # 三条恢复路径的成功时间，用真实用户反馈反推长期寿命。
        self.lastWebLoginTime = int(lastWebLoginTime or 0)
        self.lastRefreshTokenRotateTime = int(lastRefreshTokenRotateTime or 0)
        self.lastStateRefreshTime = int(lastStateRefreshTime or 0)

        real_game_id = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res is None:
            self.logger.warning(
                f"cloudRes 中未找到 4399com 配置 (game_id={real_game_id})，使用默认参数"
            )
            self.channelConfig = {}
        else:
            self.channelConfig = res.get(self.channel_name, {})

        self.game_key = self.channelConfig.get("app_key") or DEFAULT_GAME_KEY
        self.sdk_version = self.channelConfig.get("sdk_ver") or DEFAULT_SDK_VER
        self.bid = (res or {}).get("package_name") or DEFAULT_BID
        self.realGameId = real_game_id

        self.m4399Login = M4399Login(
            game_key=self.game_key,
            bid=self.bid,
            canal=DEFAULT_CANAL,
            sdk_version=self.sdk_version,
            oauth_device=self.oauthDevice,
        )
        # M4399Login 会补齐缺失的稳定 UDID；立即同步，以便首次保存。
        self.oauthDevice = self.m4399Login.export_oauth_device()

    # ── Serialization ─────────────────────────────────────────

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            login_info=data.get("login_info", {}),
            user_info=data.get("user_info", {}),
            ext_info=data.get("ext_info", {}),
            device_info=data.get("device_info", {}),
            create_time=data.get("create_time", int(time.time())),
            last_login_time=data.get("last_login_time", 0),
            name=data.get("name", ""),
            game_id=data.get("game_id", ""),
            loginResp=data.get("loginResp"),
            uuid=data.get("uuid", ""),
            cookies=data.get("cookies", {}),
            cookieRecords=data.get("cookieRecords", []),
            refreshToken=data.get("refreshToken", ""),
            oauthDevice=data.get("oauthDevice", {}),
            callbackExpiredAt=data.get("callbackExpiredAt"),
            callbackExpiresIn=data.get("callbackExpiresIn"),
            lastWebLoginTime=data.get("lastWebLoginTime", 0),
            lastRefreshTokenRotateTime=data.get("lastRefreshTokenRotateTime", 0),
            lastStateRefreshTime=data.get("lastStateRefreshTime", 0),
        )

    def before_save(self):
        if self.loginResp is not None:
            json.dumps(self.loginResp)
        json.dumps(self.cookies)
        json.dumps(self.cookieRecords)
        json.dumps(self.oauthDevice)
        # refreshToken/string + timestamps 本身均可 JSON 序列化。

    # ── Local credential / diagnostics ────────────────────────

    def _get_login_data(self) -> Optional[Dict[str, Any]]:
        if not isinstance(self.loginResp, dict):
            return None
        result = self.loginResp.get("result")
        if isinstance(result, dict) and result.get("uid"):
            return result
        return None

    def is_token_valid(self) -> bool:
        """本地完整性检查；在线真值由 get_uniSdk_data 的 oauth-check 决定。"""
        data = self._get_login_data()
        if not data:
            return False
        return (
            bool(data.get("uid"))
            and bool(data.get("state"))
            and bool(data.get("access_token"))
        )

    def _update_name(self):
        data = self._get_login_data()
        if data:
            nick = data.get("nick") or data.get("username") or ""
            if nick:
                self.name = str(nick)

    @staticmethod
    def _elapsed_text(timestamp: int) -> str:
        if not timestamp:
            return "从未"
        seconds = max(0, int(time.time()) - int(timestamp))
        if seconds < 3600:
            return f"{seconds / 60:.1f}分钟"
        if seconds < 86400:
            return f"{seconds / 3600:.2f}小时"
        return f"{seconds / 86400:.2f}天"

    def _warn_recovery_timeline(self, message: str) -> None:
        self.logger.warning(
            "[4399-recovery] "
            f"{message}; "
            f"距上次Web登录={self._elapsed_text(self.lastWebLoginTime)}, "
            f"距上次RT轮换={self._elapsed_text(self.lastRefreshTokenRotateTime)}, "
            f"距上次refresh=1={self._elapsed_text(self.lastStateRefreshTime)}"
        )

    def _persist_recovery_state(self) -> None:
        """刷新值可能立即轮转；若当前账号已受 ChannelManager 管理则立即落盘。"""
        try:
            helper = getattr(app_state, "channels_helper", None)
            channels = getattr(helper, "channels", None)
            if helper is not None and isinstance(channels, list) and self in channels:
                helper.save_records()
        except Exception:
            self.logger.exception("4399 恢复凭证落盘失败")

    def _accept_silent_login_resp(self, resp: Dict[str, Any]) -> bool:
        """用刷新响应整包替换 loginResp，但绝不动 refreshToken。"""
        if not isinstance(resp, dict) or not isinstance(resp.get("result"), dict):
            return False
        result = resp["result"]
        if not result.get("uid") or not result.get("state"):
            return False
        self.loginResp = resp
        self._update_name()
        return self.is_token_valid()

    def _sync_web_snapshot(self):
        self.cookies = dict(self.m4399Login.cookies or {})
        self.cookieRecords = [
            dict(item)
            for item in (self.m4399Login.cookie_records or [])
            if isinstance(item, dict)
        ]
        self.oauthDevice = self.m4399Login.export_oauth_device()

        # 新 Web callback 若成功捕获 RT，则替换旧 RT；若意外缺失则保留旧 RT。
        if self.m4399Login.refresh_token:
            self.refreshToken = self.m4399Login.refresh_token
        self.callbackExpiredAt = self.m4399Login.callback_expired_at
        self.callbackExpiresIn = self.m4399Login.callback_expires_in

        persistent_count = sum(
            1 for item in self.cookieRecords if not item.get("session", True)
        )
        self.logger.debug(
            "[4399-cookie] channel snapshot updated: "
            f"cookies={len(self.cookies)} records={len(self.cookieRecords)} "
            f"persistent={persistent_count} "
            f"session={len(self.cookieRecords) - persistent_count} "
            f"refresh_token_present={bool(self.refreshToken)}"
        )

    # ── Recovery chain ────────────────────────────────────────

    def _recover_existing_credential(self) -> bool:
        """check -> refresh_token -> state refresh=1。失败后由上层进入 WebView。"""
        data = self._get_login_data() or {}
        uid = str(data.get("uid") or "")
        state = str(data.get("state") or "")

        # 1) Official state check.
        if uid and state:
            valid, code = self.m4399Login.check_state(uid, state)
            if valid is True and self.is_token_valid():
                self._warn_recovery_timeline(
                    "oauth-check 有效，继续使用现有 state"
                )
                return True
            if valid is True:
                # state 有效但本地缺 access_token，仍需恢复完整登录材料。
                self._warn_recovery_timeline(
                    "oauth-check 有效但本地登录材料不完整，继续尝试恢复"
                )
            elif valid is False:
                self._warn_recovery_timeline(
                    f"oauth-check 未通过(code={code or '?'})，尝试 RT 轮换"
                )
            else:
                self._warn_recovery_timeline(
                    "oauth-check 无法判定(network_error)，尝试 RT 轮换"
                )
        else:
            self._warn_recovery_timeline(
                "本地 uid/state 不完整，跳过 oauth-check，尝试 RT 轮换"
            )

        # 2) refresh_token recovery. The response has NO refresh_token.
        if uid and self.refreshToken:
            refreshed, code = self.m4399Login.refresh_by_token(
                uid,
                self.refreshToken,
            )
            if refreshed and self._accept_silent_login_resp(refreshed):
                self.lastRefreshTokenRotateTime = int(time.time())
                self._persist_recovery_state()
                self._warn_recovery_timeline(
                    f"RT 轮换成功(code={code or '?'})，已更新 state/access_token"
                )
                return True
            self._warn_recovery_timeline(
                f"RT 轮换失败(code={code or '?'})，尝试 refresh=1"
            )
        else:
            self._warn_recovery_timeline(
                "无可用 refresh_token/uid，跳过 RT 轮换，尝试 refresh=1"
            )

        # 3) oauth.html state=<business state> + refresh=1.
        # Use the original current state even if RT refresh failed; RT failure response
        # is never accepted into loginResp.
        if state:
            refreshed, code = self.m4399Login.refresh_by_state(state)
            if refreshed and self._accept_silent_login_resp(refreshed):
                self.lastStateRefreshTime = int(time.time())
                self._persist_recovery_state()
                self._warn_recovery_timeline(
                    f"refresh=1 成功(code={code or '?'})，已更新 state/access_token"
                )
                return True
            self._warn_recovery_timeline(
                f"refresh=1 失败(code={code or '?'})，需要重新 Web 登录"
            )
        else:
            self._warn_recovery_timeline(
                "无可用业务 state，跳过 refresh=1，需要重新 Web 登录"
            )

        return False

    # ── Interactive login ─────────────────────────────────────

    def request_user_login(self, on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        if on_complete is not None:

            def _on_done(resp):
                self._sync_web_snapshot()
                if resp and isinstance(resp.get("result"), dict):
                    self.loginResp = resp
                    self._update_name()
                    self.lastWebLoginTime = int(time.time())
                    self._persist_recovery_state()
                    self._warn_recovery_timeline(
                        "Web 登录成功，已保存 callback JSON/cookies/refresh_token"
                    )
                    on_complete(True)
                else:
                    self._warn_recovery_timeline("Web 登录未完成或失败")
                    on_complete(False)

            self.m4399Login.web_login(on_complete=_on_done)
            return

        resp = self.m4399Login.web_login()
        self._sync_web_snapshot()
        if resp and isinstance(resp.get("result"), dict):
            self.loginResp = resp
            self._update_name()
            self.lastWebLoginTime = int(time.time())
            self._persist_recovery_state()
            self._warn_recovery_timeline(
                "Web 登录成功，已保存 callback JSON/cookies/refresh_token"
            )
            return True

        self._warn_recovery_timeline("Web 登录未完成或失败")
        return False

    # ── UniSDK ────────────────────────────────────────────────

    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        short_game_id = getShortGameId(game_id)

        def _build_result():
            data = self._get_login_data()
            if not data:
                raise RuntimeError("4399 登录数据缺失")

            uid = str(data.get("uid") or "")
            state = str(data.get("state") or "")
            access_token = str(data.get("access_token") or "")
            if not uid or not state or not access_token:
                raise RuntimeError(
                    "4399 登录数据不完整: "
                    f"uid_present={bool(uid)}, state_present={bool(state)}, "
                    f"access_token_present={bool(access_token)}"
                )

            sdk_version = self.sdk_version
            uniBody = buildSAUTH(
                login_channel=self.channel_name,
                app_channel=self.channel_name,
                uid=uid,
                session=state,
                game_id=short_game_id,
                sdk_version=sdk_version,
            )
            uniData = postSignedData(uniBody, short_game_id, True)
            uniSDKJSON = json.loads(
                base64.b64decode(uniData["unisdk_login_json"]).decode()
            )

            fd = app_state.fake_device
            extra_data = {
                "realname": json.dumps({"realname_type": 0, "age": 22}),
            }
            json_data = {
                "extra_data": extra_data.get("extra_data"),
                "get_access_token": "1",
                "sdk_udid": fd["udid"],
                "realname": extra_data.get("realname"),
            }
            json_data.update(uniBody)

            str_data = json_data.copy()
            str_data.update({"username": uniSDKJSON["username"]})
            str_data = "&".join([f"{k}={v}" for k, v in str_data.items()])

            extra_unisdk = json.dumps(
                {
                    "SAUTH_STR": base64.b64encode(str_data.encode()).decode(),
                    "SAUTH_JSON": base64.b64encode(
                        json.dumps(json_data).encode()
                    ).decode(),
                    **extra_data,
                }
            )

            return {
                "user_id": uid,
                "token": base64.b64encode(access_token.encode()).decode(),
                "login_channel": self.channel_name,
                "udid": fd["udid"],
                "app_channel": self.channel_name,
                "sdk_version": sdk_version,
                "jf_game_id": short_game_id,
                "pay_channel": self.channel_name,
                "extra_data": "",
                "extra_unisdk_data": extra_unisdk,
                "gv": "157",
                "gvn": "1.5.80",
                "cv": "a1.5.0",
            }

        if self._recover_existing_credential():
            try:
                result = _build_result()
            except Exception as exc:
                self.logger.error(f"4399 UniSDK error: {exc}")
                result = None
            if on_complete is not None:
                on_complete(result)
                return None
            return result

        # All non-interactive recovery paths failed: interactive Web fallback.
        if on_complete is not None:

            def _on_login_done(success):
                if success and self.is_token_valid():
                    try:
                        on_complete(_build_result())
                    except Exception as exc:
                        self.logger.error(f"4399 UniSDK error: {exc}")
                        on_complete(None)
                else:
                    on_complete(None)

            self.request_user_login(on_complete=_on_login_done)
            return None

        self.request_user_login()
        if not self.is_token_valid():
            return None
        try:
            return _build_result()
        except Exception as exc:
            self.logger.error(f"4399 UniSDK error: {exc}")
            return None
