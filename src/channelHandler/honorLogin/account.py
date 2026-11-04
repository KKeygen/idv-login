"""荣耀账号扫码登录与长期账号态恢复。"""
import base64
import hashlib
import io
import random
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

import qrcode
import requests
from PyQt6.QtCore import QTimer

import app_state
from envmgr import genv
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.WebLoginUtils import WebBrowser

ACCOUNT_APP_ID = "com.hihonor.id"
ACCOUNT_VERSION = "9.0.2.351"
INTERFACE_VERSION = "60101"
QRCODE_CACHE_KEY = "HONOR_QRCODE_CACHE"
SITES = {
    "1": ("hnid-drcn.cloud.hihonor.com", "hnid-drcn.platform.hihonorcloud.com", "login.platform.hihonorcloud.com"),
    "5": ("hnid-dra.cloud.hihonor.com", "hnid-dra.platform.hihonorcloud.com", "hnoauth-login-dra.cloud.hihonor.com"),
    "7": ("hnid-dre.cloud.hihonor.com", "hnid-dre.platform.hihonorcloud.com", "hnoauth-login-dre.cloud.hihonor.com"),
    "8": ("hnid-drru.cloud.hihonor.com", "hnid-drru.platform.hihonorcloud.com", "hnoauth-login-drru.cloud.hihonor.com"),
}


