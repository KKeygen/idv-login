import json
import os
import random
import string
import time
import urllib.parse

import requests
from faker import Faker
from logutil import setup_logger, safe_exception
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
from channelHandler.honorLogin.account import HonorAccountLogin
from channelHandler.honorLogin.session import HonorSessionError, app_version_code, parse_session, valid_redirect
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

    def __init__(self, expected_state):
        super().__init__("honor", True, frameless=True)
        self.expected_state = expected_state
        self.logger = setup_logger()
        self.scheme_handler = self.HonorSchemeHandler(self)
        self.profile.removeAllUrlSchemeHandlers()
        self.profile.installUrlSchemeHandler(b"honorid", self.scheme_handler)

    def verify(self, url):
        return valid_redirect(url, self.expected_state, HONOR_REDIRECT_URI)

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


class HonorLogin(HonorAccountLogin):
    """荣耀渠道登录：OAuth → Game Center API → unionToken。"""

    def __init__(self, channelConfig: dict, unionToken: dict = None, accountSession: dict = None, game_id=""):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.channelConfig = channelConfig
        # unionToken 持久化格式: {"openId": "...", "token": "...", "unionId": "...", "expireTimeout": ...}
        self.unionToken = unionToken
        self.accountSession = dict(accountSession or {})
        self.account_game_id = game_id
        self.displayName = ""
        self.code_verifier = None
        self.lastLoginTime = 0
        self.expiredTime = 0
        self.lastSessionError = None
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

    def _openOAuthLogin(self, on_complete=None):
        """打开已准备配置的荣耀 OAuth 浏览器。"""
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
                        safe_exception(self.logger, "荣耀异步登录处理失败")
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

        self.lastSessionError = None
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
            self.expiredTime = now + lifetime if lifetime is not None else None
            self.logger.info("荣耀登录成功")
            return True
        except HonorSessionError as error:
            self.lastSessionError = error
            self.logger.warning("荣耀 Game Center login 失败，错误码: {}", error.code)
            return False
        except Exception as error:
            self.logger.error("荣耀 Game Center login 请求失败（{}）", type(error).__name__)
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

    def gameConfig(self):
        """首次/重新授权前的配置请求；失败不阻断账号登录。"""
        body = {
            "amsPackageName": HONOR_AMS_PACKAGE,
            "appVersionCode": app_version_code(self.channelConfig),
            "tInfo": self._build_terminal_info(),
        }
        headers = {
            "apkVer": str(HONOR_APK_VER), "core-version": str(HONOR_CORE_VERSION),
            "Area-Id": HONOR_AREA_ID, "sdk-version": str(HONOR_CORE_VERSION),
            "x-uuid": self.device["deviceId"], "Content-Type": "application/json",
        }
        try:
            resp = requests.post(f"{HONOR_GAMECENTER_API}/game/union/sdk/v1/aggregate/config",
                                 json=body, headers=headers, timeout=30, verify=should_verify_ssl())
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or data.get("errorCode") not in (None, 0, "0"):
                return False
            self.gameConfigResponse = data
            return True
        except Exception as error:
            self.logger.warning("荣耀配置请求未完成，继续登录（{}）", type(error).__name__)
            return False

    # ── Session 刷新 ──────────────────────────────────────────

    def configLogin(self):
        """通过 configLogin 接口用现有 unionToken 刷新 session（无需重新 OAuth）。"""
        self.lastSessionError = None
        if not isinstance(self.unionToken, dict) or not all(
                isinstance(self.unionToken.get(key), str) and self.unionToken[key]
                for key in ("openId", "token")):
            return False

        url = f"{HONOR_GAMECENTER_API}/game/union/sdk/v1/aggregate/configLogin"

        body = {
            "amsPackageName": HONOR_AMS_PACKAGE,
            "appVersionCode": app_version_code(self.channelConfig),
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
            self.expiredTime = now + lifetime if lifetime is not None else None
            self.logger.info("荣耀 configLogin 刷新成功")
            return True
        except HonorSessionError as error:
            self.lastSessionError = error
            self.logger.warning("荣耀 configLogin 失败，错误码: {}", error.code)
            return False
        except Exception as error:
            self.logger.error("荣耀 configLogin 请求失败（{}）", type(error).__name__)
            return False

    # ── 状态检查 ──────────────────────────────────────────────

    def is_token_expired(self) -> bool:
        if self.unionToken is None:
            return True
        now = int(time.time())
        return self.expiredTime is not None and now >= self.expiredTime
