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
from channelHandler.channelUtils import getShortGameId
from channelHandler.huaLogin.huaChannel import HuaweiLogin


class huaweiLoginResponse:
    def __init__(self, rawJson: dict) -> None:
        self.playerLevel = rawJson.get("playerLevel")
        self.unionId = rawJson.get("unionId")
        self.openIdSign = rawJson.get("openIdSign")
        self.openId = rawJson.get("openId")
        self.gameAuthSign = rawJson.get("gameAuthSign")
        self.playerId = rawJson.get("playerId")
        self.ts = str(rawJson.get("ts"))

    def __str__(self) -> str:
        return f"playerLevel:{self.playerLevel},unionId:{self.unionId},openIdSign:{self.openIdSign},openId:{self.openId},gameAuthSign:{self.gameAuthSign},playerId:{self.playerId},ts:{self.ts}"


class huaweiChannel(channelmgr.channel):
    """华为扫码渠道（cross 类型）。

    ST（serviceToken）为账号级长期凭证，可为任意已配置的游戏包名签发 access_token，
    因此本渠道账号对所有华为渠道游戏可用（crossGames=True）。
    """

    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        serviceToken: str = "",
        game_id: str = "",
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
        self.serviceToken = serviceToken
        self.logger = setup_logger()
        self.crossGames = True
        self.game_id = game_id
        real_game_id = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res is None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            raise Exception(f"游戏{real_game_id}-渠道{self.channel_name}暂不支持，请参照教程联系开发者发起添加请求。")
        self.huaweiLogin = HuaweiLogin(res.get(self.channel_name), self.serviceToken, real_game_id)
        self.realGameId = real_game_id
        self.uniBody = None
        self.uniData = None
        self.session: huaweiLoginResponse = None

    # ── 登录（qr 扫码 / web 浏览器） ─────────────────────────

    def request_user_login(self, on_complete=None, login_method="qr"):
        """请求用户登录。

        - login_method="qr"（默认）：web UI 展示二维码，手机扫码；阻塞式，需在后台线程调用。
        - login_method="web"：内嵌浏览器打开华为登录页，URL 变 loginSuccess.html 即成功。
        """
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        if login_method == "qr":
            # 扫码登录：阻塞式，由 manual_import 在后台线程调用
            self.huaweiLogin.qrLogin()
            self.serviceToken = self.huaweiLogin.serviceToken
            return self.serviceToken is not None

        # 网页登录
        if on_complete is not None:
            def _on_done(_success):
                self.serviceToken = self.huaweiLogin.serviceToken
                on_complete(self.serviceToken is not None)
            self.huaweiLogin.webLogin(on_complete=_on_done)
            return

        self.huaweiLogin.webLogin()
        self.serviceToken = self.huaweiLogin.serviceToken
        return self.serviceToken is not None

    def is_token_valid(self):
        if not self.serviceToken:
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
            serviceToken=data.get("serviceToken", ""),
            game_id=data.get("game_id", ""),
        )

    # ── 目标游戏 session ────────────────────────────────────

    def _resolve_game_cfg(self, game_id):
        """按目标游戏解析华为渠道配置。"""
        short_gid = getShortGameId(game_id)
        item = CloudRes().get_channelData(self.channel_name, short_gid)
        if item is None:
            return None, short_gid
        game_cfg = item.get(self.channel_name)
        if not isinstance(game_cfg, dict):
            return None, short_gid
        return game_cfg, short_gid

    def _ensure_session(self, game_cfg, short_gid):
        """确保获得指定游戏的 session（accessToken → gameAuthSign）。"""
        if not self.huaweiLogin.ensure_game_token(game_cfg, short_gid):
            # ST 可能已失效，清空以便下次重新扫码
            self.serviceToken = None
            self.huaweiLogin.serviceToken = None
            return None
        try:
            data = self.huaweiLogin.initAccountData(game_cfg)
        except Exception as e:
            self.logger.error(f"{e}")
            self.logger.error("Failed to get session data")
            data = None
        if data is None:
            return None
        self.session = huaweiLoginResponse(data)
        return self.session

    # ── UniSDK 数据（cross：目标游戏按需签发 token） ────────

    def _build_extra_unisdk_data(self, short_gid: str) -> str:
        fd = app_state.fake_device
        res = {
            "SAUTH_STR": "",
            "SAUTH_JSON": "",
        }
        extra_data = {
            "anonymous": "",
            "get_access_token": "0",
            "extra_data": self._get_extra_data(short_gid),
            "timestamp": self.session.ts,
            "realname": json.dumps({"realname_type": 0, "duration": 0}),
        }
        res.update(extra_data)
        json_data = {
            "extra_data": extra_data.get("extra_data"),
            "get_access_token": "0",
            "sdk_udid": fd["udid"],
            "realname": extra_data.get("realname"),
        }
        json_data.update(self.uniBody)

        str_data = json_data.copy()
        str_data.update({"username": self.uniSDKJSON["username"]})
        # value 需 URL 编码：gameAuthSign 是 base64（含 + / = / /），游戏端按
        # application/x-www-form-urlencoded 解析，裸 + 会被当成空格导致 session 损坏。
        from urllib.parse import quote
        str_data = "&".join([f"{k}={quote(str(v), safe='')}" for k, v in str_data.items()])

        res["SAUTH_STR"] = base64.b64encode(str_data.encode()).decode()
        res["SAUTH_JSON"] = base64.b64encode(json.dumps(json_data).encode()).decode()
        return json.dumps(res)

    def _get_extra_data(self, short_gid: str):
        if short_gid == "g37":
            self.logger.info(f"游戏{short_gid}-需要HMS AccessToken, 二次登录中")
            ext = {}
            ext["playerLevel"] = str(self.session.playerLevel)
            sdk = {}
            sdk["transtition_version"] = 1
            sdk["openId"] = self.session.openId
            sdk["accessToken"] = self.huaweiLogin.accessToken
            ext["sdk_info"] = sdk
            return json.dumps(ext)

        return str(self.session.playerLevel)

    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        """获取 UniSDK 登录数据（cross 渠道：可为任意已配置游戏签发）。

        Args:
            game_id: 目标游戏 ID；为空时使用账号自身 game_id。
            on_complete: 异步回调，接收登录数据或 None。
        """
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if game_id == "":
            game_id = self.game_id
        self.logger.info(f"Get unisdk data for {self.name} (game={game_id})")

        game_cfg, short_gid = self._resolve_game_cfg(game_id)
        if game_cfg is None:
            self.logger.error(f"游戏{short_gid}-渠道{self.channel_name}暂不支持，请参照教程联系开发者发起添加请求。")
            if on_complete is not None:
                on_complete(None)
            return None

        if not self.is_token_valid():
            # 无 ST，需要重新登录
            if on_complete is not None:
                def _on_login_done(success):
                    if success and self.is_token_valid():
                        on_complete(self._build(game_cfg, short_gid))
                    else:
                        on_complete(None)
                self.request_user_login(on_complete=_on_login_done, login_method="web")
                return None
            self.request_user_login()

        result = self._build(game_cfg, short_gid)
        if on_complete is not None:
            on_complete(result)
            return None
        return result

    def _build(self, game_cfg, short_gid):
        if self._ensure_session(game_cfg, short_gid) is None:
            return None
        try:
            return self._build_unisdk_data(short_gid)
        except Exception as e:
            self.logger.error(f"构建 UniSDK 数据失败: {e}")
            return None

    def _build_unisdk_data(self, short_gid: str):
        import channelHandler.channelUtils as channelUtils

        fd = app_state.fake_device
        self.uniBody = channelUtils.buildSAUTH(
            self.channel_name,
            self.channel_name,
            self.session.playerId,
            self.session.gameAuthSign,
            short_gid,
            "6.1.0.301",
            {
                "anonymous": "",
                "get_access_token": "0",
                "extra_data": self._get_extra_data(short_gid),
                "timestamp": str(self.session.ts),
                "realname": json.dumps({"realname_type": 0, "duration": 0}),
            },
        )
        self.uniData = channelUtils.postSignedData(self.uniBody, short_gid, False)
        self.uniSDKJSON = json.loads(
            base64.b64decode(self.uniData["unisdk_login_json"]).decode()
        )
        res = {
            "user_id": self.session.playerId,
            "token": base64.b64encode(self.session.gameAuthSign.encode()).decode(),
            "login_channel": self.channel_name,
            "udid": fd["udid"],
            "app_channel": self.channel_name,
            "sdk_version": "6.1.0.301",
            "jf_game_id": short_gid,
            "pay_channel": self.channel_name,
            "extra_data": "",
            "extra_unisdk_data": self._build_extra_unisdk_data(short_gid),
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
        return res
