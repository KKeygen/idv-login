from PyQt6 import QtCore
from PyQt6.QtCore import QUrl, QTimer, pyqtSlot
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QPushButton, QWidget, QHBoxLayout, QLabel
from envmgr import genv
from logutil import setup_logger
import os
import typing
class WebBrowser(QWidget):
    class WebBrowserPage(QWebEnginePage):
        def __init__(self, profile: QWebEngineProfile, parent: typing.Any, owner: "WebBrowser"):
            super().__init__(profile, parent)
            self._owner = owner

        def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
            try:
                try:
                    self._owner.on_console_message(level, message, lineNumber, sourceID, self)
                except TypeError:
                    self._owner.on_console_message(level, message, lineNumber, sourceID)
            except Exception:
                pass
            return super().javaScriptConsoleMessage(level, message, lineNumber, sourceID)

    class WebEngineView(QWebEngineView):
        def createWindow(self, QWebEnginePage_WebWindowType):
            page = QWebEngineView(self)
            page.urlChanged.connect(self.on_url_changed)
            return page

        def on_url_changed(self, url):
            self.setUrl(url)

    def __init__(self,name="WebLoginDefault",keepCookie=True):
        self.app = QApplication.instance()
        self.logger=setup_logger()
        super().__init__()
        self.view = self.WebEngineView()
        tmpName=genv.get("GLOB_LOGIN_UUID","")
        name=tmpName if tmpName!="" else name
        try:
            # 绑定到当前 QWidget，确保窗口销毁时 profile 能被一并释放（避免 Cookies 文件句柄长期占用）
            self.profile:QWebEngineProfile =  QWebEngineProfile(name, self)
            profile_base_path = genv.get("GLOB_LOGIN_PROFILE_PATH", name)
            cache_base_path = genv.get("GLOB_LOGIN_CACHE_PATH", name)
            self._persistent_storage_path = os.path.join(profile_base_path, tmpName)
            self._cache_path = os.path.join(cache_base_path, tmpName)
            self.profile.setPersistentStoragePath(self._persistent_storage_path)
            self.profile.setCachePath(self._cache_path)
            self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            # 统一使用中文页面语言，避免英文系统下登录页语言错乱
            self.profile.setHttpAcceptLanguage("zh-CN,zh;q=0.9,en;q=0.8")
            self.logger.info(f"Profile创建成功: {name}")
        except Exception as e:
            self.logger.error(f"Profile创建失败： {e}")
            raise

        #cookie相关
        if keepCookie:
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        else:
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.profile.cookieStore().cookieAdded.connect(self.cookie_added)
        
        #page相关
        self.page: QWebEnginePage = self.create_page(self.profile, self.view)
        self.view.setPage(self.page)
        self.cookies = {}
        self.result = ""
        self.page.loadFinished.connect(self.on_load_finished)
        self.page.urlChanged.connect(self.handle_url_change)

        # 创建清除Cookie按钮
        self.clear_cookie_button = QPushButton("强制退登")
        self.clear_cookie_button.clicked.connect(self.clear_cookies)

        # 设置布局
        self.toolBarLayout=QHBoxLayout()
        self.toolBarLayout.addWidget(self.clear_cookie_button)

        self.layout = QVBoxLayout()
        self.layout.addWidget(self.view)
        self.layout.addLayout(self.toolBarLayout)
        self.setLayout(self.layout)

        self._toast_label: typing.Optional[QLabel] = None
        self._toast_timer: typing.Optional[QTimer] = None
        self._browser_running = False

        #窗口置顶
        self.setWindowFlags(QtCore.Qt.WindowType.WindowStaysOnTopHint)
        #设置窗口大小
        self.resize(1000, 1000)

    def _hide_toast(self):
        if self._toast_label is not None:
            try:
                self._toast_label.hide()
            except Exception:
                pass

    def show_toast(self, text: str, duration_ms: int = 3000):
        """在网页视图(view)上方显示一个短提示，并在指定时间后自动隐藏。"""
        if not text:
            return
        if getattr(self, "view", None) is None:
            return

        if self._toast_label is None:
            label = QLabel(self.view)
            label.setObjectName("webbrowser_toast")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet(
                "QLabel#webbrowser_toast {"
                "background-color: rgba(0, 0, 0, 180);"
                "color: white;"
                "padding: 8px 12px;"
                "border-radius: 6px;"
                "}"
            )
            label.hide()
            self._toast_label = label

        self._toast_label.setText(str(text))

        # 尽量限制宽度，避免长文本撑满窗口
        max_width = max(200, int(self.view.width() * 0.8))
        self._toast_label.setMaximumWidth(max_width)
        self._toast_label.adjustSize()

        x = max(0, int((self.view.width() - self._toast_label.width()) / 2))
        y = max(0, int(self.view.height() * 0.05))
        self._toast_label.move(x, y)
        self._toast_label.raise_()
        self._toast_label.show()

        if self._toast_timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._hide_toast)
            self._toast_timer = timer

        self._toast_timer.start(max(0, int(duration_ms)))

    def create_page(self, profile: QWebEngineProfile, parent: typing.Any) -> QWebEnginePage:
        page = self.WebBrowserPage(profile, parent, self)
        try:
            page.certificateError.connect(self._on_cert_error)
        except Exception:
            pass
        try:
            page.renderProcessTerminated.connect(
                lambda status, code: self.logger.error(
                    f"[WebBrowser] renderProcessTerminated: status={status}, exitCode={code}"
                )
            )
        except Exception:
            pass
        return page

    def _on_cert_error(self, error):
        """记录证书错误并自动接受（调试用）。"""
        self.logger.warning(
            f"[WebBrowser] 证书错误: url={error.url().toString()}, "
            f"type={error.type()}, description={error.description()}, "
            f"isOverridable={error.isOverridable()}"
        )
        if error.isOverridable():
            error.acceptCertificate()

    def on_console_message(self, level, message: str, lineNumber: int, sourceID: str, page: typing.Optional[QWebEnginePage] = None):
        """子类可覆盖该方法，实现 JS->Python 的轻量回传（例如通过 console.log 打点）。"""
        return

    def set_user_agent(self, user_agent: str):
        if user_agent:
            self.profile.setHttpUserAgent(user_agent)

    def add_init_script(
        self,
        source: str,
        name: str = "",
        injection_point: QWebEngineScript.InjectionPoint = QWebEngineScript.InjectionPoint.DocumentCreation,
        world_id: QWebEngineScript.ScriptWorldId = QWebEngineScript.ScriptWorldId.MainWorld,
        runs_on_sub_frames: bool = True,
    ):
        """在文档创建前注入脚本（等价于 addInitScript），适合 JSBridge mock。"""
        if not source:
            return
        script = QWebEngineScript()
        if name:
            script.setName(name)
        script.setSourceCode(source)
        script.setInjectionPoint(injection_point)
        script.setWorldId(world_id)
        script.setRunsOnSubFrames(runs_on_sub_frames)
        self.profile.scripts().insert(script)

    def replace_page(self, new_page: QWebEnginePage):
        """替换页面对象并保持 signal 连接一致。"""
        if new_page is None:
            return

        try:
            self.page.loadFinished.disconnect(self.on_load_finished)
        except Exception:
            pass
        try:
            self.page.urlChanged.disconnect(self.handle_url_change)
        except Exception:
            pass

        try:
            old_page = self.page
            self.view.setPage(new_page)
            self.page = self.view.page()
            try:
                old_page.deleteLater()
            except Exception:
                pass
        except Exception:
            self.view.setPage(new_page)
            self.page = self.view.page()

        self.page.loadFinished.connect(self.on_load_finished)
        self.page.urlChanged.connect(self.handle_url_change)

    def set_url(self, url):
        self.view.load(QUrl(url))

    @pyqtSlot(bool)
    def on_load_finished(self, success):
        if not success:
            url = self.page.url().toString() if self.page else "?"
            self.logger.warning(f"[WebBrowser] 页面加载失败: {url}")


    def handle_url_change(self, url):
        self.logger.debug(f"[WebBrowser] URL changed: {url.toString()[:200]}")
        if self.verify(url.toString()):
            if self.parseReslt(url.toString()):
                self.cleanup()

    def export_cookie(self):
        return self.cookies

    def cookie_added(self, cookie):
        self.logger.debug(f"Cookie added: {cookie.name().data().decode()}")
        self.cookies[cookie.name().data().decode()] = cookie.value().data().decode()

    def verify(self, url):
        return True

    def parseReslt(self, url):
        self.result = url
        return True

    def clear_cookies(self):
        cookie_store = self.profile.cookieStore()
        cookie_store.deleteAllCookies()
        #重载页面
        self.view.reload()

    def run(self):
        self.show()
        app = QApplication.instance()
        if app and app.property("_main_loop_running"):
            # 异步模式：主事件循环已运行，嵌套事件循环无法处理
            # QtWebEngine/Chromium IPC 事件。直接返回 None，
            # 登录完成后通过 _async_completion_callback 回调传递结果。
            return None
        else:
            app.exec()
            return self.result

    def closeEvent(self, event):
        """用户手动关闭窗口时，触发 cleanup 释放资源并退出事件循环。"""
        if not getattr(self, '_cleanup_called', False):
            self._cleanup_called = True
            self.cleanup()
        event.accept()

    def cleanup(self):
        self._cleanup_called = True
        self._browser_running = False

        # 在销毁资源之前触发异步回调（回调可能需要访问 cookies / profile）
        cb = getattr(self, '_async_completion_callback', None)
        if cb:
            self._async_completion_callback = None
            try:
                cb(self)
            except Exception:
                self.logger.exception("[WebBrowser] 异步登录回调执行失败")

        try:
            self.profile.cookieStore().cookieAdded.disconnect(self.cookie_added)
        except Exception:
            pass

        try:
            self.page.loadFinished.disconnect(self.on_load_finished)
        except Exception:
            pass
        try:
            self.page.urlChanged.disconnect(self.handle_url_change)
        except Exception:
            pass

        try:
            self.view.setPage(None)
        except Exception:
            pass

        for attr in ("page", "view", "profile"):
            obj: typing.Any = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.deleteLater()
                except Exception:
                    pass
                setattr(self, attr, None)

        try:
            self.close()
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass

        app_inst = QApplication.instance()
        if app_inst and not app_inst.property("_main_loop_running"):
            QTimer.singleShot(0, app_inst.quit)

    