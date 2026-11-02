from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.parse import urlsplit

from PyQt6.QtCore import QCoreApplication, QEventLoop, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot

RUNTIME_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
MINIMUM_RUNTIME_MAJOR = 136
from webview2_setup import MINIMUM_RUNTIME_VERSION
_BODY_LIMIT = 8 * 1024 * 1024
_live_hosts = set()

_CONSOLE_BRIDGE = r"""(() => {
    const send = (level, args) => {
        try {
            const message = args.map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' ');
            if (message.length <= 1048576 && window.chrome && window.chrome.webview)
                window.chrome.webview.postMessage({__idv_console__: true, level, message});
        } catch (_) {}
    };
    for (const level of ['log', 'info', 'warn', 'error', 'debug']) {
        const original = console[level].bind(console);
        console[level] = (...args) => { send(level, args); return original(...args); };
    }
})();
"""


def _log_callback_failure(operation, error):
    frames = [(frame.f_code.co_filename, line, frame.f_code.co_name)
              for frame, line in traceback.walk_tb(error.__traceback__)]
    logging.getLogger(__name__).error(
        "WebView2 %s: %s hresult=%s stack=%s", operation,
        type(error).__name__, getattr(error, "hresult", None), frames)


class WebView2Error(RuntimeError):
    def __init__(self, operation, hresult=None):
        self.operation = str(operation)
        self.hresult = hresult
        suffix = "" if hresult is None else f" (HRESULT 0x{int(hresult) & 0xffffffff:08X})"
        message = f"WebView2 {self.operation}失败{suffix}"
        if hresult is not None and (int(hresult) & 0xffffffff) == 0x800704ec:
            message += "：检测到会覆盖本工具隔离配置的 WebView2 环境变量或策略。工具不会修改它，请使用未覆盖配置的环境或联系管理员。"
        super().__init__(message)


def runtime_status():
    version = ""
    try:
        if sys.platform != "win32":
            raise WebView2Error("仅支持 Windows")
        from webview2_com_loader import detect_runtime
        version, hr = detect_runtime()
        return {"available": hr >= 0, "version": version, "hresult": hr,
                "minimum_major": MINIMUM_RUNTIME_MAJOR, "minimum_version": MINIMUM_RUNTIME_VERSION}
    except Exception as error:
        hr = getattr(error, "hresult", None)
        message = str(error) if isinstance(error, WebView2Error) else str(WebView2Error("加载器或运行时检测", hr))
        return {"available": False, "version": version, "hresult": hr, "error": message,
                "minimum_major": MINIMUM_RUNTIME_MAJOR, "minimum_version": MINIMUM_RUNTIME_VERSION}


def runtime_version():
    status = runtime_status()
    if not status["available"]:
        raise WebView2Error("运行时检测", status["hresult"])
    return status["version"]