class HonorAccountSession:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})
        self.device_id = self.saved.get("deviceId") or str(uuid.uuid4())
        self.session = requests.Session()
        self.logger = setup_logger()
        self.qr_info = {}

    def _post(self, site, service, path, data, content_type="application/x-www-form-urlencoded; charset=UTF-8", extra_query=None, game_headers=None):
        host = SITES[str(site)][service]
        query = {
            "Version": INTERFACE_VERSION,
            "cVersion": "HiHonorID_" + ACCOUNT_VERSION,
            "blackScreen": "0", "appBrand": "HONOR",
            "ctrID": str(int(time.time() * 1000)) + "".join(random.choices("0123456789", k=19)),
        }
        query.update(extra_query or {})
        headers = {"Content-Type": content_type, "x-apk-version": ACCOUNT_VERSION,
                   "PKG": ACCOUNT_APP_ID, "UUID": self.device_id}
        if game_headers is None:
            headers["Authorization"] = str(int(time.time() * 1000))
        else:
            headers.update(game_headers)
        response = self.session.post(
            "https://" + host + path, params=query, data=data, headers=headers,
            timeout=30, verify=should_verify_ssl(),
        )
        response.raise_for_status()
        return response

    def get_qr_info(self):
        info = self._post("1", 0, "/DimensionalCode/getqrInfo", {
            "version": INTERFACE_VERSION, "appID": ACCOUNT_APP_ID,
            "loginChannel": "7000700", "reqClientType": "701", "confirmFlag": "1", "lang": "zh_CN",
        }).json()
        params = parse_qs(urlsplit(info.get("content", "")).query)
        info["qrCode"] = params.get("qRCode", [""])[0]
        info["qrSiteID"] = params.get("qrSiteID", ["1"])[0]
        if not all(info.get(key) for key in ("content", "qrCode", "qrToken")):
            raise ValueError("荣耀账号未返回有效登录二维码")
        self.qr_info = info
        return info

    def poll(self):
        return self._post(self.qr_info["qrSiteID"], 0, "/DimensionalCode/async",
                          {"qrToken": self.qr_info["qrToken"]}).json()

    def exchange(self, scan, device):
        site = str(scan.get("siteID") or "1")
        root = ET.Element("LoginByQrCodeReq")
        fields = {
            "version": INTERFACE_VERSION, "accountType": scan.get("accountType") or "0",
            "userAccount": scan.get("userAccount", ""), "userID": scan.get("userID", ""),
            "qrCode": self.qr_info["qrCode"], "qrSiteID": self.qr_info["qrSiteID"],
            "code": scan.get("code", ""), "appID": ACCOUNT_APP_ID,
            "reqClientType": "7", "loginChannel": "7000000",
            "osVersion": device.get("androidVersion", ""), "plmn": "", "uuid": self.device_id,
            "languageCode": "zh_CN", "deviceSecure": "0", "mainAcctLogin": "1",
            "clientID": "", "riskToken": scan.get("riskToken", ""),
        }
        for key, value in fields.items():
            ET.SubElement(root, key).text = str(value)
        info = ET.SubElement(root, "deviceInfo")
        for key, value in (("deviceID", self.device_id), ("deviceType", "6"), ("terminalType", "Android")):
            ET.SubElement(info, key).text = value
        ET.SubElement(root, "loginType").text = "1"
        response = self._post(site, 1, "/AccountServer/IDM/loginByQrCode",
                              ET.tostring(root, encoding="utf-8", xml_declaration=True), "text/html; charset=UTF-8")
        try:
            result = ET.fromstring(response.content)
        except ET.ParseError:
            self.logger.error("荣耀扫码登录失败：账号响应不是有效 XML")
            return None
        token = result.findtext(".//serviceToken") or ""
        if result.get("resultCode", "0") != "0" or not token:
            # The response may still contain credentials or personal information.
            self.logger.error("荣耀扫码登录失败：账号响应未通过校验")
            return None
        return {"serviceToken": token, "siteId": site, "deviceId": self.device_id, "deviceType": "6",
                "userId": result.findtext(".//userID") or scan.get("userID", ""),
                "nickName": result.findtext(".//nickName") or "",
                # Keep the complete response, not a field allowlist: unknown fields,
                # attributes, repeated elements and future expiry metadata survive.
                # Base64 preserves the exact XML bytes in the private account JSON;
                # it is an encoding, not encryption. Never include this in logs/UI.
                "accountExchangeResponse": {
                    "endpoint": "/AccountServer/IDM/loginByQrCode",
                    "format": "xml",
                    "bodyBase64": base64.b64encode(response.content).decode("ascii"),
                    "httpStatus": response.status_code,
                    "capturedAtUnix": time.time(),
                }}

    def authorization_code(self, game_config):
        token = self.saved.get("serviceToken", "")
        if not token:
            return ""
        package = str(game_config["package_name"])
        client_id = str(game_config["app_id"])
        # 为目标游戏包绑定账号凭据。
        transformed = token if package == ACCOUNT_APP_ID else token[:20] + hashlib.sha256((token + ":" + package).encode()).hexdigest()
        data = self._post(self.saved.get("siteId", "1"), 2, "/oauth2/v3/silent_token", {
            "grant_type": "service_token", "service_token": transformed, "scope": "openid",
            "device_type": self.saved.get("deviceType", "6"), "package_name": package,
            "siteId": str(self.saved.get("siteId", "1")), "device_id": self.device_id,
            "need_code": "true", "app_version": str(game_config.get("app_version", "")), "client_id": client_id,
        }, extra_query={"client_id": client_id},
            game_headers={"PKG": package, "APPID": client_id, "UID": self.saved.get("userId", "")}).json()
        code = data.get("code") or ""
        if not code:
            self.logger.warning("荣耀账号静默取授权码失败: {}", data)
        return code


class HonorAccountBrowser(WebBrowser):
    def __init__(self, on_cancel):
        super().__init__("honor", True)
        self._on_cancel = on_cancel
        self.setWindowTitle("荣耀账号登录")
        self.set_user_agent(
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
        )

    def verify(self, url):
        # 登录结果由同一账号会话的轮询和 ST 兑换确认。
        return False

    def handle_url_change(self, url):
        self.logger.debug("荣耀账号登录页面已跳转")

    def on_load_finished(self, success):
        if not success:
            self.logger.debug("荣耀账号登录页面加载中断")

    def closeEvent(self, event):
        if not getattr(self, "_cleanup_called", False):
            self._on_cancel()
        super().closeEvent(event)


