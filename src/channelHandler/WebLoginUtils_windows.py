from __future__ import annotations
import os
from urllib.parse import urlsplit
from PyQt6.QtCore import QEvent, Qt, QUrl, QTimer
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QPushButton, QWidget, QHBoxLayout, QLabel
import app_state
from browser_runtime import BrowserSession
from envmgr import genv
from logutil import setup_logger
_CURRENT_COOKIE_TOKEN = object()

class WebView2Page:
    def __init__(self, owner, page_id="main"):
        self.owner = owner
        self.page_id = page_id
        self._url = "about:blank"
        self.cookie_token = None

    def url(self):
        return QUrl(self._url)

    def runJavaScript(self, script, callback=None):
        if self.owner.session is not None:
            self.owner.session.evaluate(self.page_id, script, callback)

    def toPlainText(self, callback):
        if self.owner.cookie_export is not None:
            if self.cookie_token is None or self.owner.session is None:
                callback(None)
                return
            self.owner.session.read_body(
                self.page_id, callback, cookie_token=self.cookie_token)
        else:
            self.runJavaScript("document.body ? document.body.innerText : ''", callback)

    def close(self):
        if self.owner.session is not None:
            self.owner.session.close_page(self.page_id)


class WebBrowser(QWidget):
    callback_schemes = ()
    cookie_export = None

    def __init__(self, name="WebLoginDefault", keepCookie=True, frameless=False):
        super().__init__()
        self.app = QApplication.instance()
        self.logger = setup_logger()
        self.frameless = bool(frameless)
        self.cookies = {}
        self.cookie_records = []
        self._cookie_snapshots = {}
        self._event_cookie_token = self._cookie_commit_token = None
        self._cookie_commit_accepted = self._session_finished = False
        self.result = ""
        self.session = None
        self.page = WebView2Page(self)
        self._pages = {"main": self.page}
        self._keep_cookies = bool(keepCookie)
        self._user_agent = ""
        self._scripts = []
        self._url = "about:blank"
        self._cleanup_called = self._finished = self._browser_running = False
        self._login_cancelled = False
        self._startup_error = self._setup_dialog = None
        self._drag_pos = None
        self._moving_focus = False
        self._toast_label = self._toast_timer = None
        self._frameless_close_button = None
        identity = str(genv.get("GLOB_LOGIN_UUID", "") or name)
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            import hashlib
            identity = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        profile_base = genv.get("GLOB_LOGIN_PROFILE_PATH") or os.path.join(genv.get("FP_WORKDIR") or os.getcwd(), "profile")
        self._persistent_storage_path = os.path.join(profile_base, identity, "webview2-login")
        self.setWindowTitle("渠道登录")
        self.layout = QVBoxLayout(self)
        self.view = QWidget(self)
        self.view.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.view.setMinimumSize(240, 240)
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.installEventFilter(self)
        self.layout.addWidget(self.view, 1)
        self._status_label = QLabel("正在准备系统 WebView2 登录页面…", self)
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(True)
        self.layout.addWidget(self._status_label)
        self.toolBarLayout = QHBoxLayout()
        self.clear_cookie_button = None
        if not self.frameless:
            self.clear_cookie_button = QPushButton("强制退登")
            self.clear_cookie_button.clicked.connect(self.clear_cookies)
            self.toolBarLayout.addWidget(self.clear_cookie_button)
            self.layout.addLayout(self.toolBarLayout)
        flags = Qt.WindowType.WindowStaysOnTopHint
        if self.frameless:
            flags |= Qt.WindowType.FramelessWindowHint
            self.layout.setContentsMargins(0, 0, 0, 0)
        self.setWindowFlags(flags)
        self.resize(1000, 750)
        if self.frameless:
            close = QPushButton("✕", self)
            close.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            close.setFixedSize(28, 28)
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setStyleSheet("QPushButton{background:rgba(0,0,0,90);color:white;border:0;border-radius:14px;font-size:14px;}QPushButton:hover{background:rgba(220,53,69,220);}QPushButton:pressed{background:rgba(180,40,50,240);}")
            close.clicked.connect(self.close)
            self._frameless_close_button = close
            self._position_close()

    def _position_close(self):
        if self._frameless_close_button is not None:
            self._frameless_close_button.move(self.width() - 32, 4)
            self._frameless_close_button.raise_()

    def _resize_host(self):
        if self.session is not None and not self.session.is_closing:
            ratio = self.view.devicePixelRatioF()
            self.session.resize(round(self.view.width() * ratio), round(self.view.height() * ratio))

    def eventFilter(self, watched, event):
        if watched is self.view and event.type() in (QEvent.Type.Resize, QEvent.Type.DevicePixelRatioChange):
            self._resize_host()
            self._position_toast()
        elif watched is self.view and event.type() == QEvent.Type.Move:
            if self.session is not None:
                self.session.notify_position_changed()
        elif watched is self.view and event.type() == QEvent.Type.FocusIn and not self._moving_focus:
            if self.session is not None and not self.session.is_closing:
                reason = {Qt.FocusReason.TabFocusReason: 'next', Qt.FocusReason.BacktabFocusReason: 'previous'}.get(event.reason(), 'programmatic')
                self.session.focus(reason=reason)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        self._position_close()
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        if self.frameless and event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.frameless and self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _position_toast(self):
        if self._toast_label is not None and self._toast_label.isVisible():
            label = self._toast_label
            label.setMaximumWidth(max(200, int(self.view.width() * .8)))
            label.adjustSize()
            from PyQt6.QtCore import QPoint
            point = QPoint(max(0, (self.view.width() - label.width()) // 2), max(0, int(self.view.height() * .05)))
            label.move(self.view.mapToGlobal(point))

    def moveEvent(self, event):
        if self.session is not None:
            self.session.notify_position_changed()
        self._position_toast()
        super().moveEvent(event)

    def hideEvent(self, event):
        if self._toast_label is not None:
            self._toast_label.hide()
        super().hideEvent(event)

    def show_toast(self, text, duration_ms=3000):
        if not text:
            return
        if self._toast_label is None:
            label = QLabel(self, Qt.WindowType.ToolTip | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
            label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            label.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            label.setObjectName('webbrowser_toast')
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet('QLabel#webbrowser_toast { background-color: rgba(0, 0, 0, 180); color: white; padding: 8px 12px; border-radius: 6px; }')
            self._toast_label = label
            self._toast_timer = QTimer(self)
            self._toast_timer.setSingleShot(True)
            self._toast_timer.timeout.connect(label.hide)
        self._toast_label.setText(str(text))
        self._toast_label.show()
        self._position_toast()
        self._toast_timer.start(max(0, int(duration_ms)))

    def _start_browser(self):
        if self.clear_cookie_button is not None:
            self.clear_cookie_button.setEnabled(False)
        self._status_label.setText("正在准备系统 WebView2 登录页面…")
        self._status_label.show()
        self._startup_error = None
        self._session_finished = False
        self.session = BrowserSession(dispatch=app_state.run_on_main_thread, on_event=self._on_browser_event,
            parent_hwnd=int(self.view.winId()), profile_path=self._persistent_storage_path,
            persistent=self._keep_cookies, user_agent=self._user_agent, scripts=self._scripts,
            callback_schemes=self.callback_schemes,
            cookie_export=self.cookie_export)
        self.session.start(self._url)

    def set_user_agent(self, user_agent):
        if self.session is not None:
            raise RuntimeError("浏览器启动后不可更换登录 User-Agent")
        self._user_agent = str(user_agent or "")

    def add_init_script(self, source, name=""):
        if self.session is not None:
            raise RuntimeError("初始化脚本必须在打开登录页面前设置")
        if source:
            self._scripts.append(str(source))

    def set_url(self, url):
        self._url = str(url)
        if self.session is not None:
            self.session.navigate("main", self._url)

    def open_popup(self, url):
        if self.session is None:
            raise RuntimeError("登录浏览器尚未启动")
        page_id = self.session.new_page(str(url))
        page = WebView2Page(self, page_id)
        self._pages[page_id] = page
        return page

    def _on_browser_event(self, event, *args):
        if event == "preparing_runtime":
            if not self._cleanup_called:
                self._status_label.setText(args[0])
                self._status_label.show()
            return
        if event == "cookies_snapshot":
            if self._cleanup_called:
                return
            token, records = args
            self._cookie_snapshots[token] = tuple(dict(cookie) for cookie in records)
            while len(self._cookie_snapshots) > 256:
                self._cookie_snapshots.pop(next(iter(self._cookie_snapshots)))
            self._replace_cookie_records(records)
            return
        elif event == "cookie_boundary":
            if self._cleanup_called:
                return
            page_id, token = args
            page = self._pages.setdefault(page_id, WebView2Page(self, page_id))
            page.cookie_token = token
            self._event_cookie_token = token
            self._replace_cookie_records(self._cookie_snapshots.get(token, ()))
            return
        elif event == "cookies_committed":
            token, records = args
            if self._finished or token != self._cookie_commit_token:
                return
            self._cookie_commit_accepted = True
            self._replace_cookie_records(records)
            self._cookie_snapshots = {token: tuple(dict(cookie) for cookie in records)}
            if self._session_finished:
                self._complete()
            return
        elif event == "cookies_reset":
            if not self._cleanup_called:
                self._cookie_snapshots.clear()
                self._event_cookie_token = None
                for page in self._pages.values():
                    page.cookie_token = None
                self._replace_cookie_records(())
            return
        if event == "cookie_error":
            if self._cookie_commit_accepted:
                return
            self.result = ""
            self._cookie_commit_token = None
            self._cookie_snapshots.clear()
            self._replace_cookie_records(())
            self._status_label.setText(args[0])
            self._status_label.show()
            app_state.toast(args[0], 7000)
            self.cleanup()
            if self._session_finished:
                self._complete()
            return
        if event == "finished":
            self._session_finished = True
            if self._cookie_commit_token is not None and not self._cookie_commit_accepted:
                self.result = ""
                self._cookie_commit_token = None
                self._cookie_snapshots.clear()
                self._replace_cookie_records(())
                app_state.toast("浏览器已关闭，未确认登录状态。请重新登录。", 7000)
            if self._startup_error and not self._cleanup_called:
                from browser_setup import BrowserSetupDialog
                def retry():
                    self._setup_dialog.deleteLater()
                    self._setup_dialog = None
                    self._start_browser()
                def clear_retry():
                    self._setup_dialog.deleteLater()
                    self._setup_dialog = None
                    self.setEnabled(False)
                    self._status_label.setText("正在清除本工具的独立登录状态…")
                    def cleared(future):
                        try:
                            success = future.result() is True
                        except Exception:
                            success = False
                        def resume():
                            if self._finished or self._cleanup_called:
                                return
                            self.setEnabled(True)
                            if success:
                                self._start_browser()
                            else:
                                app_state.toast("无法安全清除独立登录状态，本次登录已取消。", 7000)
                                self._complete()
                        app_state.run_on_main_thread(resume)
                    self.session.clear_cookies().add_done_callback(cleared)
                self._setup_dialog = BrowserSetupDialog(
                    self._startup_error, on_retry=retry, on_cancel=self._complete,
                    on_clear_retry=clear_retry, parent=self)
                self._setup_dialog.open()
            else:
                self._complete()
            return
        if self._cleanup_called:
            return
        if event == "started":
            self._status_label.hide()
            if self.clear_cookie_button is not None:
                self.clear_cookie_button.setEnabled(True)
            self._resize_host()
            if self._frameless_close_button is not None:
                self._frameless_close_button.raise_()
        elif event == "startup_failed":
            self._startup_error = args[0]
            self._status_label.setText(args[0])
            self._status_label.show()
        elif event == "error":
            self._status_label.setText(args[0])
            self._status_label.show()
            self.logger.error(args[0])
            app_state.toast(args[0], 6000)
        elif event == "focus_move":
            if args[0] == 'main':
                self._moving_focus = True
                try:
                    self.view.setFocus(Qt.FocusReason.OtherFocusReason)
                    self.view.focusNextPrevChild(args[1] != 'previous')
                finally:
                    self._moving_focus = False
        elif event == "page":
            self._pages.setdefault(args[0], WebView2Page(self, args[0]))
        elif event == "url":
            page_id, url = args
            page = self._pages.setdefault(page_id, WebView2Page(self, page_id))
            page._url = url
            self.handle_url_change(QUrl(url))
        elif event == "loaded":
            page = self._pages.setdefault(args[0], WebView2Page(self, args[0]))
            self.on_page_load_finished(page, args[1])
        elif event == "console":
            page_id, level, message, line, source = args
            page = self._pages.setdefault(page_id, WebView2Page(self, page_id))
            self.on_console_message(level, message, line, source, page)
        elif event == "page_closed":
            self.on_popup_closed(self._pages.pop(args[0], None))

    def on_popup_closed(self, page):
        pass

    def on_console_message(self, level, message, lineNumber, sourceID, page=None):
        pass

    def on_page_load_finished(self, page, success):
        if page is self.page:
            self.on_load_finished(success)

    def on_load_finished(self, success):
        if not success:
            self.logger.debug("[WebBrowser] 页面导航中断或加载失败")

    def handle_url_change(self, url):
        parsed = urlsplit(url.toString())
        self.logger.debug(f"[WebBrowser] 页面变化: {parsed.scheme}://{parsed.hostname or ''}{parsed.path}")
        if self.verify(url.toString()) and self.parseReslt(url.toString()):
            self.cleanup()

    def verify(self, url):
        return True

    def parseReslt(self, url):
        self.result = url
        return True

    def cookies_replaced(self):
        pass

    def _replace_cookie_records(self, records):
        self.cookie_records = [dict(cookie) for cookie in records]
        self.cookies.clear()
        self.cookies_replaced()
        for cookie in self.cookie_records:
            self.cookie_added(cookie)

    def cookie_added(self, cookie):
        self.cookies[cookie["name"]] = cookie["value"]

    def export_cookie(self):
        return dict(self.cookies)

    def clear_cookies(self):
        if self.session is not None:
            self.session.clear_cookies()

    def run(self):
        self.show()
        self._browser_running = True
        self._start_browser()
        if self.app and self.app.property("_main_loop_running"):
            return None
        self.app.exec()
        return self.result

    def cancel_login(self):
        if not self._finished:
            self._login_cancelled = True
            self.result = ""
            self.close()

    def closeEvent(self, event):
        if not self._finished:
            self.cleanup()
            event.ignore()
        else:
            event.accept()

    def cleanup(self, cookie_token=_CURRENT_COOKIE_TOKEN):
        if self._cleanup_called:
            return
        if self.cookie_export is not None and self.result:
            token = self._event_cookie_token if cookie_token is _CURRENT_COOKIE_TOKEN else cookie_token
            if token is None or token not in self._cookie_snapshots:
                self.result = ""
                self._replace_cookie_records(())
                app_state.toast("未取得完成页面对应的安全 Cookie 快照，已取消本次登录。", 7000)
            else:
                self._cookie_commit_token = token
                self._replace_cookie_records(self._cookie_snapshots[token])
        self._cleanup_called = True
        self._browser_running = False
        self.setEnabled(False)
        if self.session is not None:
            self.session.finish(self._cookie_commit_token)
            if self._session_finished and self._cookie_commit_token is None:
                self._complete()
        else:
            self._complete()

    def _complete(self):
        if self._finished:
            return
        if self._cookie_commit_token is not None and not self._cookie_commit_accepted:
            return
        if self._login_cancelled or (self.cookie_export is not None and not self._cookie_commit_accepted):
            self.result = ""
            self._replace_cookie_records(())
        self._finished = True
        self._cleanup_called = True
        self._browser_running = False
        callback = getattr(self, "_async_completion_callback", None)
        self._async_completion_callback = None
        self.hide()
        if callback is not None:
            try:
                callback(self)
            except Exception:
                self.logger.exception("[WebBrowser] 异步登录回调执行失败")
        self.close()
        self.deleteLater()
        if self.app and not self.app.property("_main_loop_running"):
            QTimer.singleShot(0, self.app.quit)
