# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations
import os
import sys
import threading
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon
from logutil import setup_logger
logger = setup_logger()

def configure_windows_app_identity():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [wintypes.LPCWSTR]
        set_app_id.restype = ctypes.c_long
        set_app_id("KKeygen.idv-login.GUI")
    except Exception as exc:
        logger.debug(f"设置 Windows 应用标识失败: {exc}")


def _launcher_icon_path() -> str:
    candidates = []
    install_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.extend([
        os.path.join(install_root, "icon.ico"),
        os.path.join(install_root, "assets", "icon.ico"),
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "icon.ico"),
    ])
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        candidates.extend([
            os.path.join(bundle_root, "icon.ico"),
            os.path.join(bundle_root, "assets", "icon.ico"),
        ])
    return next((path for path in candidates if os.path.isfile(path)), "")


def configure_application_icon(app):
    icon_path = _launcher_icon_path()
    if not icon_path:
        logger.warning("未找到启动器图标，任务栏可能继续使用 Qt 默认图标")
        return
    icon = QIcon(icon_path)
    if icon.isNull():
        logger.warning(f"启动器图标无法读取: {icon_path}")
        return
    app.setWindowIcon(icon)


def _minimize_console_window():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        get_console_window = ctypes.windll.kernel32.GetConsoleWindow
        get_console_window.argtypes = []
        get_console_window.restype = wintypes.HWND
        show_window = ctypes.windll.user32.ShowWindow
        show_window.argtypes = [wintypes.HWND, ctypes.c_int]
        show_window.restype = wintypes.BOOL
        hwnd = get_console_window()
        if hwnd:
            show_window(hwnd, 6)
    except Exception as exc:
        logger.debug(f"最小化控制台窗口失败: {exc}")


def register_url_scheme():
    pass


class _UISignalRouter(QObject):
    open_game_sig = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._callback = None
        self.open_game_sig.connect(self._on_open_game)

    def set_callback(self, callback):
        self._callback = callback

    @pyqtSlot(str, str)
    def _on_open_game(self, game_id: str, initial_view: str):
        if self._callback:
            self._callback(game_id, initial_view)


class _MainThreadDispatcher(QObject):
    _dispatch_sig = pyqtSignal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dispatch_sig.connect(self._execute)

    @pyqtSlot(object, object, object)
    def _execute(self, fn, args, bag):
        try:
            bag["value"] = fn(*args)
        except Exception as e:
            bag["error"] = e
        finally:
            bag["event"].set()

    def run_sync(self, fn, *args):
        if threading.current_thread() is threading.main_thread():
            return fn(*args)
        bag = {"event": threading.Event(), "value": None, "error": None}
        self._dispatch_sig.emit(fn, args, bag)
        bag["event"].wait()
        if bag["error"] is not None:
            raise bag["error"]
        return bag["value"]


class UIManager:
    def __init__(self, *, game_helper, ui_logger):
        self.game_helper = game_helper
        self.ui_logger = ui_logger
        self._window = None
        self._router = _UISignalRouter()
        self._router.set_callback(self._do_open_for_game)
        self._dispatcher = _MainThreadDispatcher()

    def open_for_game(self, game_id='', initial_view=''):
        self._router.open_game_sig.emit(game_id, initial_view)

    def start_system_move(self):
        def move():
            window = self._window
            if window is None or not window.isVisible():
                return False
            handle = window.windowHandle()
            return bool(handle and handle.startSystemMove())
        return bool(self._dispatcher.run_sync(move))

    def toggle_maximized(self):
        def toggle():
            if self._window is None or not self._window.isVisible():
                return False
            self._window.toggle_maximized()
            return True
        return bool(self._dispatcher.run_sync(toggle))

    def _force_foreground(self):
        if self._window is None:
            return
        from local_handler import LocalRequestHandler
        LocalRequestHandler._force_dialog_foreground(self._window)
