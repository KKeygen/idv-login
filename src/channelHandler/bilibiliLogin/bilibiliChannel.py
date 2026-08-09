"""Bilibili Game SDK 登录（二维码 + 网页 OTP/账密）。

登录方式：
  1. 二维码登录（默认）：调用 biligame API 生成二维码 → 用户扫码 → 轮询获取 token
  2. 网页登录（备选）：QWebEngine 打开 sdk.biligame.com/login/ → 用户 OTP 或账密登录 → 拦截响应
"""

import base64
import hashlib
import io
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from PyQt6.QtCore import QTimer

from channelHandler.WebLoginUtils import WebBrowser
from logutil import setup_logger
from ssl_utils import should_verify_ssl


# ── Console 前缀 ──────────────────────────────────────────────
BILI_NET_PREFIX = "__BILI_NET__::"
BILI_LOGIN_PREFIX = "__BILI_LOGIN__::"

# ── 登录页 URL 模板 ──────────────────────────────────────────
# 注意：不传 sdk_ver——B站登录页的 Axios 拦截器会把 URL 里的 sdk_ver
# 自动加到所有 API 请求参数里参与签名，但服务端对含 sdk_ver 的 sign
# 验证会报错。去掉 sdk_ver 后 sign 只含 game_id + timestamp，服务端
# 验证正常通过。
_LOGIN_URL_TEMPLATE = (
    "https://sdk.biligame.com/login/"
    "?cef=true&gameId={game_id}&appKey={app_key}&is_gov_ver=1"
)

# ── 默认参数（第五人格 B站服） ────────────────────────────────
DEFAULT_GAME_ID = "301"
DEFAULT_APP_KEY = "h9Ejat5tFh81cq8"


def _build_login_url(
    game_id: str = DEFAULT_GAME_ID,
    app_key: str = DEFAULT_APP_KEY,
) -> str:
    return _LOGIN_URL_TEMPLATE.format(game_id=game_id, app_key=app_key)


# ── Bilibili Sign 算法 ───────────────────────────────────────

def compute_sign(params: dict, app_key: str = DEFAULT_APP_KEY) -> str:
    """Bilibili Game SDK 签名：按 key 排序 → 拼接 value → 追加 appKey → MD5。"""
    sorted_items = sorted(params.items())
    plaintext = "".join(
        str(v) for k, v in sorted_items if k != "sign" and v is not None
    )
    plaintext += app_key
    return hashlib.md5(plaintext.encode("utf-8")).hexdigest()


# ── 二维码登录 API ────────────────────────────────────────────

_QR_GENERATE_URL = "https://wpg-api.biligame.com/api/pcg/qrcode/login/generate"
_QR_POLL_URL = "https://wpg-api.biligame.com/api/pcg/qrcode/login/status/poll"
_QR_POLL_INTERVAL = 2  # 秒
_QR_POLL_TIMEOUT = 180  # 秒


def _generate_qr_ticket(
    game_id: str = DEFAULT_GAME_ID,
    app_key: str = DEFAULT_APP_KEY,
) -> Optional[Dict[str, Any]]:
    """调用 generate API 获取二维码 ticket。

    成功返回 dict: {ticket, redirect_url, game_base_id, polymer_qrcode_switch, ...}
    失败返回 None。
    """
    params = {
        "game_id": game_id,
        "timestamp": str(int(time.time())),
    }
    params["sign"] = compute_sign(params, app_key)
    try:
        resp = requests.post(
            _QR_GENERATE_URL, data=params, timeout=10, verify=should_verify_ssl()
        )
        result = resp.json()
        if result.get("code") == 0 and result.get("ticket"):
            return result
    except Exception:
        pass
    return None


