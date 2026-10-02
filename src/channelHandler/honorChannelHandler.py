# coding=UTF-8
"""荣耀 (Honor) 游戏中心渠道处理器。

登录流程：OAuth 浏览器授权 → Game Center aggregate/login → unionToken → SAUTH。
支持 configLogin 刷新 session（无需重新 OAuth）。
"""
import json
import time
import base64

import channelmgr
from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger
from channelHandler.channelUtils import buildSAUTH, postSignedData, getShortGameId
from channelHandler.honorLogin.consts import HONOR_APK_VER_NAME
from channelHandler.honorLogin.honorChannel import HonorLogin


class honorChannel(channelmgr.channel):

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
        unionToken: dict = None,
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
        self.unionToken = unionToken

        real_game_id = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res is None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            raise Exception(
                f"游戏{real_game_id}-渠道{self.channel_name}暂不支持，请参照教程联系开发者发起添加请求。"
            )
        self.channelConfig = res.get(self.channel_name, {})
        self.sdk_version = self.channelConfig.get("sdk_ver", HONOR_APK_VER_NAME)
        self.honorLogin = HonorLogin(self.channelConfig, self.unionToken)
        self.realGameId = real_game_id

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
            unionToken=data.get("unionToken"),
            uuid=data.get("uuid", ""),
        )

    # ── 登录 ──────────────────────────────────────────────────

    def request_user_login(self, on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        def _apply(success):
            if success is not True:
                return success
            token = self.honorLogin.unionToken
            if not isinstance(token, dict) or not token.get("openId") or not token.get("token"):
                return False
            self.unionToken = token
            self._update_name()
            return True
        try:
            if on_complete is not None:
                self.honorLogin.newOAuthLogin(on_complete=lambda success: on_complete(_apply(success)))
                return None
            return _apply(self.honorLogin.newOAuthLogin())
        except Exception:
            self.logger.error("渠道登录未完成")
            if on_complete is not None:
                on_complete(False)
            return False

    def is_token_valid(self) -> bool:
        if self.unionToken is None:
            return False
        open_id = self.unionToken.get("openId", "")
        token = self.unionToken.get("token", "")
        return bool(open_id and token)

    def _refresh_session(self) -> bool:
        """尝试用 configLogin 刷新 session。"""
        self.honorLogin.unionToken = self.unionToken
        success = self.honorLogin.configLogin()
        if success:
            self.unionToken = self.honorLogin.unionToken
            self._update_name()
        return success

    def _update_name(self):
        """从 honorLogin.displayName 更新渠道记录名称。"""
        dn = getattr(self.honorLogin, "displayName", "")
        if dn:
            self.name = dn

    # ── UniSDK 数据 ──────────────────────────────────────────

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        short_game_id = getShortGameId(game_id)

        def _build_result():
            ut = self.unionToken
            open_id = ut.get("openId", "")
            token = ut.get("token", "")

            sdk_version = self.sdk_version
            uniBody = buildSAUTH(
                login_channel=self.channel_name,
                app_channel=self.channel_name,
                uid=open_id,
                session=token,
                game_id=short_game_id,
                sdk_version=sdk_version,
                custom_data={
                    "realname": json.dumps({"realname_type": 0, "duration": 0}),
                },
            )
            uniData = postSignedData(uniBody, short_game_id, False)
            uniSDKJSON = json.loads(
                base64.b64decode(uniData["unisdk_login_json"]).decode()
            )

            fd = app_state.fake_device

            extra_data = {
                "realname": json.dumps({"realname_type": 0, "duration": 0}),
            }
            json_data = {
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
                "user_id": open_id,
                "token": base64.b64encode(token.encode()).decode(),
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

        # 检查 token 是否有效
        if not self.is_token_valid():
            if not interactive:
                if on_complete is not None:
                    on_complete(False)
                return False
            if on_complete is not None:
                def _on_login_done(success):
                    if success is True and self.is_token_valid():
                        try:
                            result = _build_result()
                            on_complete(result)
                        except Exception as e:
                            self.logger.error(f"Honor UniSDK error: {e}")
                            on_complete(False)
                    else:
                        on_complete(success)
                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                success = self.request_user_login()
                if success is not True:
                    return success

        # 尝试 configLogin 刷新
        if self.honorLogin.is_token_expired():
            if not self._refresh_session():
                if not interactive:
                    if on_complete is not None:
                        on_complete(False)
                    return False
                # 刷新失败，需要重新 OAuth
                if on_complete is not None:
                    def _on_relogin_done(success):
                        if success is True and self.is_token_valid():
                            try:
                                result = _build_result()
                                on_complete(result)
                            except Exception as e:
                                self.logger.error(f"Honor UniSDK error after re-login: {e}")
                                on_complete(False)
                        else:
                            on_complete(success)
                    self.request_user_login(on_complete=_on_relogin_done)
                    return None
                else:
                    success = self.request_user_login()
                    if success is not True:
                        return success

        try:
            result = _build_result()
        except Exception:
            self.logger.error("构建渠道登录数据失败")
            if on_complete is not None:
                on_complete(False)
            return False
        if on_complete is not None:
            on_complete(result)
            return None
        return result


