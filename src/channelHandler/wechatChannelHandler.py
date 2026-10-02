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
from channelHandler.wechatLogin.wechatChannel import WechatLogin


class myappVeriftResp:
    def __init__(self, rawJson: dict) -> None:
        try:
            self.atk = rawJson.get("atk")
            self.atk_expire = rawJson.get("atk_expire")
            self.first = rawJson.get("first")
            self.judgeLoginData = rawJson.get("judgeLoginData")
            self.msg = rawJson.get("msg")
            self.openid = rawJson.get("openid")
            self.pf = rawJson.get("pf")
            self.pfKey = rawJson.get("pfKey")
            self.regChannel = rawJson.get("regChannel")
            self.retk = rawJson.get("retk")
            self.rtk = rawJson.get("rtk")
            self.visitorLoginData = rawJson.get("visitorLoginData")
            self.pay_token = rawJson.get("pay_token")
        except Exception as e:
            self.msg = "Failed to parse json"
            print(e)

    def __json__(self):
        return {
            "atk": self.atk,
            "atk_expire": self.atk_expire,
            "first": self.first,
            "judgeLoginData": self.judgeLoginData,
            "msg": self.msg,
            "openid": self.openid,
            "pf": self.pf,
            "pfKey": self.pfKey,
            "regChannel": self.regChannel,
            "retk": self.retk,
            "rtk": self.rtk,
            "visitorLoginData": self.visitorLoginData,
            "pay_token": self.pay_token,
        }


class wechatChannel(channelmgr.channel):

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
        if res == None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            raise Exception(f"游戏{real_game_id}-渠道myapp暂不支持，请参照教程联系开发者发起添加请求。")

        self.wx_appid = res.get(self.channel_name).get("wx_appid")
        self.channel = res.get(self.channel_name).get("channel")

        self.wechatLogin = WechatLogin(self.wx_appid, self.channel, game_id=game_id)
        self.realGameId = real_game_id
        self.uniBody = None
        self.uniData = None
        self.session: myappVeriftResp = myappVeriftResp(session) if session != None else None

    def request_user_login(self, on_complete=None):
        """本次扫码登录：True 成功，False 失败，None 用户取消。"""
        def _do_login():
            try:
                genv.set("GLOB_LOGIN_UUID", self.uuid)
                resp = self.wechatLogin.webLogin()
                if resp is None or resp is False:
                    return resp
                if not isinstance(resp, dict) or not all(
                    resp.get(key) for key in ("atk", "openid", "atk_expire")
                ):
                    return False
                self.session = myappVeriftResp(resp)
                self.last_login_time = int(time.time())
                try:
                    r = requests.get(
                        f"https://api.weixin.qq.com/sns/userinfo?access_token={self.session.atk}&openid={self.session.openid}",
                        verify=should_verify_ssl()
                    )
                    nickname = r.json().get("nickname")
                    if nickname:
                        self.name = nickname
                except Exception:
                    pass
                return True
            except Exception:
                self.logger.error("微信扫码登录失败")
                return False

        if on_complete is not None:
            import threading
            threading.Thread(target=lambda: on_complete(_do_login())).start()
            return None
        return _do_login()

    def _refresh_session(self):
        """仅用已有凭证续期；失败时保留原凭证。"""
        if self.session is None or not self.session.rtk:
            return False
        try:
            r = requests.get(
                "https://api.weixin.qq.com/sns/oauth2/refresh_token",
                params={"appid": self.wx_appid, "grant_type": "refresh_token",
                        "refresh_token": self.session.rtk},
                verify=should_verify_ssl()
            )
            data = r.json()
            if r.status_code != 200 or not all(
                data.get(key) for key in ("access_token", "refresh_token", "expires_in")
            ) or data.get("errcode"):
                return False
            renewed = self.session.__json__()
            renewed.update(atk=data["access_token"], rtk=data["refresh_token"],
                           atk_expire=int(data["expires_in"]))
            self.session = myappVeriftResp(renewed)
            self.last_login_time = int(time.time())
            return True
        except Exception:
            self.logger.error("微信凭证续期失败")
            return False

    def is_token_valid(self):
        #	/sns/auth
        if self.session != None and self.last_login_time+self.session.atk_expire > int(time.time()):
            r = requests.get(
                    f"https://api.weixin.qq.com/sns/auth?access_token={self.session.atk}&openid={self.session.openid}",
                    verify=should_verify_ssl()
                )
            result = r.json()
            return result.get("errcode")==0
        else:
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
        self.logger.info(f"{getShortGameId(self.game_id)}")
        return json.dumps(
                {
                    "login_type": 8,
                    "session_id": "hy_gameid",
                    "session_type": "wc_actoken",
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
        """获取 UniSDK 登录数据，支持异步模式。
        
        当 token 过期需要重新登录时，可异步执行登录流程。
        
        Args:
            game_id: 游戏ID
            on_complete: 异步回调函数，接收登录数据或 None
        """
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
            if self._refresh_session():
                return _on_login_ready()
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
