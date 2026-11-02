import json
import os
import random
import string
import time
import urllib.parse

import requests
from faker import Faker
from logutil import setup_logger
from envmgr import genv
from ssl_utils import should_verify_ssl

from channelHandler.honorLogin.consts import (
    HONOR_REDIRECT_URI,
    HONOR_SCOPE,
    HONOR_GAMECENTER_API,
    HONOR_APK_VER,
    HONOR_APK_VER_NAME,
    HONOR_CORE_VERSION,
    HONOR_AREA_ID,
    HONOR_CHANNEL_ID,
    HONOR_AMS_PACKAGE,
)
from channelHandler.honorLogin.utils import get_authorization_code
from channelHandler.honorLogin.session import parse_session, valid_redirect
from channelHandler.WebLoginUtils import WebBrowser

DEVICE_RECORD = "honor_device.json"


class HonorBrowser(WebBrowser):
    """荣耀 OAuth 浏览器，拦截 honorid:// scheme。"""

    callback_schemes = ("honorid",)

    def __init__(self, expected_state):
        super().__init__("honor", True)
        self.expected_state = expected_state
        self.logger = setup_logger()

    def verify(self, url):
        return valid_redirect(url, self.expected_state, HONOR_REDIRECT_URI)

    def parseReslt(self, url):
        self.result = url
        return True