class HonorAccountLogin:
    """由两种浏览器实现共用的账号态获取/恢复；QR 展示复用现有 UI。"""

    def _update_qrcode_cache(self, status, image=""):
        from channelHandler.channelUtils import getShortGameId
        game = getShortGameId(self.account_game_id) if self.account_game_id else "_default"
        cache = genv.get(QRCODE_CACHE_KEY, {})
        data = {"status": status, "qrcode_base64": image,
                "timestamp": int(time.time()), "uuid": ""}
        cache[game] = data
        genv.set(QRCODE_CACHE_KEY, cache)
        if self._on_qrcode is not None:
            self._on_qrcode(data)

    def cancel_qr(self):
        self._qr_cancelled = True

    def _account_login_cancelled(self):
        return self._qr_cancelled or (self._account_cancel_event is not None and self._account_cancel_event.is_set())

    def qrLogin(self):
        self._update_qrcode_cache("loading")
        client = HonorAccountSession(self.accountSession)
        try:
            info = client.get_qr_info()
            buf = io.BytesIO()
            qrcode.make(info["content"]).save(buf, format="PNG")
            self._update_qrcode_cache("ready", base64.b64encode(buf.getvalue()).decode("ascii"))
            return self._wait_for_account(client)
        except Exception:
            self._update_qrcode_cache("failed")
            self.logger.exception("荣耀账号扫码登录失败")
            return False

    def _wait_for_account(self, client):
        deadline = time.time() + 180
        while time.time() < deadline:
            if self._account_login_cancelled():
                self._update_qrcode_cache("cancelled")
                return None
            scan = client.poll()
            if self._account_login_cancelled():
                self._update_qrcode_cache("cancelled")
                return None
            result_code = str(scan.get("resultCode", "0"))
            if result_code == "103000201":
                self._update_qrcode_cache("expired")
                return False
            if result_code == "103000202":
                self._update_qrcode_cache("scanned")
            if result_code not in ("0", "103000200", "103000202"):
                self.logger.error("荣耀扫码确认失败: {}", scan)
                self._update_qrcode_cache("failed")
                return False
            if result_code == "0" and scan.get("userID") and scan.get("code"):
                self._update_qrcode_cache("scanned")
                saved = client.exchange(scan, self.device)
                if self._account_login_cancelled():
                    self._update_qrcode_cache("cancelled")
                    return None
                if not saved:
                    self._update_qrcode_cache("failed")
                    return False
                self.accountSession = saved
                return True
            time.sleep(0.5)
        self._update_qrcode_cache("expired")
        return False

    def _web_account_login(self, on_complete=None):
        client = HonorAccountSession(self.accountSession)

        def show(info):
            if self._account_login_cancelled():
                if on_complete is not None:
                    on_complete(None, False)
                return None, False
            browser = HonorAccountBrowser(self.cancel_qr)
            browser.set_url(info["content"])
            self._active_browser = browser

            def closed(b):
                if self._active_browser is not b:
                    return
                self._active_browser = None
                result = b.result if isinstance(b.result, tuple) else (None, False)
                if result[0] is None:
                    self.cancel_qr()
                QTimer.singleShot(0, lambda: on_complete(*result))

            if on_complete is not None:
                browser._async_completion_callback = closed

            def worker():
                try:
                    success = self._wait_for_account(client)
                    game_ready = success is True and not self._account_login_cancelled() and self.loginWithAccount()
                except Exception:
                    self.logger.exception("荣耀账号网页登录失败")
                    success, game_ready = False, False

                def complete_browser():
                    if self._active_browser is browser:
                        browser.result = (success, game_ready)
                        browser.cleanup()
                app_state.run_on_main_thread(complete_browser)

            try:
                threading.Thread(target=worker, daemon=True).start()
                result = browser.run()
            except Exception:
                self.cancel_qr()
                self._active_browser = None
                browser.cleanup()
                raise
            if on_complete is not None:
                return None
            self._active_browser = None
            if not isinstance(result, tuple):
                self.cancel_qr()
                return None, False
            return result

        if on_complete is not None:
            def prepare():
                try:
                    info = client.get_qr_info()
                except Exception:
                    self.logger.exception("荣耀账号网页登录准备失败")
                    app_state.run_on_main_thread(lambda: on_complete(False, False))
                    return
                def open_browser():
                    try:
                        show(info)
                    except Exception:
                        self.logger.exception("荣耀账号登录页打开失败")
                        on_complete(False, False)
                app_state.run_on_main_thread(open_browser)
            threading.Thread(target=prepare, daemon=True).start()
            return None
        try:
            return show(client.get_qr_info())
        except Exception:
            self.logger.exception("荣耀账号网页登录失败")
            return False, False

    def newOAuthLogin(self, on_complete=None, *, cancelled=None):
        def open_browser():
            if cancelled is not None and cancelled():
                if on_complete is not None:
                    on_complete(None)
                return None
            try:
                return self._openOAuthLogin(on_complete=on_complete)
            except Exception:
                self.logger.error("荣耀 OAuth 登录页打开失败")
                if on_complete is not None:
                    on_complete(False)
                return False
        if on_complete is not None:
            def prepare():
                self.gameConfig()
                app_state.run_on_main_thread(open_browser)
            threading.Thread(target=prepare, daemon=True).start()
            return None
        self.gameConfig()
        return open_browser()

    def loginWithAccount(self):
        if not self.accountSession.get("serviceToken"):
            return False
        self.lastSessionError = None
        self.gameConfig()
        try:
            code = HonorAccountSession(self.accountSession).authorization_code(self.channelConfig)
            return bool(code) and self._game_center_login(code)
        except Exception:
            self.logger.exception("荣耀账号静默登录失败")
            return False

    def canReauthorize(self):
        return not getattr(getattr(self, "lastSessionError", None), "blocks_reauthorization", False)

    def refreshSession(self):
        self.lastSessionError = None
        if self.unionToken and self.configLogin():
            return True
        if not self.canReauthorize():
            return False
        return self.loginWithAccount()

    def newAccountLogin(self, on_complete=None, cancel_event=None, on_qrcode=None, login_method="qr"):
        self._qr_cancelled = False
        self._account_cancel_event = cancel_event
        self._on_qrcode = on_qrcode
        def finish(success, game_ready):
            if self._account_login_cancelled():
                success = None
            if success is not True:
                if on_complete is not None:
                    on_complete(success)
                return success
            if game_ready:
                self._update_qrcode_cache("verified")
                if on_complete is not None:
                    on_complete(True)
                return True
            if not self.canReauthorize():
                self._update_qrcode_cache("failed")
                if on_complete is not None:
                    on_complete(False)
                return False
            # 设备账号登录不等于已授权这个游戏，沿用现有游戏 OAuth 交互。
            def oauth_done(result):
                if self._account_login_cancelled():
                    result = None
                self._update_qrcode_cache("verified" if result is True else "cancelled" if result is None else "failed")
                on_complete(result)
            if on_complete is not None:
                try:
                    return self.newOAuthLogin(on_complete=oauth_done, cancelled=self._account_login_cancelled)
                except Exception as error:
                    oauth_done(error)
                    return None
            result = self.newOAuthLogin(cancelled=self._account_login_cancelled)
            if self._account_login_cancelled():
                result = None
            self._update_qrcode_cache("verified" if result is True else "cancelled" if result is None else "failed")
            return result
        if login_method == "web":
            if on_complete is not None:
                return self._web_account_login(on_complete=finish)
            return finish(*self._web_account_login())
        if on_complete is not None:
            def worker():
                success = self.qrLogin()
                game_ready = success is True and not self._account_login_cancelled() and self.loginWithAccount()
                app_state.run_on_main_thread(lambda: finish(success, game_ready))
            threading.Thread(target=worker, daemon=True).start()
            return None
        success = self.qrLogin()
        return finish(success, success is True and not self._account_login_cancelled() and self.loginWithAccount())
