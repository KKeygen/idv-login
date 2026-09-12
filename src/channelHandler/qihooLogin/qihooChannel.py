# coding=UTF-8
"""360（奇虎）登录实现。

登录流程：
  1. 在 https://i.360.cn/login/wap 完成网页登录，取得 Q / T cookie
  2. 携带该 cookie 调用 CommonAccount.getUserInfo，换取 access_token
  3. 将 qid + access_token 交给网易 uni_sauth

响应结构（实测）：
    {"errno":"0","errmsg":"OK","consume":39,
     "data":{"qid":"...","username":"...",
             "qoauth_token":{"access_token":"...","expires_in":"35210"}}}
"""
from __future__ import annotations

import json
import urllib.parse

import requests

from logutil import setup_logger
from ssl_utils import should_verify_ssl

from channelHandler.WebLoginUtils import WebBrowser
from channelHandler.qihooLogin import consts as C
from channelHandler.qihooLogin.crypto import (
    build_from,
    build_qt_cookie,
    build_query,
    des_decrypt,
    has_qt,
    md5,
)

TAG = "[360]"

# ── 默认渠道凭证（第五人格，来自 assets/360_assistant_data，已与 manifest 交叉验证） ──
DEFAULT_APP_ID = 203840726
DEFAULT_APP_KEY = "838559dca512625db0deadb1a974d5e6"

# 设备参数默认值
DEFAULT_DEVICE_ID = "a1b2c3d4e5f60718"
DEFAULT_BRAND = "Xiaomi"
DEFAULT_MODEL = "M2102K1AC"
DEFAULT_OS_VERSION = "12"


class QihooBrowser(WebBrowser):
    """360 网页登录。

    与华为/OPPO 一致：移动端登录页 → 按手机比例开窗 + 手机 UA。
    成功判定：URL 跳到 i.360.cn/index/wap（个人中心首页）。
    """

    def __init__(self):
        super().__init__("360_assistant", True, frameless=True)
        self.logger = setup_logger()
        self.setWindowTitle("360账号登录")
        self.set_user_agent(C.MOBILE_USER_AGENT)
        self.qt_cookie: str = ""

    # ── 成功判定 ────────────────────────────────────────────
    def verify(self, url: str) -> bool:
        text = str(url or "")
        if not text:
            return False
        if text.startswith(C.SUCCESS_URL_PREFIX) or "i.360.cn/index/wap" in text:
            return self.parseReslt(text)
        return False

    def parseReslt(self, url: str) -> bool:
        cookie = self._pick_qt_cookie()
        if not cookie:
            self.logger.debug(f"{TAG} 登录已跳转，未取到 Q/T cookie，等待下一次跳转")
            return False
        self.qt_cookie = cookie
        self.result = {"code": 0, "data": {"qt_cookie": cookie, "redirect_url": url}}
        self.logger.debug(f"{TAG} 网页登录成功，已取得 Q/T cookie")
        return True

    # ── cookie 收集 ─────────────────────────────────────────
    def _pick_qt_cookie(self) -> str:
        """从已收集的 cookie 中拼出 Q/T。"""
        q = (self.cookies or {}).get(C.COOKIE_Q, "")
        t = (self.cookies or {}).get(C.COOKIE_T, "")
        if not q or not t:
            return ""
        if q.startswith("Q="):
            q = q[2:]
        cookie = build_qt_cookie(q, t)
        return cookie if has_qt(cookie) else ""


