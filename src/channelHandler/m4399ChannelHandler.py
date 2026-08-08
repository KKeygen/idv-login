# coding=UTF-8
"""4399 (游戏盒) 渠道处理器。

登录流程：H5 OAuth（无盒子路径）→ oauth-callback 拿 state → SAUTH 换网易登录态。
4399 无 token 刷新接口，state 过期后需重新 OAuth。
"""
import json
import time
import base64
from typing import Any, Dict, Optional

import channelmgr
from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger
from channelHandler.channelUtils import buildSAUTH, postSignedData, getShortGameId
from channelHandler.m4399Login.m4399Channel import M4399Login

# 4399 渠道默认参数（cloudRes 缺失时兜底）
DEFAULT_GAME_KEY = "114816"
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
        )

    # ── 序列化 / 反序列化 ────────────────────────────────────

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
        )

    def before_save(self):
        if self.loginResp is not None:
            json.dumps(self.loginResp)

    # ── 登录状态 ──────────────────────────────────────────────

    def _get_login_data(self) -> Optional[Dict[str, Any]]:
        """从 loginResp 中提取 result 字段（code=100 的 oauth 回调）。"""
        if not isinstance(self.loginResp, dict):
            return None
        result = self.loginResp.get("result")
        if isinstance(result, dict) and result.get("uid"):
            return result
        return None

    def is_token_valid(self) -> bool:
        data = self._get_login_data()
        if not data:
            return False
        return bool(data.get("state")) and bool(data.get("access_token"))

    def _update_name(self):
        data = self._get_login_data()
        if data:
            nick = data.get("nick") or data.get("username") or ""
            if nick:
                self.name = nick

    # ── 登录 ──────────────────────────────────────────────────

    def request_user_login(self, on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        if on_complete is not None:
            def _on_done(resp):
                if resp and resp.get("result"):
                    self.loginResp = resp
                    self._update_name()
                    on_complete(True)
                else:
                    on_complete(False)
            self.m4399Login.web_login(on_complete=_on_done)
            return

        resp = self.m4399Login.web_login()
        if resp and resp.get("result"):
            self.loginResp = resp
            self._update_name()
            return True
        return False

    # ── UniSDK 数据 ──────────────────────────────────────────

    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        short_game_id = getShortGameId(game_id)

        def _build_result():
            data = self._get_login_data()
            uid = str(data.get("uid") or "")
            state = str(data.get("state") or "")
            access_token = str(data.get("access_token") or "")
            if not uid or not state:
                raise RuntimeError(f"4399 登录数据缺失: uid={uid}")

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

            extra_unisdk = json.dumps({
                "SAUTH_STR": base64.b64encode(str_data.encode()).decode(),
                "SAUTH_JSON": base64.b64encode(json.dumps(json_data).encode()).decode(),
                **extra_data,
            })

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

        # token 无效：重新 OAuth
        if not self.is_token_valid():
            if on_complete is not None:
                def _on_login_done(success):
                    if success and self.is_token_valid():
                        try:
                            result = _build_result()
                            on_complete(result)
                        except Exception as e:
                            self.logger.error(f"4399 UniSDK error: {e}")
                            on_complete(None)
                    else:
                        on_complete(None)
                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                self.request_user_login()
                if not self.is_token_valid():
                    return None

        result = _build_result()
        if on_complete is not None:
            on_complete(result)
            return None
        return result
