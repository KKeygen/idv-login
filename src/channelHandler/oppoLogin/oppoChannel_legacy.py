from typing import Any, Dict, Optional

from PyQt6.QtCore import QEvent, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView

from channelHandler.WebLoginUtils import WebBrowser
from channelHandler.oppoLogin.consts import (
    DEFAULT_CONSTS,
    OPPO_UC_CLIENT_DOMAIN,
    OppoNativeConsts,
)
from channelHandler.oppoLogin.jsbridge import (
    clear_bridge,
    configure_bridge,
    handle_console_message,
    handle_popup_closed,
    run_web_login,
)
from logutil import setup_logger


class OppoBrowser(WebBrowser):
    def __init__(self, consts: OppoNativeConsts = DEFAULT_CONSTS):
        super().__init__("oppo", True, frameless=True)
        self.logger = setup_logger()
        configure_bridge(self, consts)

    def verify(self, url: str) -> bool:
        # Oppo 登录不靠 URL 跳转判定成功，靠 JSBridge 回调。
        return False

    def _open_observed_webview(self, url: str):
        if not url:
            return

        self._close_observed_popup()

        popup = QWebEngineView(self)
        popup.setWindowTitle("Oppo Verify")
        popup.resize(800, 900)

        popup_page = self.create_page(self.profile, popup)
        popup.setPage(popup_page)
        popup.installEventFilter(self)

        self._observed_popup_view = popup
        self._observed_popup_page = popup_page

        popup.load(QUrl(url))
        popup.show()

    def _close_observed_popup(self):
        popup = self._observed_popup_view
        self._observed_popup_view = None
        self._observed_popup_page = None
        self._pending_observed_callback = None
        try:
            if popup is not None:
                popup.close()
                popup.deleteLater()
        except Exception:
            pass

    def eventFilter(self, watched, event):
        if watched is self._observed_popup_view and event.type() == QEvent.Type.Close:
            self.on_popup_closed(self._observed_popup_page)
        return super().eventFilter(watched, event)

    def cleanup(self):
        clear_bridge(self)
        super().cleanup()

    on_console_message = handle_console_message
    on_popup_closed = handle_popup_closed


class OppoLogin:
    def __init__(self, start_url: str = OPPO_UC_CLIENT_DOMAIN):
        self.logger = setup_logger()
        self.start_url = start_url
        self._active_browser: Optional[OppoBrowser] = None  # 异步模式下保持强引用

    def webLogin(self, consts: OppoNativeConsts = DEFAULT_CONSTS, on_complete=None) -> Optional[Dict[str, Any]]:
        return run_web_login(self, OppoBrowser(consts=consts), on_complete)
