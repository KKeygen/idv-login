# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from urllib.parse import urlsplit

from PyQt6.QtCore import QTimer

from browser_cookies import CookieExport, CookieExportError, cookies_for_url
from channelHandler.WebLoginUtils import WebBrowser

SUCCESS_URL = "https://www.baidu.com/"
LOGIN_URL = "https://passport.baidu.com/v2/?login&u=https%3A%2F%2Fwww.baidu.com%2F"


class BaiduBrowser(WebBrowser):
    cookie_export = CookieExport(
        source_origins=("https://www.baidu.com",),
        source_paths=("/",),
        targets=(SUCCESS_URL,),
        names=frozenset(("BDUSS",)),
    )

    def __init__(self):
        self.bduss = ""
        self._qt_cookie_records = {}
        self._return_page_loaded = False
        self._finish_queued = False
        self._tls_failed = False
        self._scoped_snapshots = False
        super().__init__("baidu", True, frameless=False)
        self._scoped_snapshots = hasattr(self, "_cookie_snapshots")
        if not self._scoped_snapshots:
            self.profile.cookieStore().cookieRemoved.connect(self._cookie_removed)
        self.setWindowTitle("百度旧版渠道登录")
        self.set_url(LOGIN_URL)

    def verify(self, url):
        try:
            value = str(url or "")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                return False
            p = urlsplit(value)
            return (p.scheme == "https" and p.hostname == "www.baidu.com"
                    and p.port in (None, 443) and p.username is None
                    and p.password is None and p.path in ("", "/"))
        except (ValueError, TypeError):
            return False

    def parseReslt(self, url):
        if self._tls_failed or not self.verify(url):
            return False
        if not self._scoped_snapshots:
            self._refresh_qt_cookies()
        value = self.cookies.get("BDUSS", "")
        if not isinstance(value, str) or not value or len(value) > 4096:
            return False
        if any(ord(c) < 33 or ord(c) > 126 or c == ";" for c in value):
            return False
        self.bduss = value
        self.result = {"authenticated": True}
        return True

    def handle_url_change(self, url):
        if self._tls_failed or getattr(self, "_cleanup_called", False):
            return
        value = url.toString()
        if self._scoped_snapshots:
            if self.verify(value) and self.parseReslt(value):
                self.cleanup()
        else:
            self._return_page_loaded = False

    def on_load_finished(self, success):
        if self._scoped_snapshots or self._tls_failed or getattr(self, "_cleanup_called", False):
            return
        self._return_page_loaded = bool(success and self.page and self.verify(self.page.url().toString()))
        if self._return_page_loaded:
            self._queue_qt_finish()

    def _queue_qt_finish(self):
        if not self._finish_queued and self._return_page_loaded:
            self._finish_queued = True
            QTimer.singleShot(0, self._finish_qt)

    def _finish_qt(self):
        self._finish_queued = False
        if (self._tls_failed or not self._return_page_loaded
                or getattr(self, "_cleanup_called", False) or not self.page):
            return
        if self.parseReslt(self.page.url().toString()):
            self.cleanup()

    @staticmethod
    def _qt_record(cookie):
        name = bytes(cookie.name()).decode("ascii")
        if name != "BDUSS":
            return None
        session = cookie.isSessionCookie()
        return {
            "name": name, "value": bytes(cookie.value()).decode("ascii"),
            "domain": cookie.domain(), "path": cookie.path() or "/",
            "secure": cookie.isSecure(), "httpOnly": cookie.isHttpOnly(),
            "session": session,
            "expires": -1 if session else cookie.expirationDate().toSecsSinceEpoch(),
        }

    def _refresh_qt_cookies(self):
        try:
            records = self.cookie_export.select(list(self._qt_cookie_records.values()), SUCCESS_URL)
            selected = cookies_for_url(records, SUCCESS_URL)
        except CookieExportError:
            selected = {}
        self.cookies.clear()
        self.cookies.update(selected)
        self.bduss = ""

    def cookie_added(self, cookie):
        if isinstance(cookie, dict):
            super().cookie_added(cookie)
            return
        try:
            record = self._qt_record(cookie)
            if record is None:
                return
            key = (record["domain"].lower(), record["path"])
            self._qt_cookie_records.pop(key, None)
            if self.cookie_export.select([record], SUCCESS_URL):
                self._qt_cookie_records[key] = record
            self._refresh_qt_cookies()
            self._queue_qt_finish()
        except (AttributeError, TypeError, ValueError, UnicodeError):
            self._qt_cookie_records.clear()
            self.cookies.clear()
            self.bduss = ""

    def _cookie_removed(self, cookie):
        try:
            record = self._qt_record(cookie)
            if record is None:
                return
            self._qt_cookie_records.pop((record["domain"].lower(), record["path"]), None)
            self._refresh_qt_cookies()
        except (AttributeError, TypeError, ValueError, UnicodeError):
            self._qt_cookie_records.clear()
            self.cookies.clear()
            self.bduss = ""

    def cookies_replaced(self):
        self.bduss = ""

    def clear_cookies(self):
        self._qt_cookie_records.clear()
        self.cookies.clear()
        self.bduss = ""
        self.result = ""
        self._return_page_loaded = False
        super().clear_cookies()

    def _on_cert_error(self, error):
        self._tls_failed = True
        self._qt_cookie_records.clear()
        self.cookies.clear()
        self.bduss = ""
        self.result = ""
        try:
            error.rejectCertificate()
        except Exception:
            pass
        if not getattr(self, "_cleanup_called", False):
            self.cleanup()
