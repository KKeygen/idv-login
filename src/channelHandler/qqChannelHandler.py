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

import requests
import channelmgr

from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.channelUtils import getShortGameId
from channelHandler.qqLogin.qqChannel import QQLogin
from channelHandler.wechatChannelHandler import myappVeriftResp


class qqChannel(channelmgr.channel):

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
        session: myappVeriftResp = None,
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
            uuid,
        )
        self.logger = setup_logger()
        self.crossGames = False
        cloudRes = CloudRes()

        self.game_id = game_id
        real_game_id = getShortGameId(game_id)
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res is None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            raise Exception(f"游戏{real_game_id}-渠道myapp暂不支持，请参照教程联系开发者发起添加请求。")

        self.qq_appid = res.get(self.channel_name).get("channel")
        self.wx_appid = res.get(self.channel_name).get("wx_appid")
        self.qqLogin = QQLogin(self.qq_appid, game_id=game_id)
        self.realGameId = real_game_id
        self.uniBody = None
        self.uniData = None
        self.session: myappVeriftResp = myappVeriftResp(session) if session is not None else None

    def request_user_login(self, on_complete=None):
        """本次浏览器登录：True 成功，False 失败，None 用户关闭。"""
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        def _accept(resp):
            if resp is None or resp is False:
                return resp
            if not isinstance(resp, dict) or not all(
                resp.get(key) for key in ("atk", "openid", "atk_expire")
            ):
                return False
            self.session = myappVeriftResp(resp)
            self._fetch_nickname()
            self.last_login_time = int(time.time())
            return True

        if on_complete is not None:
            def _on_browser_done(resp):
                try:
                    result = _accept(resp)
                except Exception:
                    self.logger.error("QQ登录结果处理失败")
                    result = False
                on_complete(result)
            self.qqLogin.webLogin(on_complete=_on_browser_done)
            return None
        try:
            return _accept(self.qqLogin.webLogin())
        except Exception:
            self.logger.error("QQ登录失败")
            return False

    def _fetch_nickname(self):
        """尝试获取QQ昵称"""
        try:
            r = requests.get(
                f"https://graph.qq.com/user/get_user_info"
                f"?access_token={self.session.atk}"
                f"&oauth_consumer_key={self.qq_appid}"
                f"&openid={self.session.openid}",
                verify=should_verify_ssl()
            )
            r.encoding = "utf-8"
            nickname = r.json().get("nickname")
            if nickname:
                self.name = nickname
        except:
            pass

    def is_token_valid(self):
        if self.session is not None and self.session.atk_expire:
            return self.last_login_time + self.session.atk_expire > int(time.time())
        return False

    def before_save(self):
        self.session_json = self.session.__json__()
        return super().before_save()

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
            session=data.get("session_json", None),
            uuid=data.get("uuid", ""),
        )

    def _get_extra_data(self):
        return json.dumps(
                {
                    "login_type": 2,
                    "session_id": "openid",
                    "session_type": "kp_actoken",
                    "openid": self.session.openid,
                    "openkey": self.session.atk,
                    "pf": self.session.pf,
                    "pfkey": self.session.pfKey,
                    "zoneid": "1",
                })

    def _build_extra_unisdk_data(self) -> str:
        fd = app_state.fake_device
        res = {
            "SAUTH_STR": "",
            "SAUTH_JSON": "",
        }

        extra_data = {
            "extra_data": self._get_extra_data(),
            "realname": json.dumps({"realname_type": 0, "age": 22}),
        }

        res.update(extra_data)
        json_data = {
            "extra_data": extra_data.get("extra_data"),
            "get_access_token": "1",
            "sdk_udid": fd["udid"],
            "realname": extra_data.get("realname"),
        }
        json_data.update(self.uniBody)

        str_data = json_data.copy()
        str_data.update({"username": self.uniSDKJSON["username"]})
        str_data = "&".join([f"{k}={v}" for k, v in str_data.items()])

        res["SAUTH_STR"] = base64.b64encode(str_data.encode()).decode()
        res["SAUTH_JSON"] = base64.b64encode(json.dumps(json_data).encode()).decode()

        return json.dumps(res)

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        """获取 UniSDK 登录数据，支持异步模式。"""
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if game_id == "":
            game_id = self.game_id
        self.logger.info(f"Get unisdk data for {self.name}")
        import channelHandler.channelUtils as channelUtils

        def _build_unisdk_data():
            """构建 UniSDK 数据的实际逻辑"""
            self.uniBody = channelUtils.buildSAUTH(
                self.channel_name,
                self.channel_name,
                self.session.openid,
                self.session.atk,
                getShortGameId(game_id),
                "2.2.2",
                {
                    "get_access_token": "1",
                    "extra_data": self._get_extra_data(),
                },
            )
            fd = app_state.fake_device
            self.uniData = channelUtils.postSignedData(
                self.uniBody, getShortGameId(game_id), True
            )

            if "unisdk_login_json" not in self.uniData:
                self.logger.error(f"SAUTH 失败: {self.uniData}")
                raise Exception(f"SAUTH 返回错误: {self.uniData.get('error', self.uniData)}")

            self.uniSDKJSON = json.loads(
                base64.b64decode(self.uniData["unisdk_login_json"]).decode()
            )
            res = {
                "user_id": self.session.openid,
                "token": self.session.atk,
                "login_channel": self.channel_name,
                "udid": fd["udid"],
                "app_channel": self.channel_name,
                "sdk_version": "2.2.2",
                "jf_game_id": getShortGameId(game_id),
                "pay_channel": self.channel_name,
                "extra_data": "",
                "extra_unisdk_data": self._build_extra_unisdk_data(),
                "gv": "157",
                "gvn": "1.5.80",
                "cv": "a1.5.0",
            }
            return res

        def _on_login_ready():
            """登录完成后构建数据"""
            try:
                result = _build_unisdk_data()
                if on_complete:
                    on_complete(result)
                return result
            except Exception as e:
                self.logger.error("构建 UniSDK 数据失败")
                if on_complete:
                    on_complete(False)
                return False

        try:
            valid = self.is_token_valid()
        except Exception:
            valid = False
        if not valid:
            if not interactive:
                if on_complete is not None:
                    on_complete(False)
                return False
            if on_complete is not None:
                def _on_login_done(success):
                    if success is True:
                        _on_login_ready()
                    else:
                        on_complete(success)
                self.request_user_login(on_complete=_on_login_done)
                return None
            result = self.request_user_login()
            if result is not True:
                return result
        return _on_login_ready()
