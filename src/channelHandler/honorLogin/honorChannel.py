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
from channelHandler.honorLogin.utils import get_authorization_code, exchange_code_for_token
from channelHandler.WebLoginUtils import WebBrowser
from PyQt6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

DEVICE_RECORD = "honor_device.json"


class HonorBrowser(WebBrowser):
    """荣耀 OAuth 浏览器，拦截 honorid:// scheme。"""

    class HonorSchemeHandler(QWebEngineUrlSchemeHandler):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.parent = parent

        def requestStarted(self, info: QWebEngineUrlRequestJob):
            url = info.requestUrl().toString()
            if url.startswith("honorid://"):
                self.parent.notify(info.requestUrl())

    def __init__(self):
        super().__init__("honor", True, frameless=True)
        self.logger = setup_logger()
        self.scheme_handler = self.HonorSchemeHandler(self)
        self.profile.removeAllUrlSchemeHandlers()
        self.profile.installUrlSchemeHandler(b"honorid", self.scheme_handler)

    def verify(self, url):
        return url.startswith("honorid://")

    def parseReslt(self, url):
        self.result = url
        return True

    def notify(self, url):
        if self.verify(url.toString()):
            if self.parseReslt(url.toString()):
                try:
                    self.profile.removeAllUrlSchemeHandlers()
                except Exception:
                    pass
                self.cleanup()


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

        browser = HonorBrowser()
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
                            on_complete(False)
                            return
                        self._handle_redirect(b.result)
                        on_complete(self.unionToken is not None)
                    except Exception:
                        self.logger.exception("荣耀异步登录处理失败")
                        on_complete(False)
                browser._async_completion_callback = _on_async_done
            return

        self._handle_redirect(res)

    def _handle_redirect(self, url: str):
        """从 honorid://redirect_url?code=XXX 提取 code，直接传给 Game Center login。"""
        self.logger.info(f"荣耀 OAuth redirect URL: {url[:80]}...")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        code_list = params.get("code")
        if not code_list:
            self.logger.error(f"荣耀 OAuth redirect 未包含 code: {url}")
            return

        auth_code = code_list[0]
        self.logger.info(f"荣耀 OAuth code 提取成功: {auth_code[:8]}...")

        # 荣耀 Game Center 直接接受 oauthCode（服务端自行换 token），不需要客户端先 exchange
        self._game_center_login(auth_code)

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

        try:
            resp = requests.post(url, json=body, headers=headers, verify=should_verify_ssl())
            data = resp.json()
            # 响应结构: {"loginData": {"errorCode": 0, "data": {"unionToken": {...}, ...}}}
            login_wrapper = data.get("loginData") or data.get("data") or {}
            if isinstance(login_wrapper, dict) and login_wrapper.get("errorCode") not in (0, None):
                self.logger.error(f"荣耀 Game Center login 错误: {login_wrapper.get('errorMessage')}")
                self.unionToken = None
                return
            login_data = login_wrapper.get("data") or login_wrapper
            ut = login_data.get("unionToken")
            if ut and ut.get("openId"):
                self.unionToken = ut
                self.displayName = str(login_data.get("displayName", ""))
                self.lastLoginTime = int(time.time())
                self.expiredTime = self.lastLoginTime + ut.get("expireTimeout", 3600)
                self.logger.info(f"荣耀登录成功: openId={ut['openId'][:8]}..., displayName={self.displayName}")
            else:
                self.logger.error(f"荣耀 Game Center login 未返回有效 unionToken: {data}")
                self.unionToken = None
        except Exception:
            self.logger.exception("荣耀 Game Center login 请求失败")
            self.unionToken = None

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
            resp = requests.post(url, json=body, headers=headers, verify=should_verify_ssl())
            data = resp.json()
            login_wrapper = data.get("loginData") or data.get("data") or {}
            login_data = login_wrapper.get("data") or login_wrapper
            ut = login_data.get("unionToken")
            if ut and ut.get("openId"):
                self.unionToken = ut
                dn = str(login_data.get("displayName", ""))
                if dn:
                    self.displayName = dn
                self.lastLoginTime = int(time.time())
                self.expiredTime = self.lastLoginTime + ut.get("expireTimeout", 3600)
                self.logger.info("荣耀 configLogin 刷新成功")
                return True
            else:
                self.logger.warning(f"荣耀 configLogin 刷新失败: {data}")
                return False
        except Exception:
            self.logger.exception("荣耀 configLogin 请求失败")
            return False

    # ── 状态检查 ──────────────────────────────────────────────

    def is_token_expired(self) -> bool:
        if self.unionToken is None:
            return True
        now = int(time.time())
        return now >= self.expiredTime
