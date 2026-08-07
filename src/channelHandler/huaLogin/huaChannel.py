# coding=UTF-8
"""华为渠道登录（两种模式，对齐 bilibili）。

- 扫码登录（qr）：web UI 展示二维码，手机扫码确认；阻塞式，后台线程调用。
- 网页登录（web）：内嵌浏览器打开华为登录页，URL 变为 loginSuccess.html 即视为
  扫码成功，随后轮询一次取结果换 ST。

共同流程：getqrInfo → 确认 → loginByQrCode 换 ST → silent_token（按游戏包名变换）
签发 access_token → getGameAuthSign → gameAuthSign（游戏登录会话）。
"""
import base64
import io
import json
import os
import random
import re
import string
import time
from urllib.parse import urlparse

import requests
from faker import Faker

from envmgr import genv
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.WebLoginUtils import WebBrowser
from channelHandler.huaLogin.consts import DEVICE, COMMON_PARAMS

DEVICE_RECORD = "huawei_device.json"
QRCODE_CACHE_KEY = "HUAWEI_QRCODE_CACHE"

# ── HMS 扫码登录 HTTP 端点 ─────────────────────────────────

QR_HOST = "https://id1.cloud.huawei.com"
AS_HOST = "https://setting1.hicloud.com"
APP_ID = "com.huawei.hwid"
VER = "53000"
UA = "com.huawei.hwid/5.3.0.312 (Linux; Android 12; SM-G9910) RestClient/5.3.0.312"


def _ctr_id() -> str:
    return str(int(time.time() * 1000)) + "".join(random.choices("0123456789", k=19))


def render_qr_base64(content: str) -> str:
    """将文本渲染为 QR 码 PNG，返回 base64 字符串（不含 data: 前缀）。"""
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class HwQrSession:
    """维护华为扫码登录的会话 cookie（JSESSIONID 等）。"""

    def __init__(self):
        self.logger = setup_logger()
        self.session = requests.Session()
        self._last_qr_code = ""

    def post(self, url: str, body: str, ctype: str):
        headers = {
            "Connection": "Keep-Alive",
            "Content-Type": ctype,
            "Authorization": str(int(time.time() * 1000)),
            "User-Agent": UA,
        }
        full = url + "&ctrID=" + _ctr_id()
        r = self.session.post(
            full,
            data=body.encode("utf-8") if isinstance(body, str) else body,
            headers=headers,
            timeout=30,
            verify=should_verify_ssl(),
        )
        return r.status_code, r.text

    def get_qr_info(self):
        """取二维码信息。

        Returns:
            dict: {content, qrCode, qrToken, sessionID, expiredTime, ...}
            失败返回 None。
        """
        url = (
            f"{QR_HOST}/DimensionalCode/getqrInfo?Version={VER}"
            f"&cVersion=1&blackScreen=0&appBrand=HUAWEI"
        )
        body = (
            "version=53000&appID=com.huawei.hwid&loginChannel=7000700"
            "&reqClientType=701&confirmFlag=1&lang=zh_CN"
        )
        try:
            st, content = self.post(url, body, "application/x-www-form-urlencoded; charset=UTF-8")
            if st != 200:
                self.logger.error(f"getqrInfo HTTP {st}: {content[:300]}")
                return None
            data = json.loads(content)
            self._last_qr_code = str(data.get("qrCode", ""))
            return data
        except Exception as e:
            self.logger.error(f"getqrInfo 请求异常: {e}")
            return None

    def poll(self, qr_token: str):
        """单次轮询扫码状态。

        Returns:
            dict | None: 解析后的响应体；已确认时含 userID/userAccount/code。
            未确认或解析失败返回 None。
        """
        url = (
            f"{QR_HOST}/DimensionalCode/async?Version={VER}"
            f"&cVersion=1&blackScreen=0&appBrand=HUAWEI"
        )
        try:
            st, content = self.post(url, f"qrToken={qr_token}", "application/x-www-form-urlencoded; charset=UTF-8")
            if st != 200:
                self.logger.warning(f"poll HTTP {st}: {content[:300]}")
                return None
            return json.loads(content)
        except json.JSONDecodeError:
            self.logger.warning(f"poll 响应解析失败: {content[:300]}")
            return None
        except Exception:
            return None

    def login_by_qrcode(self, scan_result: dict, device_uuid: str):
        """用扫码结果换取 ST。

        Args:
            scan_result: poll() 返回的已确认结果。
            device_uuid: 设备 uuid（同时用于 deviceID 与 silent_token 的 device_id）。

        Returns:
            tuple[str, str]: (serviceToken, 原始 XML 响应)；失败时 serviceToken 为空。
        """
        uid = str(scan_result.get("userID") or "")
        account = str(scan_result.get("userAccount") or "")
        code = str(scan_result.get("code") or "")
        site = str(scan_result.get("siteID") or "1")
        acct_type = str(scan_result.get("accountType") or "0")
        qr_code = self._last_qr_code

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<LoginByQrCodeReq>"
            f"<version>{VER}</version>"
            f"<accountType>{acct_type}</accountType>"
            f"<userAccount>{account}</userAccount>"
            f"<userID>{uid}</userID>"
            f"<qrCode>{qr_code}</qrCode>"
            f"<qrSiteID>{site}</qrSiteID>"
            f"<code>{code}</code>"
            f"<appID>{APP_ID}</appID>"
            "<reqClientType>7</reqClientType>"
            "<loginChannel>7000000</loginChannel>"
            "<osVersion>12</osVersion>"
            "<plmn></plmn>"
            f"<uuid>{device_uuid}</uuid>"
            "<languageCode>zh_CN</languageCode>"
            "<deviceSecure>0</deviceSecure>"
            "<mainAcctLogin>1</mainAcctLogin>"
            "<clientID></clientID>"
            "<riskToken></riskToken>"
            "<deviceInfo>"
            f"<deviceID>{device_uuid}</deviceID>"
            "<deviceType>6</deviceType>"
            "<terminalType>Android</terminalType>"
            "</deviceInfo>"
            "<loginType>1</loginType>"
            "</LoginByQrCodeReq>"
        )
        url = (
            f"{AS_HOST}/AccountServer/IDM/loginByQrCode?Version={VER}"
            f"&cVersion=1&blackScreen=0&appBrand=HUAWEI"
        )
        try:
            st, content = self.post(url, xml, "text/html; charset=UTF-8")
            if st != 200:
                self.logger.error(f"loginByQrCode HTTP {st}: {content[:300]}")
                return "", content
            m = re.search(r"<serviceToken>([^<]+)</serviceToken>", content)
            if not m:
                self.logger.error(f"loginByQrCode 未返回 serviceToken: {content[:300]}")
                return "", content
            return m.group(1), content
        except Exception as e:
            self.logger.error(f"loginByQrCode 请求异常: {e}")
            return "", ""


