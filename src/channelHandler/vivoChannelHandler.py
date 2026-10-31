# coding=UTF-8
"""
 Copyright (c) 2024 KKeygen & other contributors.

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
import string
import time
import base64
import channelmgr

from cloudRes import CloudRes
from envmgr import genv
from logutil import setup_logger
from channelHandler.channelUtils import getShortGameId
from channelHandler.vivoLogin.vivoChannel import VivoLogin

class vivoSubAccount:
    def __init__(self, data: dict) -> None:
        self.nickName = data.get("nickName", "默认帐号")
        self.subRole = data.get("subRole", "")
        self.subLevel = data.get("subLevel", "")
        self.createTime = data.get("createTime", 0)
        self.lastLoginAt = data.get("lastLoginAt", 0)
        self.lastLogin = data.get("lastLogin", False)
        self.openToken = data.get("openToken", "")
        self.subOpenId = data.get("subOpenId", "")

class vivoLoginResp:
    def __init__(self, data: dict) -> None:
        self.openId = data.get("openId", "")
        self.phone = data.get("phone", "")
        self.subMax = data.get("subMax", 4)
        self.subAccounts = [vivoSubAccount(x) for x in data.get("subAccounts", [])]
        self.nickName = data.get("nickName", "")

class vivoChannel(channelmgr.channel):

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
        chosenAccount: str = "",
        uuid: str = "",
        cookies: dict = {},
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
        self.logger = setup_logger()
        self.crossGames = False
        cloudRes = CloudRes()

        self.game_id = game_id
        self.cookies = cookies
        real_game_id = getShortGameId(game_id)
        res = cloudRes.get_channelData(self.channel_name, real_game_id)
        if res == None:
            self.logger.error(f"Failed to get channel config for {self.name}")
            self.logger.error(f"游戏{real_game_id}-渠道{self.channel_name}暂不小号登录，如有小号登录需求，请参照教程联系开发者发起添加请求。即将开始默认账号登录。")
            self.crossGames=True
            self.vivoLogin = VivoLogin()
        else:
            self.vivoLogin = VivoLogin(res.get(self.channel_name))

        self.vivoLogin.cookies = self.cookies

        self.chosenAccount = chosenAccount
        self.realGameId = real_game_id
        self.uniBody = None
        self.uniData = None
        self.session: vivoLoginResp = None
        self.activeAccount:vivoSubAccount = None
        


    def request_user_login(self):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        resp=self.vivoLogin.webLogin(self.cookies)
        self.logger.debug(resp)
        if resp==None:
            self.session=None
            return False
        self.cookies = self.vivoLogin.cookies
        self.session:vivoLoginResp=vivoLoginResp(resp)

        if len(self.session.subAccounts)==0:
            return False
        elif len(self.session.subAccounts)==1:
            self.chosenAccount=self.session.subAccounts[0].subOpenId
        else:
            if self.chosenAccount!="":#check if the chosen account is valid
                for i in range(len(self.session.subAccounts)):
                    if self.session.subAccounts[i].subOpenId==self.chosenAccount:
                        self.logger.info(f"尝试登录指定账号{self.session.subAccounts[i].nickName}")
                        break
                else:
                    self.chosenAccount=self.session.subAccounts[0].subOpenId
            #ask user
            else:
                print("有多个小号，请选择一个登录")
                for i in range(len(self.session.subAccounts)):
                    print(f"{i+1}:{self.session.subAccounts[i].nickName}")
                choice = int(input("请输入序号并回车:"))
                if choice>0 and choice<=len(self.session.subAccounts):
                    self.chosenAccount=self.session.subAccounts[choice-1].subOpenId
                else:
                    self.chosenAccount=self.session.subAccounts[0].subOpenId
        for i in range(len(self.session.subAccounts)):
            if self.session.subAccounts[i].subOpenId==self.chosenAccount:
                self.activeAccount=self.session.subAccounts[i]
                self.activeAccount.openToken = ""
                self.activeAccount.openToken=self.vivoLogin.loginSubAccount(self.activeAccount.subOpenId)
                self.cookies = self.vivoLogin.cookies
        #如果自己的uuid等于name，则说明是默认名字，登录成功后用昵称更新名字
        if self.name==self.uuid:
            self.name=f"{self.session.nickName}-{self.activeAccount.nickName}"
        return self.session!=None

    def is_token_valid(self):
        return self.session!=None

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
            chosenAccount=data.get("chosenAccount", ""),
            uuid=data.get("uuid", ""),
            cookies=data.get("cookies", {}),
        )

    def _build_extra_unisdk_data(self) -> str:
        fd = genv.get("FAKE_DEVICE")
        res = {
            "SAUTH_STR": "",
            "SAUTH_JSON": "",
        }
        extra_data = {
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

    def get_uniSdk_data(self, game_id: str = ""):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if game_id == "":
            game_id = self.game_id
        self.logger.info(f"Get unisdk data for {self.name}")
        import channelHandler.channelUtils as channelUtils

        if not self.is_token_valid():
            self.request_user_login()

        self.uniBody = channelUtils.buildSAUTH(
            self.channel_name,
            self.channel_name,
            self.activeAccount.subOpenId,
            self.activeAccount.openToken,
            getShortGameId(game_id),
            "4.7.2.0",
            {
                "realname": json.dumps({"realname_type": 0, "age": 22}),
            },
        )
        fd = genv.get("FAKE_DEVICE")
        self.logger.info(json.dumps(self.uniBody))
        self.uniData = channelUtils.postSignedData(self.uniBody,getShortGameId(game_id),True)
        self.logger.info(f"Get unisdk data for {self.uniData}")
        self.uniSDKJSON = json.loads(
            base64.b64decode(self.uniData["unisdk_login_json"]).decode()
        )
        res = {
            "user_id": self.activeAccount.subOpenId,
            "token": base64.b64encode(self.activeAccount.openToken.encode()).decode(),
            "login_channel": self.channel_name,
            "udid": fd["udid"],
            "app_channel": self.channel_name,
            "sdk_version": "4.7.2.0",
            "jf_game_id": getShortGameId(game_id),
            "pay_channel": self.channel_name,
            "extra_data": "",
            "extra_unisdk_data": self._build_extra_unisdk_data(),
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
        return res
