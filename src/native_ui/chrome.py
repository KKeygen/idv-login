# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import logging
import sys

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QMainWindow, QToolButton, QVBoxLayout, QWidget

logger = logging.getLogger(__name__)


class _WindowControlButton(QToolButton):

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind

    def set_kind(self, kind: str):
        self.kind = kind
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(255, 255, 255, 245 if self.underMouse() else 210)
        pen = QPen(color, 1.4)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)

        cx = self.width() / 2
        cy = self.height() / 2
        if self.kind == "minimize":
            painter.drawLine(round(cx - 5), round(cy + 3), round(cx + 5), round(cy + 3))
        elif self.kind == "maximize":
            painter.drawRect(round(cx - 5), round(cy - 5), 10, 10)
        elif self.kind == "restore":
            painter.drawRect(round(cx - 4), round(cy - 5), 9, 9)
            painter.drawLine(round(cx - 6), round(cy - 3), round(cx - 6), round(cy + 5))
            painter.drawLine(round(cx - 6), round(cy + 5), round(cx + 3), round(cy + 5))
        else:
            painter.drawLine(round(cx - 5), round(cy - 5), round(cx + 5), round(cy + 5))
            painter.drawLine(round(cx + 5), round(cy - 5), round(cx - 5), round(cy + 5))


class _WindowsWindowControls(QWidget):

    def __init__(self, window: "_WindowsFramelessWindow"):
        super().__init__(window)
        self._window = window
        self.setObjectName("windowsWindowControls")
        self.setFixedSize(114, 34)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        self._minimize_button = self._make_button("minimize", "最小化")
        self._maximize_button = self._make_button("maximize", "最大化")
        self._close_button = self._make_button("close", "关闭", close_button=True)
        self._minimize_button.clicked.connect(window.showMinimized)
        self._maximize_button.clicked.connect(window.toggle_maximized)
        self._close_button.clicked.connect(window.close)

        layout.addWidget(self._minimize_button)
        layout.addWidget(self._maximize_button)
        layout.addWidget(self._close_button)

        self.setStyleSheet(
            """
            QWidget#windowsWindowControls {
                border: 1px solid rgba(255, 255, 255, 28);
                border-radius: 13px;
                background: rgba(12, 16, 23, 178);
            }
            QToolButton {
                color: rgba(255, 255, 255, 210);
                background: transparent;
                border: 0;
                border-radius: 9px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 24);
            }
            QToolButton:pressed {
                background: rgba(255, 255, 255, 36);
            }
            QToolButton#closeButton:hover {
                color: white;
                background: #c42b1c;
            }
            QToolButton#closeButton:pressed {
                background: #a82318;
            }
            """
        )

    def _make_button(
        self, kind: str, tooltip: str, *, close_button: bool = False
    ) -> QToolButton:
        button = _WindowControlButton(kind, self)
        button.setToolTip(tooltip)
        button.setFixedSize(34, 28)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        if close_button:
            button.setObjectName("closeButton")
        return button

    def update_window_state(self):
        maximized = self._window.isMaximized()
        self._maximize_button.set_kind("restore" if maximized else "maximize")
        self._maximize_button.setToolTip("还原" if maximized else "最大化")

class _WindowsFramelessWindow(QMainWindow):

    _WM_NCHITTEST = 0x0084
    _HTLEFT = 10
    _HTRIGHT = 11
    _HTTOP = 12
    _HTTOPLEFT = 13
    _HTTOPRIGHT = 14
    _HTBOTTOM = 15
    _HTBOTTOMLEFT = 16
    _HTBOTTOMRIGHT = 17
    def __init__(self):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setStyleSheet("QMainWindow { background: #10141b; }")

        self._container = QWidget(self)
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        super().setCentralWidget(self._container)

        self._window_controls = _WindowsWindowControls(self)
        self._position_window_controls()
        self._window_controls.raise_()
        self._dwm_style_applied = False

    def set_content_widget(self, widget: QWidget):
        self._layout.addWidget(widget, 1)
        self._window_controls.raise_()

    def toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self._window_controls.update_window_state()
            self._position_window_controls()
        super().changeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_window_controls()

    def _position_window_controls(self):
        inset = 12 if not self.isMaximized() else 8
        self._window_controls.move(
            max(inset, self.width() - self._window_controls.width() - inset),
            inset,
        )
        self._window_controls.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_window_controls()
        if not self._dwm_style_applied:
            self._apply_dwm_style()
            self._dwm_style_applied = True

    def _apply_dwm_style(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winId())
            dwmapi = ctypes.windll.dwmapi

            corner_preference = ctypes.c_int(2)

            class MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]

            margins = MARGINS(1, 1, 1, 1)
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmExtendFrameIntoClientArea.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(MARGINS),
            ]
            native_hwnd = wintypes.HWND(hwnd)
            dwmapi.DwmSetWindowAttribute(
                native_hwnd,
                33,
                ctypes.byref(corner_preference),
                ctypes.sizeof(corner_preference),
            )
            dwmapi.DwmExtendFrameIntoClientArea(
                native_hwnd, ctypes.byref(margins)
            )
        except Exception as exc:
            logger.debug(f"Windows DWM 窗口样式不可用: {exc}")

    def nativeEvent(self, event_type, message):
        if not self.isMaximized() and bytes(event_type) == b"windows_generic_MSG":
            try:
                import ctypes
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == self._WM_NCHITTEST:
                    x = ctypes.c_short(msg.lParam & 0xFFFF).value
                    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    rect = wintypes.RECT()
                    hwnd = int(self.winId())
                    user32 = ctypes.windll.user32
                    user32.GetWindowRect.argtypes = [
                        wintypes.HWND,
                        ctypes.POINTER(wintypes.RECT),
                    ]
                    native_hwnd = wintypes.HWND(hwnd)
                    if user32.GetWindowRect(native_hwnd, ctypes.byref(rect)):
                        try:
                            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
                            user32.GetDpiForWindow.restype = wintypes.UINT
                            dpi = user32.GetDpiForWindow(native_hwnd)
                        except Exception:
                            dpi = 96
                        border = max(6, round(7 * dpi / 96))
                        left = x < rect.left + border
                        right = x >= rect.right - border
                        top = y < rect.top + border
                        bottom = y >= rect.bottom - border

                        if top and left:
                            return True, self._HTTOPLEFT
                        if top and right:
                            return True, self._HTTOPRIGHT
                        if bottom and left:
                            return True, self._HTBOTTOMLEFT
                        if bottom and right:
                            return True, self._HTBOTTOMRIGHT
                        if left:
                            return True, self._HTLEFT
                        if right:
                            return True, self._HTRIGHT
                        if top:
                            return True, self._HTTOP
                        if bottom:
                            return True, self._HTBOTTOM
            except Exception:
                pass
        return False, 0


def _fit_frameless_launcher_size(
    available_width: int, available_height: int
) -> tuple[int, int]:
    target_width, target_height = 1280, 720
    max_width = max(1, int(available_width) - 24)
    max_height = max(1, int(available_height) - 24)
    scale = min(1.0, max_width / target_width, max_height / target_height)
    return max(1, round(target_width * scale)), max(1, round(target_height * scale))