def transform_service_token(st: str, package_name: str) -> str:
    """ST 变换：前20字符 + SHA256(ST + ":" + 游戏包名)。"""
    import hashlib

    if not st:
        return ""
    digest = hashlib.sha256((st + ":" + package_name).encode("utf-8")).hexdigest()
    return st[:20] + digest


def silent_token(st: str, package_name: str, client_id: str, device_id: str):
    """用 ST 为指定游戏包名签发 access_token/refresh_token。

    Returns:
        dict: silent_token 响应；成功含 access_token/refresh_token/open_id。
    """
    from urllib.parse import urlencode

    transformed = transform_service_token(st, package_name)
    q = urlencode({"client_id": client_id, "sdkVersion": "61400300"})
    url = f"https://oauth-login.platform.hicloud.com/oauth2/v3/silent_token?{q}"
    body = urlencode({
        "grant_type": "service_token",
        "service_token": transformed,
        "scope": "openid",
        "device_type": "6",
        "package_name": package_name,
        "siteId": "1",
        "device_id": device_id,
        "need_code": "true",
        "client_id": client_id,
    })
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Connection": "Keep-Alive",
        "Authorization": str(int(time.time() * 1000)),
        "User-Agent": UA,
    }
    try:
        r = requests.post(
            url + "&ctrID=" + _ctr_id(),
            data=body.encode("utf-8"),
            headers=headers,
            timeout=30,
            verify=should_verify_ssl(),
        )
        return r.json()
    except Exception as e:
        setup_logger().error(f"silent_token 请求异常: {e}")
        return {}


class HuaweiLoginBrowser(WebBrowser):
    """网页登录：打开华为登录页；URL 跳转到 loginSuccess.html 视为成功。"""

    def __init__(self, login_url: str):
        super().__init__("huawei", True)
        self.logger = setup_logger()
        self.setWindowTitle("华为账号登录")
        self._login_url = login_url

    def verify(self, url: str) -> bool:
        return urlparse(url).path.endswith("/CAS/mobile/loginSuccess.html")

    def parseReslt(self, url: str) -> bool:
        print(url)
        self.result = url
        return True


