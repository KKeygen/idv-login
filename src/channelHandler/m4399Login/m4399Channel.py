# coding=UTF-8
"""4399 游戏盒渠道 H5 OAuth 登录与凭证恢复。

网页登录流程：
1. POST /openapiv2/oauth.html -> code=607 + result.login_url
2. Qt WebView 加载 login_url，用户完成 4399 通行证登录
3. 页面自然跳转 /openapi/oauth-callback.html?... 并显示 code=100 JSON
4. loadFinished 后用 QWebEnginePage.toPlainText() 读取 JSON
5. refresh_token 只存在于成功 callback URL query 中，因此单独捕获并持久化

无交互恢复接口：
- GET  /openapiv2/oauth-check.html：验证业务 state
- POST /openapiv2/oauth-getinfobyrefresh.html：用 refresh_token 轮转登录材料
- POST /openapiv2/oauth.html + state=<业务state>&source=4399&refresh=1：第二级恢复

注意：两个刷新接口的成功响应与 callback JSON 同类，但不包含 refresh_token。
因此 refresh_token 绝不能因为刷新响应缺字段而被覆盖为空。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from PyQt6.QtCore import QTimer

from channelHandler.WebLoginUtils import WebBrowser
from logutil import setup_logger
from ssl_utils import should_verify_ssl


OAUTH_API = "https://m.4399api.com/openapiv2/oauth.html"
OAUTH_CHECK_API = "http://m.4399api.com/openapiv2/oauth-check.html"
OAUTH_REFRESH_TOKEN_API = (
    "https://m.4399api.com/openapiv2/oauth-getinfobyrefresh.html"
)
OAUTH_CALLBACK_HOST = "m.4399api.com"
OAUTH_CALLBACK_PATH = "/openapi/oauth-callback.html"

_HTTP_TIMEOUT = 20
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36"
)


def uuid_str() -> str:
    return uuid.uuid4().hex


def _build_udid() -> str:
    """构造 62 位 UDID（时间戳 + md5(uuid) + 校验尾）。"""
    prefix = time.strftime("%Y%m%d%H%M%S") + hashlib.md5(
        uuid_str().encode()
    ).hexdigest() + "00"
    return prefix + hashlib.md5(
        ("cn.m4399.uuid" + prefix).encode()
    ).hexdigest()[:14]


def _is_callback_url(url: str) -> bool:
    """只认真正 callback 文档，避免 authorize 的 redirect_uri 误命中。"""
    try:
        parsed = urlparse(str(url or ""))
        return (
            (parsed.hostname or "").lower() == OAUTH_CALLBACK_HOST
            and parsed.path.rstrip("/") == OAUTH_CALLBACK_PATH.rstrip("/")
        )
    except Exception:
        return False


def _first_query_value(query: Dict[str, List[str]], name: str) -> str:
    values = query.get(name) or []
    return str(values[0] or "") if values else ""


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class M4399Login:
    """4399 OAuth 协议层。网络恢复与 Qt Web 登录共用同一 device identity。"""

    def __init__(
        self,
        game_key: str,
        bid: str,
        canal: str,
        sdk_version: str,
        oauth_device: Optional[Dict[str, Any]] = None,
    ):
        self.logger = setup_logger()
        self.game_key = str(game_key or "")
        self.bid = str(bid or "")
        self.canal = str(canal or "")
        self.sdk_version = str(sdk_version or "")

        self.oauth_device: Dict[str, Any] = self._normalize_device(
            oauth_device or {}
        )

        self._active_browser: Optional[M4399Browser] = None
        self.login_resp: Optional[Dict[str, Any]] = None

        # Web 登录观察结果。
        self.cookies: Dict[str, str] = {}
        self.cookie_records: List[Dict[str, Any]] = []
        self.refresh_token: str = ""
        self.callback_expired_at: Optional[int] = None
        self.callback_expires_in: Optional[int] = None

    # ── Device / request helpers ──────────────────────────────

    def _normalize_device(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        """保留稳定设备身份，同时用当前渠道配置覆盖可变配置字段。"""
        saved = dict(saved) if isinstance(saved, dict) else {}
        return {
            "DEVICE_IDENTIFIER": str(
                saved.get("DEVICE_IDENTIFIER") or "pc-idv-login"
            ),
            "PLATFORM_TYPE": "android",
            "SDK_VERSION": self.sdk_version,
            "GAME_KEY": self.game_key,
            "GAME_VERSION": str(saved.get("GAME_VERSION") or "1.0.286"),
            "GAME_VERSION_CODE": int(saved.get("GAME_VERSION_CODE") or 286),
            "BID": self.bid,
            "RUNTIME": "android",
            "CANAL_IDENTIFIER": self.canal,
            "UDID": str(saved.get("UDID") or _build_udid()),
            "DEBUG": "false",
            "VIP_INFO": str(saved.get("VIP_INFO") or ""),
            "TEAM": int(saved.get("TEAM") or 0),
            "UID": "",
            "SCREEN_RESOLUTION": str(
                saved.get("SCREEN_RESOLUTION") or "1080*1920"
            ),
            "DEVICE_MODEL": str(saved.get("DEVICE_MODEL") or "Pixel 7"),
            "SYSTEM_VERSION": str(saved.get("SYSTEM_VERSION") or "13"),
            "NETWORK_TYPE": str(saved.get("NETWORK_TYPE") or "wifi"),
        }

    def export_oauth_device(self) -> Dict[str, Any]:
        data = dict(self.oauth_device)
        data["UID"] = ""
        return data

    def _device_for_request(self, uid: str = "") -> Dict[str, Any]:
        device = dict(self.oauth_device)
        device.update(
            {
                "SDK_VERSION": self.sdk_version,
                "GAME_KEY": self.game_key,
                "BID": self.bid,
                "CANAL_IDENTIFIER": self.canal,
                "UID": str(uid or ""),
            }
        )
        return device

    @staticmethod
    def _encode_form(device: Dict[str, Any], extra: Dict[str, Any]) -> str:
        # 保持现有 SDK 模拟代码的 body 形态；每个值单独 URL encode。
        parts = [
            "device="
            + requests.utils.quote(
                json.dumps(device, ensure_ascii=False), safe=""
            )
        ]
        for key, value in extra.items():
            parts.append(
                requests.utils.quote(str(key), safe="")
                + "="
                + requests.utils.quote(str(value if value is not None else ""), safe="")
            )
        return "&".join(parts)

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": _USER_AGENT,
        }

    @staticmethod
    def _successful_login_response(data: Any) -> bool:
        return (
            isinstance(data, dict)
            and str(data.get("code", "")) in ("100", "200")
            and isinstance(data.get("result"), dict)
            and bool(data["result"].get("uid"))
            and bool(data["result"].get("state"))
        )

    def _post_form(
        self,
        url: str,
        *,
        uid: str = "",
        extra: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        body = self._encode_form(self._device_for_request(uid), extra)
        try:
            resp = requests.post(
                url,
                data=body,
                headers=self._headers(),
                verify=should_verify_ssl(),
                timeout=_HTTP_TIMEOUT,
            )
            data = resp.json()
            return data if isinstance(data, dict) else None, str(
                data.get("code", "") if isinstance(data, dict) else ""
            )
        except Exception as exc:
            # 不打印请求体；其中可能包含 state/refresh_token。
            self.logger.warning(
                f"4399 credential request failed: endpoint={urlparse(url).path}, "
                f"error={type(exc).__name__}"
            )
            return None, "network_error"

    # ── OAuth bootstrap / recovery APIs ───────────────────────

    def get_login_url(self) -> Optional[str]:
        """每次网页登录都从 oauth.html 获取新的 authorize URL。"""
        data, code = self._post_form(
            OAUTH_API,
            uid="",
            extra={"top_bar": "1"},
        )
        result = data.get("result") if isinstance(data, dict) else None
        if code != "607" or not isinstance(result, dict):
            self.logger.warning(
                "4399 oauth.html 未返回登录页: "
                f"code={code or '?'}"
            )
            return None
        return str(result.get("login_url") or result.get("login_url_backup") or "") or None

    def check_state(self, uid: str, state: str) -> Tuple[Optional[bool], str]:
        """官方 oauth-check。True=有效，False=明确无效，None=无法判断。"""
        if not uid or not state:
            return False, "missing_credential"
        try:
            resp = requests.get(
                OAUTH_CHECK_API,
                params={
                    "state": state,
                    "uid": uid,
                    "key": self.game_key,
                },
                headers={"User-Agent": _USER_AGENT},
                timeout=_HTTP_TIMEOUT,
            )
            data = resp.json()
            code = str(data.get("code", "")) if isinstance(data, dict) else ""
            if code == "200":
                return True, code
            # 601/604/10204/10205 都不能继续信任当前 state；上层会尝试恢复。
            return False, code or f"http_{resp.status_code}"
        except Exception as exc:
            self.logger.warning(
                "4399 oauth-check request failed: "
                f"error={type(exc).__name__}"
            )
            return None, "network_error"

    def refresh_by_token(
        self,
        uid: str,
        refresh_token: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """使用 callback URL 保存的 refresh_token 获取新登录材料。

        成功响应不包含 refresh_token；调用者必须继续保存旧 refresh_token。
        """
        if not uid or not refresh_token:
            return None, "missing_refresh_token"
        data, code = self._post_form(
            OAUTH_REFRESH_TOKEN_API,
            uid=uid,
            extra={
                "refresh_token": refresh_token,
                "source": "4399",
                "cloud_ext": "",
            },
        )
        if self._successful_login_response(data):
            return data, code
        return None, code or "invalid_response"

    def refresh_by_state(
        self,
        state: str,
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """使用业务凭证 state 调 oauth.html?refresh=1 的兼容恢复路径。

        这里的 state 是 callback JSON result.state（业务 token），不是 callback
        302 链内部 URL 上的 OAuth state/hex code。
        """
        if not state:
            return None, "missing_state"
        # 已验证的请求形态中 device.UID 为空，保持该语义。
        data, code = self._post_form(
            OAUTH_API,
            uid="",
            extra={
                "state": state,
                "source": "4399",
                "refresh": "1",
            },
        )
        if self._successful_login_response(data):
            return data, code
        return None, code or "invalid_response"

    # ── Web login ─────────────────────────────────────────────

    def _capture_browser_session(self, browser: "M4399Browser") -> None:
        try:
            self.cookies = browser.export_cookie().copy()
            self.cookie_records = browser.export_cookie_records()
            persistent_count = sum(
                1 for item in self.cookie_records if not item.get("session", True)
            )
            self.logger.debug(
                "[4399-cookie] snapshot: "
                f"simple_count={len(self.cookies)} "
                f"record_count={len(self.cookie_records)} "
                f"persistent={persistent_count} "
                f"session={len(self.cookie_records) - persistent_count}"
            )
        except Exception:
            self.logger.exception("[4399-cookie] 导出浏览器 cookies 失败")

        # refresh_token 只从 callback URL 获取。不要从 JSON response 猜。
        if browser.refresh_token:
            self.refresh_token = browser.refresh_token
        self.callback_expired_at = browser.callback_expired_at
        self.callback_expires_in = browser.callback_expires_in

    def web_login(self, on_complete=None) -> Optional[Dict[str, Any]]:
        """启动 Qt Web 登录；遵循项目现有 WebBrowser/Bilibili/Vivo 契约。"""
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
            self._active_browser = browser
            if on_complete is not None:

                def _on_async_done(done_browser):
                    self._active_browser = None
                    try:
                        self._capture_browser_session(done_browser)
                        result = done_browser.result
                        if isinstance(result, dict) and isinstance(
                            result.get("result"), dict
                        ):
                            self.login_resp = result
                            on_complete(result)
                        else:
                            on_complete(None)
                    except Exception:
                        self.logger.exception("4399 异步登录回调失败")
                        on_complete(None)

                browser._async_completion_callback = _on_async_done
            return None

        self._capture_browser_session(browser)
        if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
            self.login_resp = resp
            return resp
        return None


class M4399Browser(WebBrowser):
    """自然加载 callback JSON，并捕获 RT/cookie；不二次请求 callback URL。"""

    def __init__(self, login_url: str):
        # WebBrowser.__init__ 会先连接 self.cookie_added。
        self._cookie_records: Dict[str, Dict[str, Any]] = {}
        self._callback_read_pending = False
        self._captured: Optional[Dict[str, Any]] = None
        self._login_url = login_url

        self.refresh_token: str = ""
        self.callback_expired_at: Optional[int] = None
        self.callback_expires_in: Optional[int] = None

        super().__init__("m4399", True)
        self.logger = setup_logger()
        self.resize(430, 680)

        try:
            self.profile.cookieStore().loadAllCookies()
        except Exception:
            self.logger.exception("[4399-cookie] 加载 profile 既有 cookies 失败")

    # ── Privacy-safe navigation logs ──────────────────────────

    def handle_url_change(self, url):
        """覆盖基类，避免 callback query 中的 token 被截入 debug 日志。"""
        try:
            parsed = urlparse(url.toString())
            self.logger.debug(
                "[4399-web] URL changed: "
                f"scheme={parsed.scheme} host={parsed.hostname or ''} path={parsed.path}"
            )
        except Exception:
            self.logger.debug("[4399-web] URL changed")
        # 4399 不依赖 URL-based parseReslt；结果只在 loadFinished 后读正文。

    # ── Cookie observation ────────────────────────────────────

    @staticmethod
    def _cookie_text(value) -> str:
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            try:
                return value.data().decode("utf-8", errors="replace")
            except Exception:
                return str(value)

    def cookie_added(self, cookie):
        """保存 cookie 值；debug 仅记录元数据，不输出 value。"""
        try:
            name = self._cookie_text(cookie.name())
            value = self._cookie_text(cookie.value())
            domain = str(cookie.domain() or "")
            path = str(cookie.path() or "/")
            is_session = bool(cookie.isSessionCookie())
            is_secure = bool(cookie.isSecure())
            is_http_only = bool(cookie.isHttpOnly())

            expires_epoch: Optional[int] = None
            expires_iso: Optional[str] = None
            remaining_seconds: Optional[int] = None
            if not is_session:
                expiry = cookie.expirationDate()
                if expiry.isValid():
                    expires_epoch = int(expiry.toSecsSinceEpoch())
                    expires_iso = expiry.toUTC().toString(
                        "yyyy-MM-ddTHH:mm:ss'Z'"
                    )
                    remaining_seconds = expires_epoch - int(time.time())

            same_site = "unknown"
            try:
                policy = cookie.sameSitePolicy()
                same_site = getattr(policy, "name", str(policy))
            except Exception:
                pass

            self.cookies[name] = value
            key = f"{domain}\n{path}\n{name}"
            self._cookie_records[key] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "session": is_session,
                "expires_epoch": expires_epoch,
                "expires_iso": expires_iso,
                "secure": is_secure,
                "http_only": is_http_only,
                "same_site": same_site,
            }

            kind = "session" if is_session else "persistent"
            remaining = (
                "session"
                if remaining_seconds is None
                else f"{remaining_seconds}s/{remaining_seconds / 86400:.2f}d"
            )
            self.logger.debug(
                "[4399-cookie] "
                f"name={name!r} domain={domain!r} path={path!r} "
                f"type={kind} expires={expires_iso or '-'} remaining={remaining} "
                f"secure={is_secure} httpOnly={is_http_only} "
                f"sameSite={same_site} value_len={len(value)}"
            )
        except Exception:
            self.logger.exception("[4399-cookie] 解析 cookie 元数据失败")

    def export_cookie_records(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._cookie_records.values()]

    # ── Callback ──────────────────────────────────────────────

    def verify(self, url: str) -> bool:
        return False

    def parseReslt(self, url: str):
        return False

    def _capture_callback_query(self, current_url: str) -> None:
        """只提取恢复所需 RT 与寿命元数据，不保存/打印完整 callback URL。"""
        try:
            query = parse_qs(urlparse(current_url).query, keep_blank_values=True)
            refresh_token = _first_query_value(query, "refresh_token")
            if refresh_token:
                self.refresh_token = refresh_token
            self.callback_expired_at = _int_or_none(
                _first_query_value(query, "expired_at")
            )
            self.callback_expires_in = _int_or_none(
                _first_query_value(query, "expires_in")
            )
            self.logger.debug(
                "[4399-oauth] callback query captured: "
                f"refresh_token_present={bool(refresh_token)} "
                f"expired_at_present={self.callback_expired_at is not None} "
                f"expires_in={self.callback_expires_in or '-'}"
            )
        except Exception:
            self.logger.exception("4399 callback query 元数据解析失败")

    def on_load_finished(self, success: bool):
        # 不调用基类：基类失败日志会打印完整 URL，callback URL 含敏感 query。
        if self.page is None:
            return

        current_url = self.page.url().toString()
        if not success:
            try:
                parsed = urlparse(current_url)
                self.logger.warning(
                    "[4399-web] 页面加载失败: "
                    f"host={parsed.hostname or ''} path={parsed.path}"
                )
            except Exception:
                self.logger.warning("[4399-web] 页面加载失败")
            return

        if self._captured is not None or not _is_callback_url(current_url):
            return
        if self._callback_read_pending:
            return

        self._capture_callback_query(current_url)
        self._callback_read_pending = True
        self.logger.debug("4399 OAuth callback 页面已加载，读取页面 JSON")
        self.page.toPlainText(self._consume_callback_text)

    def _consume_callback_text(self, text: str):
        self._callback_read_pending = False
        raw = str(text or "").strip()
        try:
            data = json.loads(raw)
        except Exception:
            self.logger.warning(
                f"4399 oauth-callback 页面不是有效 JSON: body_len={len(raw)}"
            )
            return

        code = str(data.get("code", ""))
        result = data.get("result")
        if code in ("100", "200") and isinstance(result, dict):
            self._captured = data
            self.result = data
            self.logger.info(
                "已捕获 4399 OAuth 登录结果: "
                f"code={code}, uid_present={bool(result.get('uid'))}, "
                f"state_present={bool(result.get('state'))}, "
                f"access_token_present={bool(result.get('access_token'))}, "
                f"refresh_token_present={bool(self.refresh_token)}"
            )
            QTimer.singleShot(0, self.cleanup)
            return

        self.logger.warning(
            "4399 oauth-callback 未返回成功: "
            f"code={code or '?'}"
        )

    def cleanup(self):
        super().cleanup()
