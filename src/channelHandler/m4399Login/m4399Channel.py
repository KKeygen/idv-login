# coding=UTF-8
"""4399 游戏盒渠道 H5 OAuth 登录（无盒子路径）。

流程（已逆向验证）：
1. POST https://m.4399api.com/openapiv2/oauth.html
   body: device={JSON 大串(含 GAME_KEY/CANAL_IDENTIFIER/UDID)}&top_bar=1
   → code 607 + result.login_url (ptlogin.4399.com/oauth2/authorize.do?...)
2. WebView 加载 login_url，用户完成 4399 通行证登录
3. 页面跳转 oauth-callback.html?gamekey=44553&game_key=114816&...（URL 导航）
4. 拦截导航 URL，带浏览器 cookies GET 该 URL → code=100 JSON
   {uid, access_token, state="uid|token|44553|device|state|...|phone", account_type}
5. 由 handler 用 state 走 uni_sauth 换网易侧登录态
"""
import json
import time
import random
import string
from typing import Any, Dict, Optional

import requests
from PyQt6.QtCore import QTimer

from channelHandler.WebLoginUtils import WebBrowser
from ssl_utils import should_verify_ssl
from logutil import setup_logger

# 4399 通行证开放平台接口
OAUTH_API = "https://m.4399api.com/openapiv2/oauth.html"
OAUTH_CALLBACK_MARK = "oauth-callback.html"


def _build_udid() -> str:
    """构造 62 位 UDID（与 SDK 侧生成格式一致：时间戳 + md5(uuid) + 校验尾）。"""
    import hashlib
    prefix = time.strftime("%Y%m%d%H%M%S") + hashlib.md5(
        uuid_str().encode()
    ).hexdigest() + "00"
    return prefix + hashlib.md5(("cn.m4399.uuid" + prefix).encode()).hexdigest()[:14]


def uuid_str() -> str:
    import uuid
    return uuid.uuid4().hex


def fetch_login_url(
    game_key: str,
    bid: str,
    canal: str,
    sdk_version: str,
    device_identifier: str = "pc-idv-login",
) -> Optional[Dict[str, Any]]:
    """请求 oauth.html，返回 607 响应中的 login_url 系列。

    Returns:
        dict: {"login_url": ..., "login_url_backup": ...,
               "login_url_phone": ..., "login_url_backup_phone": ...}
        失败返回 None。
    """
    device = {
        "DEVICE_IDENTIFIER": device_identifier,
        "PLATFORM_TYPE": "android",
        "SDK_VERSION": sdk_version,
        "GAME_KEY": game_key,
        "GAME_VERSION": "1.0.286",
        "GAME_VERSION_CODE": 286,
        "BID": bid,
        "RUNTIME": "android",
        "CANAL_IDENTIFIER": canal,
        "UDID": _build_udid(),
        "DEBUG": "false",
        "VIP_INFO": "",
        "TEAM": 0,
        "UID": "",
        "SCREEN_RESOLUTION": "1080*1920",
        "DEVICE_MODEL": "Pixel 7",
        "SYSTEM_VERSION": "13",
        "NETWORK_TYPE": "wifi",
    }
    body = "device=" + requests.utils.quote(json.dumps(device)) + "&top_bar=1"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36",
    }
    try:
        resp = requests.post(OAUTH_API, data=body, headers=headers,
                             verify=should_verify_ssl(), timeout=20)
        data = resp.json()
    except Exception as e:
        logger = setup_logger()
        logger.error(f"4399 oauth.html 请求异常: {e}")
        return None

    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    if str(data.get("code")) != "607" or not result.get("login_url"):
        logger = setup_logger()
        logger.warning(f"4399 oauth.html 未返回登录页: code={data.get('code')} msg={data.get('message')}")
        return None
    return result


class M4399Browser(WebBrowser):
    """QWebEngine 浏览器：加载 4399 通行证授权页并捕获 oauth-callback 结果。"""

    def __init__(self, login_url: str):
        super().__init__("m4399", True)
        self.logger = setup_logger()
        self._login_url = login_url
        self._callback_url: Optional[str] = None
        self._captured: Optional[Dict[str, Any]] = None
        # 4399 登录页为完整 H5 页面，使用常规窗口
        self.resize(430, 680)

    # ── 回调 ──────────────────────────────────────────────────

    def verify(self, url: str) -> bool:
        # 登录成功后页面跳转到 oauth-callback.html（URL 导航）
        return OAUTH_CALLBACK_MARK in url

    def parseReslt(self, url: str):
        # 记录回调 URL，异步拉取结果（避免在导航回调栈内做网络请求）
        self._callback_url = url
        QTimer.singleShot(0, self._fetch_callback)
        return False

    def _fetch_callback(self):
        if not self._callback_url:
            return
        try:
            cookies = {k: v for k, v in self.cookies.items()}
            resp = requests.get(self._callback_url, cookies=cookies,
                                verify=should_verify_ssl(), timeout=20)
            data = resp.json()
        except Exception as e:
            self.logger.error(f"4399 oauth-callback 解析失败: {e}")
            self._callback_url = None
            return

        code = str(data.get("code", ""))
        if code in ("100", "200") and data.get("result"):
            self._captured = data
            self.result = data
            self.logger.info("已捕获 4399 OAuth 登录结果 (uid=%s)", data["result"].get("uid"))
            self.cleanup()
        else:
            self.logger.warning(f"4399 oauth-callback 未返回成功: code={code} msg={data.get('message')}")
            self._callback_url = None

    def cleanup(self):
        super().cleanup()


class M4399Login:
    """封装 4399 H5 OAuth 登录流程。"""

    def __init__(
        self,
        game_key: str,
        bid: str,
        canal: str,
        sdk_version: str,
    ):
        self.logger = setup_logger()
        self.game_key = game_key
        self.bid = bid
        self.canal = canal
        self.sdk_version = sdk_version
        self._active_browser: Optional[M4399Browser] = None
        self.login_resp: Optional[Dict[str, Any]] = None

    def get_login_url(self) -> Optional[str]:
        result = fetch_login_url(self.game_key, self.bid, self.canal, self.sdk_version)
        if not result:
            return None
        return result.get("login_url") or result.get("login_url_backup")

    def web_login(self, on_complete=None) -> Optional[Dict[str, Any]]:
        """启动浏览器登录。

        同步模式：阻塞至登录完成，返回 oauth 回调 JSON（code=100）。
        异步模式（on_complete 非 None）：立即返回 None，登录完成后回调。
        """
        login_url = self.get_login_url()
        if not login_url:
            self.logger.error("无法获取 4399 登录页 URL")
            if on_complete is not None:
                on_complete(None)
            return None

        browser = M4399Browser(login_url)
        browser.set_url(login_url)
        resp = browser.run()

        if resp is None:
            # 异步模式：保持强引用，登录完成后回调
            self._active_browser = browser
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None
                    try:
                        result = b.result
                        if isinstance(result, dict) and result.get("result"):
                            self.login_resp = result
                            on_complete(result)
                        else:
                            on_complete(None)
                    except Exception:
                        self.logger.exception("4399 异步登录回调失败")
                        on_complete(None)
                browser._async_completion_callback = _on_async_done
            return None

        if isinstance(resp, dict) and resp.get("result"):
            self.login_resp = resp
            return resp
        return None