def _poll_qr_status(
    ticket: str,
    game_id: str = DEFAULT_GAME_ID,
    app_key: str = DEFAULT_APP_KEY,
) -> Optional[Dict[str, Any]]:
    """单次轮询二维码扫码状态。

    已扫码且授权成功 → 返回包含 uid/access_key 的 dict。
    未扫码或等待中 → 返回 None。
    出错 → 返回 None。
    """
    params = {
        "game_id": game_id,
        "is_gov_ver": "1",
        "merchant_id": "",
        "ticket": ticket,
        "timestamp": str(int(time.time())),
        "zone_id": "",
    }
    params["sign"] = compute_sign(params, app_key)
    try:
        resp = requests.post(
            _QR_POLL_URL, data=params, timeout=10, verify=should_verify_ssl()
        )
        result = resp.json()
        if result.get("code") == 0 and result.get("access_key"):
            return result
    except Exception:
        pass
    return None


def _render_qr_base64(content: str) -> str:
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


# ── auto.login API ────────────────────────────────────────────

def call_auto_login(
    access_key: str,
    game_id: str = DEFAULT_GAME_ID,
    app_key: str = DEFAULT_APP_KEY,
) -> Dict[str, Any]:
    """调用 auto.login API 验证 access_key 并获取用户信息。

    返回原始 API 响应 dict。code=0 表示成功。

    注意：只发送 3 个核心参数 + sign。不要加 sdk_ver/timestamp（会导致 sign error）。
    """
    params = {
        "access_key": access_key,
        "game_id": str(game_id),
        "is_gov_ver": 1,
    }
    params["sign"] = compute_sign(params, app_key)
    url = "https://wpg-api.biligame.com/api/pcg/auto.login?" + urlencode(params)
    resp = requests.post(url, timeout=15, verify=should_verify_ssl())
    return resp.json()


def _build_intercept_js() -> str:
    """构建注入到登录页的 XHR 拦截脚本。

    功能：
    1. /api/agreement/config → 本地模拟响应，不发真实请求
    2. 其他所有 XHR → 正常发送，load 后记录 {url, method, status, requestBody, responseBody}
    3. /api/pcg/otp/login 或 /api/pcg/login 且 code===0 且含 access_key → 发射登录成功消息
    """
    return r"""(function() {
  var BILI_NET  = "__BILI_NET__::";
  var BILI_LOGIN = "__BILI_LOGIN__::";

  var AGREEMENT_MOCK = JSON.stringify({
    request_id: "mock",
    timestamp: Date.now(),
    code: 0,
    message: "\u54CD\u5E94\u6210\u529F",
    data: { cooperation_mode: 0, agreement_switch: "OFF", privacy_tips_switch: "ON" },
    success: true
  });

  var _origOpen = XMLHttpRequest.prototype.open;
  var _origSend = XMLHttpRequest.prototype.send;
  var _origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function(method, url) {
    this.__bili_url    = (typeof url === "string") ? url : String(url);
    this.__bili_method = method;
    this.__bili_mock   = this.__bili_url.indexOf("/api/agreement/config") !== -1;

    if (!this.__bili_mock) {
      return _origOpen.apply(this, arguments);
    }
    // mocked: 不调用真正的 open，后续 send 也不真正发出
  };

  XMLHttpRequest.prototype.setRequestHeader = function() {
    if (this.__bili_mock) return;
    return _origSetHeader.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function(body) {
    var self = this;

    if (this.__bili_mock) {
      // ── 模拟 agreement/config ──
      setTimeout(function() {
        try {
          Object.defineProperty(self, "readyState",   { value: 4,   configurable: true });
          Object.defineProperty(self, "status",        { value: 200, configurable: true });
          Object.defineProperty(self, "statusText",    { value: "OK", configurable: true });
          Object.defineProperty(self, "responseText",  { value: AGREEMENT_MOCK, configurable: true });
          Object.defineProperty(self, "response",      { value: AGREEMENT_MOCK, configurable: true });
          self.getAllResponseHeaders = function() { return "content-type: application/json\r\n"; };
          self.getResponseHeader    = function(h) {
            return h.toLowerCase() === "content-type" ? "application/json" : null;
          };

          if (typeof self.onreadystatechange === "function") self.onreadystatechange();
          self.dispatchEvent(new Event("load"));
          self.dispatchEvent(new Event("loadend"));

          console.log(BILI_NET + JSON.stringify({
            url: self.__bili_url, method: self.__bili_method,
            status: 200, requestBody: null,
            responseBody: AGREEMENT_MOCK, mocked: true
          }));
        } catch (e) { console.error("[bili-mock] agreement mock error:", e); }
      }, 10);
      return;
    }

    // ── 真实请求：附加 load 监听 ──
    this.addEventListener("load", function() {
      try {
        var entry = {
          url:          self.__bili_url,
          method:       self.__bili_method,
          status:       self.status,
          requestBody:  body,
          responseBody: self.responseText
        };
        console.log(BILI_NET + JSON.stringify(entry));

        // 登录成功检测（账密 /api/pcg/login + OTP /api/pcg/otp/login）
        var u = self.__bili_url || "";
        if (u.indexOf("/api/pcg/otp/login") !== -1 || u.indexOf("/api/pcg/login") !== -1) {
          var resp = JSON.parse(self.responseText);
          if (resp && resp.code === 0) {
            // access_key 可能在根级（OTP）或 data 内（密码登录）
            var ak = resp.access_key || (resp.data && resp.data.access_key);
            if (ak) {
              console.log(BILI_LOGIN + self.responseText);
            }
          }
        }
      } catch (e) {}
    });

    return _origSend.apply(this, arguments);
  };
})();
"""


