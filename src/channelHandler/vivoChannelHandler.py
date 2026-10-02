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
import time
import base64
import channelmgr

from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger
from channelHandler.channelUtils import getShortGameId
from channelHandler.vivoLogin.vivoChannel import VivoLogin

from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)


def _show_account_selector(accounts: list) -> str:
    """弹出账号选择对话框（阻塞式，使用 exec() 嵌套事件循环）。
    
    Args:
        accounts: vivoSubAccount 列表
    
    Returns:
        选中的 subOpenId，或取消时返回 None
    """
    app_inst = QApplication.instance()
    if app_inst is None:
        return accounts[0].subOpenId if accounts else ""

    parent = QWidget()
    dialog = QDialog(parent)
    dialog.setWindowTitle("选择小号")
    dialog.setMinimumWidth(300)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("检测到多个小号，请选择要登录的账号："))

    lst = QListWidget(dialog)
    for acc in accounts:
        lst.addItem(f"{acc.nickName}")
    lst.setCurrentRow(0)
    layout.addWidget(lst)

    remember_cb = QCheckBox("记住选择（下次自动使用此小号）", dialog)
    remember_cb.setChecked(True)
    layout.addWidget(remember_cb)

    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    # exec() 会启动嵌套事件循环，不会阻塞 UI
    if dialog.exec() != QDialog.DialogCode.Accepted:
        parent.deleteLater()
        return None

    row = lst.currentRow()
    if row < 0 or row >= len(accounts):
        parent.deleteLater()
        return accounts[0].subOpenId if accounts else ""

    selected_id = accounts[row].subOpenId
    should_remember = remember_cb.isChecked()
    parent.deleteLater()
    
    return selected_id, should_remember

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
        

    def _refresh_open_token(self) -> bool:
        if self.activeAccount is None:
            self.logger.error("Vivo登录失败：未找到选中的小号")
            return False
        open_token = self.vivoLogin.loginSubAccount(self.activeAccount.subOpenId)
        if not open_token:
            self.logger.error("Vivo登录失败：未获取到新的登录凭证")
            return False
        self.activeAccount.openToken = open_token
        return True


    def request_user_login(self, on_complete=None, user_id: str = ""):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        try:
            if on_complete is not None:
                self.vivoLogin.webLogin(self.cookies, on_complete=lambda resp: on_complete(self._apply_login_data(resp, user_id)))
                return None
            return self._apply_login_data(self.vivoLogin.webLogin(self.cookies), user_id)
        except Exception:
            self.logger.error("渠道登录未完成")
            if on_complete is not None:
                on_complete(False)
            return False

    def _apply_login_data(self, resp, user_id="", *, interactive=True):
        if resp is None or resp is False:
            return resp
        if not isinstance(resp, dict) or not resp.get("openId"):
            return False
        try:
            candidate = vivoLoginResp(resp)
            if not candidate.subAccounts or any(not account.subOpenId for account in candidate.subAccounts):
                return False
            chosen_id = user_id or self.chosenAccount
            selected = next((account for account in candidate.subAccounts if account.subOpenId == chosen_id), None)
            if user_id and selected is None:
                return False
            if selected is None and len(candidate.subAccounts) == 1:
                selected = candidate.subAccounts[0]
            if selected is None:
                if not interactive:
                    return False
                choice = _show_account_selector(candidate.subAccounts)
                if choice is None:
                    return None
                chosen_id = choice[0] if isinstance(choice, tuple) else choice
                selected = next((account for account in candidate.subAccounts if account.subOpenId == chosen_id), None)
                if selected is None:
                    return False
            token = self.vivoLogin.loginSubAccount(selected.subOpenId)
            if not token:
                return False
            selected.openToken = token
        except Exception:
            self.logger.error("Vivo登录数据或小号凭证无效")
            return False
        self.session = candidate
        self.activeAccount = selected
        self.chosenAccount = selected.subOpenId
        self.cookies = self.vivoLogin.cookies
        if self.name == self.uuid:
            self.name = f"{candidate.nickName}-{selected.nickName}"
        return True

    def is_token_valid(self):
        return bool(
            self.session is not None
            and self.activeAccount is not None
            and self.activeAccount.openToken
        )

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
        fd = app_state.fake_device
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

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        should_refresh_open_token = self.is_token_valid()
        if game_id == "":
            game_id = self.game_id
        self.logger.info(f"Get unisdk data for {self.name}")
        import channelHandler.channelUtils as channelUtils

        def _build_unisdk_data():
            """构建 UniSDK 数据"""
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
            fd = app_state.fake_device
            self.uniData = channelUtils.postSignedData(self.uniBody,getShortGameId(game_id),True)
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

        if not self.is_token_valid():
            if not interactive:
                restored = self._apply_login_data(self.vivoLogin.webLogin(self.cookies, interactive=False), interactive=False)
                if restored is not True:
                    if on_complete is not None:
                        on_complete(False)
                    return False
            elif on_complete is not None:
                def _on_login_done(success):
                    if success is True and self.is_token_valid():
                        try:
                            result = _build_unisdk_data()
                            on_complete(result)
                        except Exception as e:
                            self.logger.error(f"构建 UniSDK 数据失败: {e}")
                            on_complete(False)
                    else:
                        on_complete(success)
                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                success = self.request_user_login()
                if success is not True:
                    return success

        if should_refresh_open_token:
            if not self._refresh_open_token():
                if on_complete is not None:
                    on_complete(False)
                return False

        try:
            result = _build_unisdk_data()
        except Exception:
            self.logger.error("构建渠道登录数据失败")
            if on_complete is not None:
                on_complete(False)
            return False
        if on_complete is not None:
            on_complete(result)
            return None
        return result
