# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import json
import time
import base64
import channelmgr

from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger
from channelHandler.miLogin.miChannel import MiLogin
from channelHandler.channelUtils import getShortGameId


class miChannel(channelmgr.channel):
    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        oAuthData: dict = {},
        game_id: str = "",
        account_type: int = 4,
    ) -> None:
        super().__init__(
            login_info,
            user_info,
            ext_info,
            device_info,
            create_time,
            last_login_time,
            name,
        )
        self.oAuthData = oAuthData
        self._update_oauth_name()
        self.logger = setup_logger()
        self.crossGames = False
        # Done: Use Actions to auto update game_id-app_id mapping by uploading an APK.
        self.game_id = game_id
        real_game_id = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res == None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            raise Exception(
                f"游戏{real_game_id}-渠道{self.channel_name}暂不支持，请参照教程联系开发者发起添加请求。"
            )
        self.miLogin = MiLogin(
            res.get(self.channel_name).replace("mi_", ""), self.oAuthData, account_type
        )
        self.realGameId = real_game_id
        self.uniBody = None
        self.uniData = None
        self.account_type = account_type

    def _update_oauth_name(self, previous_oauth=None):
        """使用小米返回的昵称，保留用户手动设置的账号名称。"""
        data = self.oAuthData if isinstance(self.oAuthData, dict) else {}
        nickname = data.get("nickname")
        previous_name = (
            previous_oauth.get("nickname")
            if isinstance(previous_oauth, dict) else None
        )
        if isinstance(nickname, str) and nickname.strip():
            if self.name == self.uuid or (previous_name and self.name == previous_name):
                self.name = nickname

    def request_user_login(self, on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        def _apply(data):
            if isinstance(data, Exception):
                raise data
            if data is None or data is False:
                return data
            if not isinstance(data, dict) or not data.get("uuid") or not data.get("st"):
                return False
            previous_oauth = self.oAuthData
            self.oAuthData = data
            if not isinstance(previous_oauth, dict) or previous_oauth.get("uuid") != data["uuid"]:
                # OAuth UUID is not the game's appAccountId; refetch that identity.
                self.active_sdkuid = ""
                self.unisdk_cache.clear()
                self.expires_at = None
            self._update_oauth_name(previous_oauth)
            self.account_type = self.miLogin.account_type
            self.mark_manual_login_success()
            return True
        def _on_browser_done(data):
            try:
                result = _apply(data)
            except Exception as error:
                result = error
            on_complete(result)
        try:
            if on_complete is not None:
                self.miLogin.webLogin(on_complete=_on_browser_done)
                return None
            return _apply(self.miLogin.webLogin())
        except Exception as error:
            if on_complete is not None:
                on_complete(error)
                return None
            raise

    def _get_session(self, on_complete=None, *, interactive=True):
        def _read():
            data = self.miLogin.initAccountData()
            if not isinstance(data, dict) or not data.get("appAccountId") or not data.get("session"):
                raise ValueError("小米未返回有效 session")
            self.observe_sdkuid(data["appAccountId"])
            return data["appAccountId"], data["session"]
        try:
            result = _read()
        except Exception as error:
            if not interactive:
                # A failed API request is not evidence that a browser is required.
                if on_complete is not None:
                    on_complete(error)
                    return None
                raise
            elif on_complete is not None:
                def _on_relogin_done(success):
                    if success is not True:
                        on_complete(success)
                        return
                    try:
                        result = _read()
                    except Exception as error:
                        result = error
                    on_complete(result)
                self.request_user_login(on_complete=_on_relogin_done)
                return None
            else:
                success = self.request_user_login()
                if success is not True:
                    return success
                result = _read()
        if on_complete is not None:
            on_complete(result)
            return None
        return result

    def is_token_valid(self):
        if not isinstance(self.oAuthData, dict) or not self.oAuthData.get("uuid") or not self.oAuthData.get("st"):
            self.logger.info(f"Token is invalid for {self.name}")
            return False
        return True

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
            oAuthData=data.get("oAuthData", None),
            game_id=data.get("game_id", ""),
            account_type=data.get("account_type", 4),
        )

    def get_sdk_udid(self):
        return self.oAuthData["uuid"]

    def _build_extra_unisdk_data(self) -> str:
        res = {
            "SAUTH_STR": "",
            "SAUTH_JSON": "",
            "extra_data": "",
            "realname": "",
            "get_access_token": "1",
        }
        extra = json.dumps({"adv_channel": "0", "adid": "0"})
        realname = json.dumps({"realname_type": 0, "age": 18})
        json_data = {
            "extra_data": extra,
            "get_access_token": "1",
            "sdk_udid": self.get_sdk_udid(),
            "realname": realname,
        }
        json_data.update(self.uniBody)

        str_data = json_data.copy()
        str_data.update({"username": self.uniSDKJSON["username"]})
        str_data = "&".join([f"{k}={v}" for k, v in str_data.items()])

        res["SAUTH_STR"] = base64.b64encode(str_data.encode()).decode()
        res["SAUTH_JSON"] = base64.b64encode(json.dumps(json_data).encode()).decode()
        res["extra_data"] = extra
        res["realname"] = realname
        return json.dumps(res)

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        """获取 UniSDK 登录数据，支持异步模式。
        
        当 token 过期需要重新登录时，会异步弹出浏览器窗口。
        
        Args:
            game_id: 游戏ID
            on_complete: 异步回调函数，接收登录数据或 None
        """
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if game_id == "":
            game_id = self.game_id
        self.logger.info(f"Get unisdk data for {self.name}")
        import channelHandler.channelUtils as channelUtils

        def _build_unisdk_data(appAccountId, session):
            """构建 UniSDK 数据的实际逻辑"""
            self.uniBody = channelUtils.buildSAUTH(
                self.channel_name,
                self.channel_name,
                str(appAccountId),
                session,
                getShortGameId(game_id),
                "3.3.0.7",
            )
            fd = app_state.fake_device
            self.uniData = channelUtils.postSignedData(
                self.uniBody, getShortGameId(game_id)
            )
            self.uniSDKJSON = json.loads(
                base64.b64decode(self.uniData["unisdk_login_json"]).decode()
            )
            res = {
                "user_id": self.get_sdk_udid(),
                "token": base64.b64encode(session.encode()).decode(),
                "login_channel": self.channel_name,
                "udid": fd["udid"],
                "app_channel": self.channel_name,
                "sdk_version": "3.0.5.002",
                "jf_game_id": getShortGameId(game_id),
                "pay_channel": self.channel_name,
                "extra_data": "",
                "extra_unisdk_data": self._build_extra_unisdk_data(),
                "gv": "157",
                "gvn": "1.5.80",
                "cv": "a1.5.0",
            }
            return res

        def _on_session_ready(session_data):
            """session 准备好后的回调"""
            if isinstance(session_data, Exception):
                if on_complete is not None:
                    on_complete(session_data)
                    return None
                raise session_data
            if session_data is None or session_data is False:
                if on_complete:
                    on_complete(session_data)
                return session_data
            try:
                appAccountId, session = session_data
                result = _build_unisdk_data(appAccountId, session)
            except Exception as error:
                if on_complete is None:
                    raise
                result = error
            if on_complete is not None:
                on_complete(result)
                return None
            return result

        # 先检查 token
        if not self.is_token_valid():
            if not interactive:
                if on_complete is not None:
                    on_complete(False)
                    return None
                return False
            if on_complete is not None:
                # 异步模式：先完成登录
                def _on_login_done(success):
                    if success is True:
                        self._get_session(on_complete=_on_session_ready, interactive=interactive)
                    else:
                        on_complete(success)
                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                # 同步模式
                success = self.request_user_login()
                if success is not True:
                    return success

        # 获取 session（可能需要异步重新登录）
        if on_complete is not None:
            self._get_session(on_complete=_on_session_ready, interactive=interactive)
            return None
        
        # 同步模式
        session_data = self._get_session(interactive=interactive)
        if session_data is None or session_data is False:
            return session_data
        appAccountId, session = session_data
        return _build_unisdk_data(appAccountId, session)