class BilibiliBrowser(WebBrowser):
    """QWebEngine 浏览器：加载 B站 登录页并捕获登录结果。"""

    def __init__(
        self,
        game_id: str = DEFAULT_GAME_ID,
        app_key: str = DEFAULT_APP_KEY,
    ):
        super().__init__("bilibili", True, frameless=True)
        self.logger = setup_logger()
        self._captured: Optional[Dict[str, Any]] = None
        self._login_url = _build_login_url(game_id, app_key)

        # 无边框、关闭按钮与拖拽行为由 WebBrowser 统一提供。
        self.resize(370, 439)

        # 注入 XHR 拦截脚本（在文档创建前）
        self.add_init_script(_build_intercept_js(), name="bili_xhr_intercept")

    # ── 回调 ──────────────────────────────────────────────────

    def verify(self, url: str) -> bool:
        # 登录靠 JS console 回调，不靠 URL 跳转
        return False

    def on_console_message(
        self,
        level,
        message: str,
        lineNumber: int,
        sourceID: str,
        page=None,
    ):
        if not isinstance(message, str):
            return

        # 网络日志（调试用）
        if message.startswith(BILI_NET_PREFIX):
            payload = message[len(BILI_NET_PREFIX):]
            self.logger.debug(f"[bili-net] {payload[:500]}")
            return

        # 登录成功
        if message.startswith(BILI_LOGIN_PREFIX):
            payload = message[len(BILI_LOGIN_PREFIX):]
            try:
                data = json.loads(payload)
            except Exception:
                self.logger.warning(f"[bili-login] JSON 解析失败: {payload[:200]}")
                return

            # 密码登录响应可能嵌套在 data 字段下，统一展平到根级
            if isinstance(data.get("data"), dict) and "access_key" in data["data"]:
                inner = data.pop("data")
                inner.update({k: v for k, v in data.items() if k not in inner})
                data = inner

            self._captured = data
            self.result = data
            self.logger.info("已捕获 Bilibili 登录响应，准备退出浏览器")
            # 延迟 teardown，避免在 console 回调栈内直接销毁
            QTimer.singleShot(0, self.cleanup)

    def cleanup(self):
        super().cleanup()