class WebView2Host(QObject):
    _event = pyqtSignal(str, object)

    def __init__(self, parent_hwnd: int, user_data_folder: str, user_agent="", scripts=(),
                 on_event=None, navigation_policy=None):
        super().__init__()
        self._assert_thread()
        self._closed = False
        self._closing = False
        self._close_callbacks = []
        self._started = False
        self._ready = False
        self._in_native_callback = False
        self._on_event = on_event or (lambda event, data: None)
        self._navigation_policy = navigation_policy
        self._next_request = 1
        self._requests = {}
        self._pending_resources = set()
        self._event.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        udf = os.path.abspath(os.fspath(user_data_folder))
        if not Path(udf).is_dir():
            raise WebView2Error("独立资料目录不存在")
        combined = _CONSOLE_BRIDGE + "\n;\n" + "\n;\n".join(str(s) for s in scripts)
        try:
            from webview2_com import ComHost
            self._engine = ComHost(int(parent_hwnd), udf, str(user_agent), combined, self._receive)
        except Exception as error:
            raise WebView2Error("创建", getattr(error, "hresult", None)) from None
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._startup_timeout)
        app = QCoreApplication.instance()
        app.aboutToQuit.connect(self._about_to_quit)
        _live_hosts.add(self)

    @staticmethod
    def _assert_thread():
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise WebView2Error("必须在 Qt GUI 线程调用")

    def _guard(self):
        self._assert_thread()
        if self._closed or self._closing:
            raise WebView2Error("会话已关闭或正在关闭")
        if self._in_native_callback:
            raise WebView2Error("不允许在原生策略回调中重入")

    def _call(self, operation, *args):
        self._guard()
        try:
            return getattr(self._engine, operation)(*args)
        except Exception as error:
            raise WebView2Error(operation, getattr(error, "hresult", None)) from None

    def _receive(self, event, data):
        self._in_native_callback = True
        try:
            if self._closed:
                return 0
            if event == "navigation_check":
                if self._navigation_policy is None:
                    p = urlsplit(data["url"])
                    return int(p.scheme in ("http", "https") or data["url"] in ("about:blank", "about:srcdoc"))
                return int(bool(self._navigation_policy(data["page_id"], data["url"], data["is_frame"])))
            self._event.emit(event, data)
        except Exception as error:
            _log_callback_failure("bridge_callback", error)
            self._event.emit("error", {"page_id": 1, "operation": "bridge_callback", "hresult": -2147467259})
            return 0
        finally:
            self._in_native_callback = False
        return 0

    def _notify(self, event, data):
        try:
            self._on_event(event, data)
        except Exception as error:
            _log_callback_failure("event_callback", error)
            if event != "error":
                try:
                    self._on_event("error", {"page_id": data.get("page_id", 1),
                                   "operation": "event_callback", "hresult": -2147467259})
                except Exception as error:
                    _log_callback_failure("error_callback", error)

    @pyqtSlot(str, object)
    def _deliver(self, event, data):
        if self._closed:
            return
        if event == "closed":
            self._closed = True
            self._closing = False
            self._ready = False
            self._startup_timer.stop()
            _live_hosts.discard(self)
            try:
                QCoreApplication.instance().aboutToQuit.disconnect(self._about_to_quit)
            except (TypeError, RuntimeError):
                pass
            self._notify(event, data)
            callbacks, self._close_callbacks = self._close_callbacks, []
            for cb in callbacks:
                self._invoke(cb, True, None)
            return
        if self._closing and event != "error":
            return
        if event == "result":
            item = self._requests.pop(data["request_id"], None)
            if item is not None:
                cb, timer = item
                timer.stop(); timer.deleteLater()
                error = WebView2Error("异步操作", data["hresult"]) if data["hresult"] < 0 else None
                value = data.get("value") if error is None else None
                self._invoke(cb, value, error)
            return
        if event == "ready":
            self._ready = True
            self._startup_timer.stop()
        if event == "resource_completed":
            self._pending_resources.discard(data["request_id"])
            return
        if event == "resource_request":
            self._pending_resources.add(data["request_id"])
            data["header_items"] = data["headers"]
            data["headers"] = dict(data["header_items"])
        if event == "message":
            value = data.get("value")
            if isinstance(value, dict) and value.get("__idv_console__") is True:
                self._notify("console", {"page_id": data["page_id"], "level": value.get("level", "log"),
                             "message": str(value.get("message", "")), "line": 0,
                             "source": data["source"], "is_frame": data["is_frame"]})
                return
        self._notify(event, data)

    def _invoke(self, cb, value, error):
        if cb is None:
            return
        try:
            cb(value, error)
        except Exception as error:
            _log_callback_failure("result_callback", error)
            self._notify("error", {"page_id": 1, "operation": "result_callback", "hresult": -2147467259})

    def _request(self, operation, args, callback):
        self._guard()
        rid = self._next_request
        self._next_request += 1
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._request_timeout(rid))
        self._requests[rid] = (callback, timer)
        try:
            self._call(operation, *args, rid)
        except WebView2Error as error:
            self._event.emit("result", {"request_id": rid, "hresult": error.hresult or -2147467259, "value": None})
        timer.start(30000)
        return rid

    def _request_timeout(self, rid):
        item = self._requests.pop(rid, None)
        if item:
            cb, timer = item
            timer.deleteLater()
            self._invoke(cb, None, WebView2Error("异步操作超时", -2147023436))

    def _startup_timeout(self):
        if not self._ready and not self._closed:
            self._notify("error", {"page_id": 1, "operation": "startup_timeout", "hresult": -2147023436})

    def start(self):
        self._guard()
        if self._started:
            raise WebView2Error("会话已启动")
        self._started = True
        self._call("start")
        self._startup_timer.start(30000)

    def navigate(self, page_id, url):
        self._call("navigate", int(page_id), str(url))

    def evaluate(self, page_id, script, callback=None):
        return self._request("evaluate", (int(page_id), str(script)), callback)

    def call_devtools(self, page_id, method, params, callback=None):
        return self._request("devtools", (int(page_id), str(method),
                            json.dumps(params, ensure_ascii=False, separators=(",", ":"))), callback)

    def subscribe_devtools(self, page_id, event_name):
        self._call("subscribe_devtools", int(page_id), str(event_name))

    def new_page(self, url="about:blank"):
        return self._call("new_page", str(url))

    def close_page(self, page_id):
        self._call("close_page", int(page_id))

    def resize(self, width, height):
        self._call("resize", int(width), int(height))

    def notify_position_changed(self):
        self._call("notify_position_changed")

    def visible(self, value):
        self._call("visible", bool(value))

    def focus(self, page_id=1, reason="programmatic"):
        reasons = {"programmatic": 0, "next": 1, "previous": 2}
        if reason not in reasons:
            raise WebView2Error("焦点方向无效")
        self._call("focus", int(page_id), reasons[reason])

    def clear_data(self, callback=None):
        return self._request("clear_data", (), callback)

    def capture_preview(self, page_id=1, callback=None):
        return self._request("capture_preview", (int(page_id),), callback)

    def set_virtual_host(self, hostname, folder):
        self._guard()
        if self._started or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
            raise WebView2Error("本地资源 host 配置无效")
        path = Path(folder).resolve(strict=True)
        if not path.is_dir():
            raise WebView2Error("本地资源目录无效")
        self._call("virtual_host", hostname, str(path))

    def add_resource_filter(self, pattern, *, allow_document=False):
        self._guard()
        parsed = urlsplit(pattern)
        if (self._started or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None or parsed.port is not None
                or "*" in parsed.netloc or "?" in parsed.netloc):
            raise WebView2Error("资源拦截需要明确的 HTTPS host")
        if allow_document and (parsed.query or parsed.fragment or '*' in pattern or '?' in pattern):
            raise WebView2Error("文档入口必须是无通配的精确 URL")
        self._call("resource_filter", str(pattern), bool(allow_document))

    def respond(self, request_id, status=200, reason="OK", headers=None, body=b""):
        self._guard()
        if request_id not in self._pending_resources:
            raise WebView2Error("请求已完成或不存在")
        if not isinstance(body, bytes) or len(body) > _BODY_LIMIT:
            raise WebView2Error("响应正文无效")
        if not 100 <= int(status) <= 599 or any(c in str(reason) for c in "\r\n\0"):
            raise WebView2Error("响应状态无效")
        pairs = headers.items() if hasattr(headers, "items") else (headers or ())
        encoded = []
        for name, value in pairs:
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", str(name)) or any(c in str(value) for c in "\r\n\0"):
                raise WebView2Error("响应头无效")
            encoded.append(f"{name}: {value}\r\n")
        self._pending_resources.remove(request_id)
        self._call("respond", int(request_id), int(status), str(reason), "".join(encoded), body)

    @pyqtSlot()
    def _about_to_quit(self):
        try:
            self.close()
        except Exception as error:
            self._notify("error", {"page_id": 1, "operation": "shutdown_close",
                                  "hresult": getattr(error, "hresult", None) or -2147467259})

    def close(self, callback=None):
        self._assert_thread()
        if self._in_native_callback:
            raise WebView2Error("不允许在原生回调中关闭")
        if self._closed:
            if callback is not None:
                QTimer.singleShot(0, lambda: self._invoke(callback, True, None))
            return
        if callback is not None:
            self._close_callbacks.append(callback)
        if self._closing:
            return
        self._closing = True
        self._ready = False
        self._startup_timer.stop()
        try:
            self._engine.close()
        except Exception:
            self._closing = False
            raise
        self._pending_resources.clear()
        requests, self._requests = self._requests, {}
        for cb, timer in requests.values():
            timer.stop(); timer.deleteLater()
            self._invoke(cb, None, WebView2Error("会话正在关闭"))


def shutdown_hosts(timeout=35):
    if not _live_hosts:
        return
    WebView2Host._assert_thread()
    app = QCoreApplication.instance()
    for host in tuple(_live_hosts):
        host._about_to_quit()
    deadline = time.monotonic() + max(0, timeout)
    while _live_hosts and time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(.005)
    if _live_hosts:
        raise WebView2Error("关闭尚未确认，独立资料仍被保留")