class HonorLogin:
    """荣耀渠道登录：OAuth → Game Center API → unionToken。"""

    def __init__(self, channelConfig: dict, unionToken: dict = None):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.channelConfig = channelConfig
        # unionToken 持久化格式: {"openId": "...", "token": "...", "unionId": "...", "expireTimeout": ...}
        self.unionToken = unionToken
        self.displayName = ""
        self.code_verifier = None
        self.lastLoginTime = 0
        self.expiredTime = 0
        self._active_browser: HonorBrowser = None

        if os.path.exists(DEVICE_RECORD):
            with open(DEVICE_RECORD, "r", encoding="utf-8") as f:
                self.device = json.load(f)
        else:
            self.device = self._make_fake_device()
            from secure_write import write_json_restricted
            write_json_restricted(DEVICE_RECORD, self.device)

    @staticmethod
    def _make_fake_device() -> dict:
        fake = Faker()
        manufacturers = ["HONOR", "Samsung", "Xiaomi"]
        brand = random.choice(manufacturers)
        return {
            "deviceId": "".join(random.choice("abcdef" + string.digits) for _ in range(64)),
            "brand": brand,
            "manufacturer": brand,
            "phoneType": fake.lexify(text="SM-????", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            "androidVersion": random.choice(["12", "13", "14"]),
        }

    # ── OAuth 登录 ──────────────────────────────────────────

    def newOAuthLogin(self, on_complete=None):
        """打开浏览器进行荣耀 OAuth 登录。"""
        client_id = str(self.channelConfig["app_id"])
        auth_url, self.code_verifier = get_authorization_code(client_id, HONOR_REDIRECT_URI, HONOR_SCOPE)

        expected_state = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)["state"][0]
        self._expected_state = expected_state
        browser = HonorBrowser(expected_state)
        browser.set_url(auth_url)
        res = browser.run()

        if res is None:
            # 异步模式
            self._active_browser = browser
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None
                    try:
                        if not b.result or b.result == "":
                            self.logger.warning("荣耀登录未完成（用户取消或窗口关闭）")
                            on_complete(None)
                            return
                        on_complete(self._handle_redirect(b.result))
                    except Exception:
                        self.logger.exception("荣耀异步登录处理失败")
                        on_complete(False)
                browser._async_completion_callback = _on_async_done
            return

        result = None if not res else self._handle_redirect(res)
        if on_complete is not None:
            on_complete(result)
        return result

    def _handle_redirect(self, url: str):
        """从 honorid://redirect_url?code=XXX 提取 code，直接传给 Game Center login。"""
        if not valid_redirect(url, getattr(self, "_expected_state", None), HONOR_REDIRECT_URI):
            self.logger.warning("荣耀 OAuth 回调校验失败")
            return False
        self._expected_state = None
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        auth_code = params["code"][0]

        # 荣耀 Game Center 直接接受 oauthCode（服务端自行换 token），不需要客户端先 exchange
        return self._game_center_login(auth_code)

    def _game_center_login(self, oauth_code: str):
        """调用荣耀游戏中心 aggregate/login 获取 unionToken。"""
        url = f"{HONOR_GAMECENTER_API}/game/union/sdk/v1/aggregate/login"

        body = {
            "amsPackageName": HONOR_AMS_PACKAGE,
            "appId": str(self.channelConfig["app_id"]),
            "oauthCode": oauth_code,
            "realNameAuthMode": 0,
            "scene": 0,
            "trackingStr": "",
            "tInfo": self._build_terminal_info(),
        }

        headers = {
            "apkVer": str(HONOR_APK_VER),
            "core-version": str(HONOR_CORE_VERSION),
            "Area-Id": HONOR_AREA_ID,
            "sdk-version": str(HONOR_CORE_VERSION),
            "x-uuid": self.device["deviceId"],
            "Content-Type": "application/json",
        }

        data = None
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30, verify=should_verify_ssl())
            resp.raise_for_status()
            data = resp.json()
            ut, name, lifetime = parse_session(data)
            now = int(time.time())
            self.unionToken = ut
            self.displayName = name
            self.lastLoginTime = now
            self.expiredTime = now + lifetime
            self.logger.info("荣耀登录成功")
            return True
        except Exception:
            if isinstance(data, dict):
                login_wrapper = data.get("loginData") or data.get("data") or {}
                if isinstance(login_wrapper, dict) and login_wrapper.get("errorMessage"):
                    self.logger.error(f"荣耀 Game Center login 错误: {login_wrapper.get('errorMessage')}")
            self.logger.exception("荣耀 Game Center login 请求失败")
            return False

    def _build_terminal_info(self, open_id: str = "", access_token: str = "") -> dict:
        pkg = self.channelConfig.get("package_name", "com.netease.dwrg.honor")
        return {
            "openId": open_id,
            "accessToken": access_token,
            "randomId": self.device["deviceId"],
            "chId": HONOR_CHANNEL_ID,
            "pName": pkg,
            "apkVer": HONOR_APK_VER,
            "apkVerName": HONOR_APK_VER_NAME,
            "hman": self.device.get("brand", "HONOR"),
            "htype": self.device.get("phoneType", "unknown"),
            "osVer": self.device.get("androidVersion", "12"),
            "language": "zh_CN",
        }

    # ── Session 刷新 ──────────────────────────────────────────

    def configLogin(self):
        """通过 configLogin 接口用现有 unionToken 刷新 session（无需重新 OAuth）。"""
        if not self.unionToken:
            return False

        url = f"{HONOR_GAMECENTER_API}/game/union/sdk/v1/aggregate/configLogin"

        body = {
            "amsPackageName": HONOR_AMS_PACKAGE,
            "appId": str(self.channelConfig["app_id"]),
            "oauthCode": "",
            "realNameAuthMode": 0,
            "scene": 0,
            "trackingStr": "",
            "tInfo": self._build_terminal_info(
                open_id=self.unionToken.get("openId", ""),
                access_token=self.unionToken.get("token", ""),
            ),
        }

        headers = {
            "apkVer": str(HONOR_APK_VER),
            "core-version": str(HONOR_CORE_VERSION),
            "Area-Id": HONOR_AREA_ID,
            "sdk-version": str(HONOR_CORE_VERSION),
            "x-uuid": self.device["deviceId"],
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(url, json=body, headers=headers, timeout=30, verify=should_verify_ssl())
            resp.raise_for_status()
            ut, name, lifetime = parse_session(resp.json())
            now = int(time.time())
            self.unionToken = ut
            self.displayName = name
            self.lastLoginTime = now
            self.expiredTime = now + lifetime
            self.logger.info("荣耀 configLogin 刷新成功")
            return True
        except Exception:
            self.logger.exception("荣耀 configLogin 请求失败")
            return False

    # ── 状态检查 ──────────────────────────────────────────────

    def is_token_expired(self) -> bool:
        if self.unionToken is None:
            return True
        now = int(time.time())
        return now >= self.expiredTime