class BilibiliLogin:
    """封装 Bilibili 登录流程：二维码扫码（主） + 网页 OTP（备选）。"""

    def __init__(
        self,
        game_id: str = DEFAULT_GAME_ID,
        app_key: str = DEFAULT_APP_KEY,
        cache_game_id: str = "",
    ):
        self.logger = setup_logger()
        self.game_id = game_id
        self.app_key = app_key
        # cache_game_id: 系统 game_id（如 "h55"），用于 QR 缓存 key，与前端一致
        self._cache_game_id = cache_game_id or game_id
        self._active_browser: Optional[BilibiliBrowser] = None
        self._qr_cancelled = False

    def cancel_qr(self):
        """取消正在进行的二维码轮询。"""
        self._qr_cancelled = True

    # ── 二维码缓存 ────────────────────────────────────────────

    def _update_qrcode_cache(
        self,
        status: str,
        qrcode_base64: str = "",
        ticket: str = "",
    ):
        """更新全局二维码缓存，供前端轮询展示。"""
        from envmgr import genv

        cache = genv.get("BILIBILI_QRCODE_CACHE", {})
        if not isinstance(cache, dict):
            cache = {}
        cache_key = self._cache_game_id or "_default"
        cache[cache_key] = {
            "status": status,
            "qrcode_base64": qrcode_base64,
            "ticket": ticket,
            "timestamp": int(time.time()),
        }
        genv.set("BILIBILI_QRCODE_CACHE", cache)

    # ── 二维码登录（阻塞） ────────────────────────────────────

    def qr_login(self) -> Optional[Dict[str, Any]]:
        """二维码扫码登录（阻塞）。

        生成二维码 → 等待扫码 → 返回登录响应 dict（含 uid/access_key）。
        超时、失败或被取消返回 None。
        """
        self._qr_cancelled = False
        self._update_qrcode_cache("loading")

        # 1) 生成 ticket
        gen = _generate_qr_ticket(self.game_id, self.app_key)
        if not gen:
            self.logger.error("Bilibili QR generate 失败")
            self._update_qrcode_cache("failed")
            return None

        ticket = gen["ticket"]
        redirect_url = gen.get("redirect_url", "https://game.bilibili.com/sdk/scanH5/")
        game_base_id = gen.get("game_base_id", "")

        # 2) 构造二维码内容（Web URL，兼容性更好）
        qr_content = f"{redirect_url}?ticket={ticket}&fromNative=false&id={game_base_id}"
        qr_b64 = _render_qr_base64(qr_content)

        self.logger.info(f"Bilibili QR 二维码已生成, ticket={ticket[:20]}...")
        self._update_qrcode_cache("ready", qrcode_base64=qr_b64, ticket=ticket)

        # 3) 轮询扫码状态
        deadline = time.time() + _QR_POLL_TIMEOUT
        while time.time() < deadline:
            if self._qr_cancelled:
                self.logger.info("Bilibili QR 登录已取消")
                self._update_qrcode_cache("cancelled")
                return None

            time.sleep(_QR_POLL_INTERVAL)

            result = _poll_qr_status(ticket, self.game_id, self.app_key)
            if result is not None:
                self.logger.info(
                    f"Bilibili QR 登录成功: uid={result.get('uid')}, "
                    f"uname={result.get('uname')}"
                )
                self._update_qrcode_cache("verified", ticket=ticket)
                return result

        # 超时
        self.logger.warning("Bilibili QR 登录超时")
        self._update_qrcode_cache("expired")
        return None

    # ── 网页 OTP 登录 ─────────────────────────────────────────

    def web_login(self, on_complete=None) -> Optional[Dict[str, Any]]:
        """启动浏览器登录。

        同步模式：阻塞至登录完成，返回登录响应 dict。
        异步模式（on_complete 非 None）：立即返回 None，登录完成后回调。
        """
        browser = BilibiliBrowser(
            game_id=self.game_id,
            app_key=self.app_key,
        )
        browser.set_url(browser._login_url)
        resp = browser.run()

        if resp is None:
            # 异步模式：主事件循环已运行，保持强引用防止 GC
            self._active_browser = browser
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None
                    try:
                        result = b.result
                        if isinstance(result, dict) and result:
                            on_complete(result)
                        else:
                            on_complete(None)
                    except Exception:
                        self.logger.exception("Bilibili 异步登录回调失败")
                        on_complete(None)

                browser._async_completion_callback = _on_async_done
            return None

        # 同步模式
        return resp if isinstance(resp, dict) else None