class QihooLogin:
    """360 登录协议封装。"""

    def __init__(self, app_id: int = DEFAULT_APP_ID, app_key: str = DEFAULT_APP_KEY):
        self.logger = setup_logger()
        self.app_id = app_id
        self.app_key = app_key
        self.from_param = build_from(self.app_id)
        self.qt_cookie: str = ""
        self.user_info: dict = {}
        self._active_browser: QihooBrowser = None

    # ── 设备参数 ────────────────────────────────────────────
    def _device_params(self, device_id: str = "") -> dict:
        """公共设备参数。mid / m2 均为 MD5(设备标识)，可任意构造。"""
        dev = md5(device_id or DEFAULT_DEVICE_ID)
        return {
            "v": C.SDK_V,
            "mid": dev,
            "mgsdk_channel": "default",
            "mgsdk_login_type": str(C.LOGIN_METHOD_PASSWORD),
            "m2": dev,
            "oaid": "",
            "qdas_m2": "",
            "brand": DEFAULT_BRAND,
            "model": DEFAULT_MODEL,
            "os_version": DEFAULT_OS_VERSION,
        }

    # ── 请求 ────────────────────────────────────────────────
    def _call(self, params: dict, cookie: str = "", timeout: int = 20) -> dict:
        """发一次 360 passport 请求，返回归一化结果。"""
        method = params.get("method")
        url, _sign_src, _query = build_query(params, self.from_param)
        headers = {"User-Agent": C.MOBILE_USER_AGENT}
        if cookie:
            headers["Cookie"] = cookie

        try:
            resp = requests.get(url, timeout=timeout, headers=headers,
                                verify=should_verify_ssl())
        except Exception as exc:
            self.logger.debug(f"{TAG} 请求异常 method={method}: {exc}")
            return {"ok": False, "errno": None, "errmsg": str(exc), "data": {}}

        body = resp.text or ""

        # 响应为 DES 加密的 JSON
        payload = None
        for cand in (body.strip(), urllib.parse.unquote(body.strip())):
            if not cand:
                continue
            try:
                text = des_decrypt(cand)
            except Exception:
                continue
            if text.lstrip().startswith("{"):
                try:
                    payload = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue

        if payload is None:
            self.logger.debug(f"{TAG} 响应无法解析 method={method}: {body[:200]!r}")
            return {"ok": False, "errno": None, "errmsg": "响应无法解析", "data": {}}

        errno = payload.get(C.KEY_ERRNO)
        if errno is None:
            content = payload.get("content")
            if isinstance(content, dict):
                errno = content.get(C.KEY_ERRNO)
        try:
            errno_int = int(errno)
        except (TypeError, ValueError):
            errno_int = None

        data = payload.get(C.KEY_DATA)
        if not isinstance(data, dict):
            content = payload.get("content")
            if isinstance(content, dict):
                data = content.get(C.KEY_DATA) or content.get("user")
        if not isinstance(data, dict):
            data = {}

        if errno_int != C.ERRNO_OK:
            self.logger.warning(
                f"{TAG} 登录接口返回错误: errno={errno_int} "
                f"msg={payload.get(C.KEY_ERRMSG, '')}"
            )

        return {
            "ok": errno_int == C.ERRNO_OK,
            "errno": errno_int,
            "errmsg": payload.get(C.KEY_ERRMSG, ""),
            "data": data,
        }

    # ── QT cookie → token ───────────────────────────────────
    def login_by_qt(self, qt_cookie: str, device_id: str = "") -> dict:
        """携带 Q/T cookie 调用 getUserInfo，换取 access_token（免密码）。"""
        cookie = str(qt_cookie or "").strip()
        if not has_qt(cookie):
            self.logger.debug(f"{TAG} 缺少 Q/T cookie，放弃登录")
            return {"ok": False, "errno": None, "errmsg": "缺少 Q/T cookie", "data": {}}

        params = {
            "method": C.METHOD_USER_INFO,
            "des": "0",
            "ckey": self.app_key,
        }
        params.update(self._device_params(device_id))
        result = self._call(params, cookie=cookie)
        if result["ok"]:
            self.qt_cookie = cookie
            self.user_info = result["data"]
            self.logger.debug(f"{TAG} 已换取 access_token: qid={result['data'].get('qid')}")
        return result

    # ── 从 user_info 提取给网易的凭据 ───────────────────────
    @staticmethod
    def extract_credentials(user_info: dict) -> dict:
        """从 data 中取出 qid / access_token / expires_in。"""
        data = user_info if isinstance(user_info, dict) else {}
        qoauth = data.get(C.KEY_QOAUTH_TOKEN)
        if not isinstance(qoauth, dict):
            qoauth = {}
        return {
            "qid": str(data.get(C.KEY_QID) or ""),
            "access_token": str(qoauth.get(C.KEY_ACCESS_TOKEN) or ""),
            "expires_in": qoauth.get(C.KEY_EXPIRES_IN),
            "username": str(data.get("username") or ""),
        }

    # ── Web 登录（主入口） ──────────────────────────────────
    def web_login(self, qt_cookie: str = "", on_complete=None):
        """优先用已有 cookie 验证；失败则拉起浏览器登录。

        on_complete 非空时走异步（浏览器显示后立即返回，登录完成回调）。
        """
        if qt_cookie and has_qt(qt_cookie):
            result = self.login_by_qt(qt_cookie)
            if result["ok"]:
                if on_complete is not None:
                    on_complete(result)
                    return None
                return result
            self.logger.warning(f"{TAG} 登录状态已失效，重新打开登录窗口")
            self.logger.debug(
                f"{TAG} cookie 复验失败 errno={result.get('errno')}"
            )

        browser = QihooBrowser()
        browser.set_url(C.LOGIN_URL)
        resp = browser.run()

        if resp is None:
            # 异步模式：保持强引用，避免 profile 被提前释放
            self._active_browser = browser
            if on_complete is not None:

                def _on_done(browser_ref):
                    self._active_browser = None
                    try:
                        cookie = getattr(browser_ref, "qt_cookie", "") or ""
                        if not cookie:
                            self.logger.warning(f"{TAG} 登录未完成，请重试")
                            on_complete(None)
                            return
                        result = self.login_by_qt(cookie)
                        on_complete(result if result["ok"] else None)
                    except Exception:
                        self.logger.exception(f"{TAG} 异步登录处理失败")
                        on_complete(None)

                browser._async_completion_callback = _on_done
            return None

        cookie = getattr(browser, "qt_cookie", "") or ""
        if not cookie:
            self.logger.warning(f"{TAG} 登录未完成，请重试")
            if on_complete is not None:
                on_complete(None)
                return None
            return None

        result = self.login_by_qt(cookie)
        if on_complete is not None:
            on_complete(result if result["ok"] else None)
            return None
        return result