class HuaweiLogin:
    """华为登录：qr 扫码 / web 浏览器两种模式 → ST → 游戏 AT → gameAuthSign。"""

    def __init__(self, channelConfig, serviceToken=None, real_game_id=None):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.channelConfig = channelConfig
        self.serviceToken = serviceToken
        self.accessToken = None
        self.expiredTime = 0
        self._at_game = ""  # accessToken 对应的短 game_id
        self.real_game_id = real_game_id
        self._qr_cancelled = False
        self._active_browser: HuaweiLoginBrowser = None
        self.device = self._load_or_create_device()
        self._ensure_device_uuid()

    # ── 设备 ────────────────────────────────────────────────

    def _load_or_create_device(self):
        if os.path.exists(DEVICE_RECORD):
            with open(DEVICE_RECORD, "r", encoding="utf-8") as f:
                return json.load(f)
        device = self.makeFakeDevice()
        from secure_write import write_json_restricted
        write_json_restricted(DEVICE_RECORD, device)
        return device

    def _ensure_device_uuid(self):
        if not str(self.device.get("device_uuid", "") or "").strip():
            import uuid as uuid_mod
            self.device["device_uuid"] = str(uuid_mod.uuid4())
            from secure_write import write_json_restricted
            write_json_restricted(DEVICE_RECORD, self.device)

    def _device_uuid(self) -> str:
        return str(self.device.get("device_uuid", "") or "").strip()

    def _device_id(self) -> str:
        return self._device_uuid().replace("-", "")[:16]

    def makeFakeDevice(self):
        fake = Faker()
        device = DEVICE.copy()
        manufacturers = ["Samsung", "Huawei", "Xiaomi", "OPPO"]
        import uuid as uuid_mod
        device["deviceId"] = "".join(random.choice("abcdef" + string.digits) for _ in range(64))
        device["device_uuid"] = str(uuid_mod.uuid4())
        device["brand"] = random.choice(manufacturers)
        device["romVersion"] = fake.lexify(
            text="V???IR release-keys", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        device["androidVersion"] = random.choice(["12", "13", "11"])
        device["manufacturer"] = device["brand"]
        device["phoneType"] = fake.lexify(
            text="SM-????", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        )
        return device

    # ── 二维码状态缓存（web UI 轮询展示，扫码模式用） ───────

    def _update_qrcode_cache(self, status, qrcode_base64="", uuid=""):
        cache = genv.get(QRCODE_CACHE_KEY, {})
        if not isinstance(cache, dict):
            cache = {}
        cache[self.real_game_id if self.real_game_id else "_default"] = {
            "status": status,
            "qrcode_base64": qrcode_base64,
            "uuid": uuid,
            "timestamp": int(time.time()),
        }
        genv.set(QRCODE_CACHE_KEY, cache)

    def cancel_qr(self):
        """取消正在进行的扫码轮询（web UI 关闭弹窗时调用）。"""
        self._qr_cancelled = True

    # ── 扫码登录（qr：web UI 展示二维码，阻塞轮询） ─────────

    def qrLogin(self):
        """扫码登录主流程（阻塞，直到扫码完成/超时/取消）。

        由调用方（manual_import）在后台线程执行。

        Returns:
            bool: 是否成功获取 ST。
        """
        self._qr_cancelled = False
        self._update_qrcode_cache("loading")

        qr_session = HwQrSession()
        qr_info = qr_session.get_qr_info()
        if not qr_info:
            self.logger.error("获取华为登录二维码失败")
            self._update_qrcode_cache("failed")
            return False

        qr_b64 = render_qr_base64(str(qr_info.get("content", "")))
        self._update_qrcode_cache("ready", qrcode_base64=qr_b64, uuid=self._device_uuid())

        scan = self._poll_scan(qr_session, qr_info)
        if scan is None:
            if self._qr_cancelled:
                self.logger.info("华为扫码登录已取消")
                self._update_qrcode_cache("cancelled")
            else:
                self.logger.warning("华为扫码登录超时")
                self._update_qrcode_cache("expired")
            return False

        self._update_qrcode_cache("scanned")
        st, _resp = qr_session.login_by_qrcode(scan, self._device_uuid())
        if not st:
            self.logger.error("华为扫码换 ST 失败")
            self._update_qrcode_cache("failed")
            return False

        self.serviceToken = st
        self.logger.info("华为扫码登录成功，已获取 ST")
        self._update_qrcode_cache("verified")
        return True

    def _poll_scan(self, qr_session, qr_info):
        qr_token = str(qr_info.get("qrToken", ""))
        try:
            expired = int(qr_info.get("expiredTime", 0))
        except (TypeError, ValueError):
            expired = 0
        deadline = (expired / 1000.0 - 5) if expired else time.time() + 180
        while time.time() < deadline:
            if self._qr_cancelled:
                return None
            r = qr_session.poll(qr_token)
            if isinstance(r, dict) and r.get("userID"):
                return r
            time.sleep(0.5)
        return None

    # ── 网页登录（web：内嵌浏览器打开登录页） ───────────────

    def webLogin(self, on_complete=None):
        """网页登录：getqrInfo → 浏览器打开登录页 → loginSuccess → 轮询换 ST。"""
        qr_session = HwQrSession()
        qr_info = qr_session.get_qr_info()
        if not qr_info:
            self.logger.error("获取华为登录二维码信息失败")
            if on_complete is not None:
                on_complete(False)
            return False
        print(qr_info)
        login_url = str(qr_info.get("content", ""))
        qr_token = str(qr_info.get("qrToken", ""))
        browser = HuaweiLoginBrowser(login_url)
        browser.set_url(login_url)
        self._active_browser = browser
        resp = browser.run()

        if resp is None:
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None
                    try:
                        self._exchange_st(qr_session, qr_token)
                        success = self.serviceToken is not None
                    except Exception:
                        self.logger.exception("华为异步登录回调失败")
                        success = False
                    on_complete(success)
                browser._async_completion_callback = _on_async_done
            return None

        # 同步模式：run() 已阻塞至登录完成
        self._exchange_st(qr_session, qr_token)
        return self.serviceToken is not None

    def _exchange_st(self, qr_session, qr_token):
        """URL 已跳转 loginSuccess，poll（最多5次）驱动状态机取结果并换 ST。"""
        r = None
        for _ in range(5):
            r = qr_session.poll(qr_token)
            if isinstance(r, dict) and r.get("userID"):
                break
        if not (isinstance(r, dict) and r.get("userID")):
            self.logger.error(f"登录成功后轮询未取到扫码结果: {r}")
            return
        st, _resp = qr_session.login_by_qrcode(r, self._device_uuid())
        if not st:
            self.logger.error("华为登录换 ST 失败")
            return
        self.serviceToken = st
        self.logger.info("华为登录成功，已获取 ST")

    # ── 游戏 token / 账号数据 ───────────────────────────────

    def ensure_game_token(self, game_cfg, short_game_id):
        """为指定游戏保证有效 accessToken（ST → silent_token）。

        Returns:
            bool: 是否成功。
        """
        now = int(time.time())
        if (
            self.accessToken
            and self._at_game == short_game_id
            and now < self.expiredTime - 60
        ):
            return True
        if not self.serviceToken:
            self.logger.warning("华为缺少 ST，无法签发游戏 token")
            return False
        package_name = str(game_cfg.get("package_name") or "").strip()
        client_id = str(game_cfg.get("app_id") or "").strip()
        if not package_name or not client_id:
            self.logger.error(f"华为渠道配置缺失 package_name/app_id: {game_cfg}")
            return False
        resp = silent_token(self.serviceToken, package_name, client_id, self._device_id())
        if not isinstance(resp, dict) or "access_token" not in resp:
            self.logger.error(f"silent_token 签发失败: {resp}")
            self.accessToken = None
            return False
        self.accessToken = resp["access_token"]
        try:
            self.expiredTime = now + int(resp.get("expire_in", 3600))
        except (TypeError, ValueError):
            self.expiredTime = now + 3600
        self._at_game = short_game_id
        self.logger.info(f"华为 silent_token 签发成功 (game={short_game_id})")
        return True

    def initAccountData(self, game_cfg):
        """用 accessToken 获取账号数据（getGameAuthSign）。

        调用者需先 ensure_game_token() 保证 accessToken 有效。
        """
        if not self.accessToken:
            return None
        url = "https://jgw-drcn.jos.dbankcloud.cn/gameservice/api/gbClientApi"
        headers = {
            "User-Agent": f"com.huawei.hms.game/6.14.0.300 (Linux; Android 12; {self.device.get('phoneType')}) RestClient/7.0.6.300",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = COMMON_PARAMS.copy()
        body.update({k: v for k, v in self.device.items() if k != "device_uuid"})
        body["method"] = "client.hms.gs.getGameAuthSign"
        body["extraBody"] = f'json={{"appId":"{game_cfg.get("app_id")}"}}'
        body["accessToken"] = self.accessToken
        try:
            r = requests.post(url, headers=headers, data=body, verify=should_verify_ssl())
            return r.json()
        except Exception as e:
            self.logger.error(f"getGameAuthSign 请求异常: {e}")
            return None

    def is_token_expired(self) -> bool:
        if self.accessToken is None:
            return True
        return int(time.time()) >= self.expiredTime
