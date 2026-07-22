"""Host mpay and bridge one user ticket to each Fever-managed game launch."""

from __future__ import annotations

import ctypes
from collections import deque
import json
import os
import re
import sys
import threading

if sys.platform == "win32":
    import ctypes.wintypes

import app_state
from channelHandler.channelUtils import getShortGameId
from fever_assets import (
    MPAY_DLL_SHA256,
    MPAY_SKIN_SHA256,
    install_asset_path,
    verified_asset_shadow,
    verified_install_asset,
)
from fever_ipc import FeverIpcHost
from fever_skin import prepare_mpay_skin


MPAY_RESOURCE_ZIP = 2
MPAY_LOGIN_STYLE = 0
MPAY_PARENT_HWND_OPTION = b"mpay_option_parent_hwnd"
SESSION_LIVENESS_CHECK_INTERVAL_SECONDS = 0.5


_MPAY_LOG_SECRET_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|authorization|cookie|session|ticket|token)\b"
    r"\s*[:=]\s*[\"']?)([^\s,;&\"'}]+)"
)


class FeverBridge:
    MPAY_APP_CHANNEL = b"a50_sdk_cn"

    STATE_IDLE = "idle"
    STATE_LOGIN_PENDING = "login_pending"
    STATE_AUTHENTICATED = "authenticated"
    STATE_TICKET_PENDING = "ticket_pending"
    STATE_READY = "ready"

    def __init__(self, logger):
        self.logger = logger
        self.ipc = FeverIpcHost(self._on_ticket_request, logger)
        self._lock = threading.RLock()

        self._sessions = {}
        self._session_queue = deque()
        self._active_session = None
        self._next_session_serial = 0
        self._consumed_sessions = set()
        self._liveness_stop = threading.Event()
        self._liveness_thread = None

        self._ticket = ""
        self._login_channel = "netease"
        self._state = self.STATE_IDLE

        self._mpay = None
        self._instance = None
        self._native_config = None
        self._generation = 0
        self._callback_refs = []
        self._callback_vtable = None
        self._callback_object = None
        self._log_hook_ref = None
        self._mpay_host_window = None
        self._dll_directory = None
        self._dll_path = ""
        self._active_dll_key = ""
        self._mpay_image_generation = 0
        self._pinned_mpay_images = []

    # ------------------------------------------------------------------
    # Launch/session queue
    # ------------------------------------------------------------------

    def activate(self, game_id: str) -> bool:
        """Enable the IPC host for a game; login starts after its op13."""
        if sys.platform != "win32":
            return False
        short_id = getShortGameId(game_id)
        if not self.ipc.start():
            return False
        self._ensure_liveness_watchdog()
        app_state.fever_bridge_target_game_ids.add(short_id)
        return True

    def _ensure_liveness_watchdog(self) -> None:
        with self._lock:
            if self._liveness_thread is not None and self._liveness_thread.is_alive():
                return
            self._liveness_stop.clear()
            self._liveness_thread = threading.Thread(
                target=self._liveness_watchdog_loop,
                name="fever-session-liveness",
                daemon=True,
            )
            self._liveness_thread.start()

    def _liveness_watchdog_loop(self) -> None:
        while not self._liveness_stop.wait(
            SESSION_LIVENESS_CHECK_INTERVAL_SECONDS
        ):
            self._remove_closed_sessions()

    def _remove_closed_sessions(self) -> None:
        closed_active = None
        next_serial = None
        closed = []
        with self._lock:
            for session in list(self._sessions.values()):
                if self._is_session_sender_alive(session):
                    continue
                closed.append(session)
                self._sessions.pop(session["key"], None)
                if self._active_session is session:
                    closed_active = session

            if closed_active is not None:
                self._active_session = None
                self._ticket = ""
                self._state = self.STATE_IDLE
                next_session = self._promote_next_session_locked()
                if next_session is not None:
                    next_serial = next_session["serial"]

        for session in closed:
            self.logger.info(
                "Fever 游戏窗口已关闭，清除未领取 ticket 的会话: "
                f"game={session['short_game_id']}, "
                f"pid={session['process_id']}, instance={session['instance']}"
            )
        if closed_active is not None:
            app_state.run_on_main_thread(
                lambda: self._handle_closed_active_session(next_serial)
            )

    @staticmethod
    def _is_session_sender_alive(session) -> bool:
        if sys.platform != "win32":
            return True
        hwnd = int(session.get("sender_hwnd") or 0)
        expected_pid = int(session.get("process_id") or 0)
        if not hwnd or not expected_pid:
            return False
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
        user32.IsWindow.restype = ctypes.wintypes.BOOL
        if not user32.IsWindow(hwnd):
            return False
        actual_pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(actual_pid))
        return int(actual_pid.value) == expected_pid

    def _handle_closed_active_session(self, next_serial: int | None) -> None:
        try:
            self._logout()
        except Exception:
            self.logger.exception("清理已关闭游戏的 mpay 登录状态失败")
        try:
            self._release_interface()
        except Exception:
            self.logger.exception("卸载已关闭游戏的 mpay 运行时失败")
            return
        if next_serial is not None:
            self._start_active_login(next_serial)

    @property
    def active_target_game_id(self) -> str:
        with self._lock:
            if not self._active_session:
                return ""
            return self._active_session["short_game_id"]

    @property
    def active_target_long_game_id(self) -> str:
        with self._lock:
            if not self._active_session:
                return ""
            return self._active_session["long_game_id"]

    def _resolve_long_game_id(self, short_game_id: str) -> str:
        from cloudRes import CloudRes

        return CloudRes().resolve_cloud_game_id(short_game_id)

    @staticmethod
    def _session_key(sender_hwnd: int, process_id: int, instance: int):
        return int(process_id), int(sender_hwnd), int(instance)

    def _on_ticket_request(
        self,
        sender_hwnd: int,
        process_id: int,
        short_game_id: str,
        instance: int,
        identifier: bytes,
    ) -> bool:
        short_game_id = getShortGameId(short_game_id)
        targets = app_state.fever_bridge_target_game_ids
        if targets and short_game_id not in targets:
            self.logger.warning(
                f"忽略未由工具启动的 Fever 游戏 ticket 请求: {short_game_id}"
            )
            return False

        key = self._session_key(sender_hwnd, process_id, instance)
        should_start = False
        should_flush = False
        with self._lock:
            if key in self._consumed_sessions:
                # A repeated op13 from the same process is an acknowledgement
                # retry, not a second launch and must never consume a new ticket.
                return True
            session = self._sessions.get(key)
            if session is None:
                self._next_session_serial += 1
                long_game_id = self._resolve_long_game_id(short_game_id)
                session = {
                    "key": key,
                    "serial": self._next_session_serial,
                    "sender_hwnd": int(sender_hwnd),
                    "process_id": int(process_id),
                    "instance": int(instance),
                    "identifier": bytes(identifier),
                    "short_game_id": short_game_id,
                    "long_game_id": long_game_id,
                    "sending": False,
                    "destroy_host_on_send": False,
                }
                self._sessions[key] = session
                self._session_queue.append(session)
            if self._active_session is None:
                self._promote_next_session_locked()
                should_start = self._active_session is not None
            should_flush = (
                self._active_session is session
                and self._state == self.STATE_READY
                and bool(self._ticket)
            )

        if should_flush:
            self._flush_active_ticket()
        elif should_start:
            self._schedule_active_login()
        return True

    def _promote_next_session_locked(self):
        while self._session_queue:
            session = self._session_queue.popleft()
            if self._sessions.get(session["key"]) is not session:
                continue
            self._active_session = session
            self._ticket = ""
            self._login_channel = "netease"
            self._state = self.STATE_IDLE
            return session
        self._active_session = None
        return None

    def _schedule_active_login(self):
        with self._lock:
            session = self._active_session
            if not session:
                return
            serial = session["serial"]
        app_state.run_on_main_thread(lambda: self._start_active_login(serial))

    # ------------------------------------------------------------------
    # Native mpay lifecycle
    # ------------------------------------------------------------------

    def _start_active_login(self, session_serial: int):
        with self._lock:
            session = self._active_session
            if (
                not session
                or session["serial"] != session_serial
                or self._state != self.STATE_IDLE
            ):
                return
            self._state = self.STATE_LOGIN_PENDING
            self._ticket = ""
            self._login_channel = "netease"
            long_game_id = session["long_game_id"]
            short_game_id = session["short_game_id"]
        try:
            if (
                not long_game_id
                or long_game_id == short_game_id
                or "-" not in long_game_id
            ):
                long_game_id = self._resolve_long_game_id(short_game_id)
                with self._lock:
                    if self._active_session is session:
                        session["long_game_id"] = long_game_id
            if (
                not long_game_id
                or long_game_id == short_game_id
                or "-" not in long_game_id
            ):
                raise RuntimeError(
                    f"尚未取得 {short_game_id} 的 mpay 长 game_id，无法启动平台托管登录"
                )
            self._logout()
            self._ensure_interface(long_game_id)
            login = getattr(self._mpay, "?login@CMpay_Interface@Mpay@@QEAAXHH@Z")
            login.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            login.restype = None
            login(self._instance, 1, MPAY_LOGIN_STYLE)
            self._schedule_mpay_login_window_activation()
            self.logger.info(
                "平台托管登录界面已启动: "
                f"game={short_game_id}, mpay_game_id={long_game_id}"
            )
        except Exception:
            with self._lock:
                if self._active_session is session:
                    self._state = self.STATE_IDLE
            try:
                self._release_interface()
            except Exception:
                self.logger.exception("清理失败的 mpay 运行时失败")
            self.logger.exception("启动平台托管 mpay 登录失败")

    def _load_mpay_dll(self, target_long_game_id: str):
        if self._mpay is not None:
            return
        runtime_dir = os.getcwd()
        source_path = os.path.abspath(
            verified_install_asset("mpay.dll", MPAY_DLL_SHA256)
        )
        self._mpay_image_generation += 1
        image_generation = self._mpay_image_generation
        dll_path = verified_asset_shadow(
            source_path,
            MPAY_DLL_SHA256,
            os.path.join(runtime_dir, ".idv-login-mpay-runtime"),
            f"generation:{image_generation}",
        )
        if hasattr(os, "add_dll_directory") and self._dll_directory is None:
            self._dll_directory = os.add_dll_directory(runtime_dir)
        try:
            image = ctypes.CDLL(dll_path)
            # init pins every MPay image in the Windows loader.  Keep the
            # wrapper too: once its interface is released this exact image is
            # permanently retired and must never be Create/init-ed again.
            self._pinned_mpay_images.append(image)
            self._mpay = image
            self._dll_path = dll_path
            self._active_dll_key = target_long_game_id
            self.logger.info(
                "MPay 独立映像已激活: "
                f"game_id={target_long_game_id}, generation={image_generation}, "
                f"path={dll_path}, "
                f"handle=0x{int(getattr(image, '_handle', 0) or 0):x}"
            )
        except Exception:
            if self._dll_directory is not None:
                self._dll_directory.close()
                self._dll_directory = None
            raise

    def _ensure_interface(self, target_long_game_id: str):
        game_id = target_long_game_id.encode("utf-8")
        app_channel = self.MPAY_APP_CHANNEL
        desired_config = (game_id, app_channel)
        if (
            self._instance is not None
            and self._native_config == desired_config
        ):
            self._prepare_mpay_host_window()
            return
        if self._instance is not None or self._mpay is not None:
            # MPay binds its account database and other process-global state to
            # the first init game id.  Release the active game's interface;
            # the next game then activates its own path-specific DLL image.
            self._release_interface()

        self._load_mpay_dll(target_long_game_id)
        if self._instance is None:
            create = getattr(
                self._mpay,
                "?Create_Interface@CMpay_Interface@Mpay@@SAPEAV12@XZ",
            )
            create.argtypes = []
            create.restype = ctypes.c_void_p
            self._instance = create()
            if not self._instance:
                raise RuntimeError("创建 mpay 接口失败")
            self._install_mpay_log_hook()

        self._generation += 1
        generation = self._generation
        callback_pointer = self._build_callbacks(generation)

        base_skin_path = install_asset_path("mpay_default_skin.zip")
        skin_path = prepare_mpay_skin(
            base_skin_path,
            os.path.join(os.getcwd(), ".idv-login-mpay-skins"),
            MPAY_SKIN_SHA256,
        )
        set_resource = getattr(
            self._mpay, "?SetResPath@CMpay_Interface@Mpay@@QEAAHHPEBD@Z"
        )
        set_resource.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
        set_resource.restype = ctypes.c_int
        resource_result = set_resource(
            self._instance,
            MPAY_RESOURCE_ZIP,
            skin_path.encode("gbk"),
        )
        self.logger.info(
            f"MPay SetResPath: type={MPAY_RESOURCE_ZIP}, "
            f"result={resource_result}, path={skin_path}"
        )

        self._prepare_mpay_host_window()
        window = self._mpay_host_window
        if window is not None:
            set_option = getattr(
                self._mpay, "?SetOption@CMpay_Interface@Mpay@@SAXPEBDPEBX@Z"
            )
            set_option.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            set_option.restype = None
            hwnd = ctypes.c_void_p(int(window.winId()))
            # SetOption dereferences a pointer to the HWND value immediately.
            # `gameWndHandle` is not an MPay option in this DLL; the supported
            # key is `mpay_option_parent_hwnd`.
            set_option(MPAY_PARENT_HWND_OPTION, ctypes.byref(hwnd))
            self.logger.info(
                "MPay gameWndHandle 已设置: "
                f"hwnd=0x{int(hwnd.value or 0):x}, "
                f"visible={window.isVisible()}"
            )

        init = getattr(
            self._mpay,
            "?init@CMpay_Interface@Mpay@@QEAAHPEBD0000PEAVApiCallBack@2@H@Z",
        )
        init.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        init.restype = ctypes.c_int
        init_result = init(
            self._instance,
            game_id,
            b"",
            app_channel,
            b"",
            b"",
            callback_pointer,
            0,
        )
        self.logger.info(
            "MPay init: "
            f"result={init_result}, game_id={target_long_game_id}, "
            f"app_channel={app_channel.decode('ascii')}"
        )
        self._native_config = desired_config

    def _prepare_mpay_host_window(self):
        from PyQt6.QtWidgets import QApplication

        if self._mpay_host_window is not None:
            if not self._mpay_host_window.isVisible():
                self._mpay_host_window.show()
            self._center_mpay_host_window()
            self._mpay_host_window.raise_()
            self._mpay_host_window.activateWindow()
            QApplication.processEvents()
            return
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QWidget

        # MPay requires a real visible parent HWND.  A one-pixel tool window is
        # enough, does not get its own taskbar button, and gives the SDK a sane
        # monitor/centre point for positioning the login dialog.
        window = QWidget(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        window.setObjectName("idv-login-mpay-host")
        window.setWindowTitle("idv-login mpay host")
        window.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        window.setFixedSize(1, 1)
        # winId() forces creation of the native HWND before it is handed to
        # MPay.  The window must remain visible throughout the login flow.
        window.winId()
        self._mpay_host_window = window
        self._center_mpay_host_window()
        window.show()
        window.raise_()
        window.activateWindow()
        QApplication.processEvents()

    def _center_mpay_host_window(self):
        window = self._mpay_host_window
        if window is None:
            return
        from PyQt6.QtGui import QCursor, QGuiApplication

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        centre = screen.availableGeometry().center()
        window.move(centre.x(), centre.y())

    def _schedule_mpay_login_window_activation(self):
        """Promote the native dialog that MPay creates asynchronously."""
        if sys.platform != "win32":
            return
        from PyQt6.QtCore import QTimer

        for delay_ms in (0, 120, 350, 800, 1500):
            QTimer.singleShot(delay_ms, self._activate_mpay_login_windows)

    def _activate_mpay_login_windows(self):
        if sys.platform != "win32":
            return
        try:
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            current_pid = os.getpid()
            candidates = []
            enum_proc_type = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )
            user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
            user32.EnumWindows.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetClassNameW.argtypes = [
                wintypes.HWND,
                wintypes.LPWSTR,
                ctypes.c_int,
            ]
            user32.GetClassNameW.restype = ctypes.c_int

            @enum_proc_type
            def collect(hwnd, _lparam):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value != current_pid or not user32.IsWindowVisible(hwnd):
                    return True
                class_name = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(hwnd, class_name, len(class_name))
                if class_name.value.upper().startswith("MPAY_"):
                    candidates.append(hwnd)
                return True

            user32.EnumWindows(collect, 0)
            if not candidates:
                return

            SW_RESTORE = 9
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            HWND_TOPMOST = wintypes.HWND(-1)
            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowRect.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.RECT),
            ]
            user32.GetWindowRect.restype = wintypes.BOOL
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HMONITOR
            user32.GetMonitorInfoW.argtypes = [
                wintypes.HMONITOR,
                ctypes.POINTER(MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            foreground = user32.GetForegroundWindow()
            current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
            foreground_thread = (
                user32.GetWindowThreadProcessId(foreground, None)
                if foreground
                else 0
            )
            attached = bool(
                foreground_thread
                and foreground_thread != current_thread
                and user32.AttachThreadInput(
                    current_thread, foreground_thread, True
                )
            )
            try:
                for hwnd in candidates:
                    rect = wintypes.RECT()
                    monitor = user32.MonitorFromWindow(hwnd, 2)
                    monitor_info = MONITORINFO()
                    monitor_info.cbSize = ctypes.sizeof(monitor_info)
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)) and user32.GetMonitorInfoW(
                        monitor, ctypes.byref(monitor_info)
                    ):
                        width = max(1, rect.right - rect.left)
                        height = max(1, rect.bottom - rect.top)
                        work = monitor_info.rcWork
                        x = work.left + max(0, (work.right - work.left - width) // 2)
                        y = work.top + max(0, (work.bottom - work.top - height) // 2)
                    else:
                        x = y = 0
                    user32.ShowWindow(hwnd, SW_RESTORE)
                    user32.SetWindowPos(
                        hwnd,
                        HWND_TOPMOST,
                        x,
                        y,
                        0,
                        0,
                        SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                    )
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    user32.SetActiveWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(
                        current_thread, foreground_thread, False
                    )
        except Exception:
            self.logger.debug("激活 MPay 登录窗口失败", exc_info=True)

    def _install_mpay_log_hook(self):
        log_callback_type = ctypes.CFUNCTYPE(None, ctypes.c_char_p)

        def on_log(message):
            try:
                if not message:
                    return
                text = message.decode("utf-8", errors="replace")
                for line in text.splitlines() or [text]:
                    sanitized = _MPAY_LOG_SECRET_RE.sub(r"\1***", line)
                    self.logger.warning(f"[mpay] {sanitized}")
            except Exception:
                # Never let a Python exception escape through the native hook.
                self.logger.exception("处理 mpay 日志失败")

        self._log_hook_ref = log_callback_type(on_log)
        set_log_hook = getattr(
            self._mpay,
            "?SetLogHook@CMpay_Interface@Mpay@@QEAAXP6AXPEBD@Z@Z",
        )
        set_log_hook.argtypes = [ctypes.c_void_p, log_callback_type]
        set_log_hook.restype = None
        set_log_hook(self._instance, self._log_hook_ref)

    def _build_callbacks(self, generation: int | None = None):
        generation = self._generation if generation is None else generation
        no_args = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        login_finish = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p
        )
        int_void = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)
        ticket_result = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p
        )
        signatures = [
            no_args,
            login_finish,
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p),
            int_void,
            ticket_result,
            ticket_result,
            int_void,
            no_args,
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_byte, ctypes.c_int),
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_uint),
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint),
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p),
            int_void,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_byte, ctypes.c_char_p),
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p),
            ctypes.CFUNCTYPE(
                None,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_byte,
            ),
        ] + [no_args] * 8

        def make_callback(index, signature):
            def callback(*args):
                if generation != self._generation:
                    return
                if index == 1:
                    self._on_login_finish(args, generation)
                elif index == 5:
                    self._on_ticket_result(args, generation)

            return signature(callback)

        self._callback_refs = [
            make_callback(index, signature)
            for index, signature in enumerate(signatures)
        ]
        vtable = (ctypes.c_void_p * len(self._callback_refs))(
            *[ctypes.cast(callback, ctypes.c_void_p) for callback in self._callback_refs]
        )

        class CallbackObject(ctypes.Structure):
            _fields_ = [("vptr", ctypes.c_void_p), ("padding", ctypes.c_ubyte * 4096)]

        self._callback_vtable = vtable
        self._callback_object = CallbackObject()
        self._callback_object.vptr = ctypes.addressof(vtable)
        return ctypes.cast(ctypes.pointer(self._callback_object), ctypes.c_void_p)

    def _logout(self):
        if self._instance is None:
            return
        logout = getattr(self._mpay, "?logout@CMpay_Interface@Mpay@@QEAAXXZ")
        logout.argtypes = [ctypes.c_void_p]
        logout.restype = None
        logout(self._instance)

    def _release_interface(self):
        self._generation += 1
        instance = self._instance
        release_error = None
        try:
            if instance is not None and self._mpay is not None:
                release = getattr(
                    self._mpay,
                    "?Release_Interface@CMpay_Interface@Mpay@@SAXXZ",
                )
                release.argtypes = []
                release.restype = None
                release()
        except Exception as exc:
            release_error = exc
        finally:
            # Release_Interface is synchronous.  Once it returns, no native
            # object may retain the previous callback object, vtable, log hook,
            # or parent-window association.  Destroy all Python/Qt references
            # before detaching the released, loader-pinned image.
            self._instance = None
            self._native_config = None
            self._callback_refs = []
            self._callback_vtable = None
            self._callback_object = None
            self._log_hook_ref = None
            self._destroy_mpay_host_window()
            self._detach_mpay_dll()
        if release_error is not None:
            raise release_error

    def _detach_mpay_dll(self):
        """Detach the released image; MPay pins it until process exit.

        A released image is permanently retired.  The next Create/init uses a
        fresh verified path even for the same game, because Release_Interface
        does not reset all window/database globals inside the pinned module.
        """
        library = self._mpay
        self._mpay = None
        dll_path = self._dll_path
        self._dll_path = ""
        dll_key = self._active_dll_key
        self._active_dll_key = ""
        try:
            if library is None:
                return
            self.logger.info(
                "MPay interface 已释放，独立映像保持驻留: "
                f"game_id={dll_key}, path={dll_path}, "
                f"handle=0x{int(getattr(library, '_handle', 0) or 0):x}"
            )
        finally:
            if self._dll_directory is not None:
                self._dll_directory.close()
                self._dll_directory = None

    # ------------------------------------------------------------------
    # Login callbacks and one-shot delivery
    # ------------------------------------------------------------------

    def _on_login_finish(self, args, generation: int | None = None):
        if generation is not None and generation != self._generation:
            return
        code = int(args[1]) if len(args) > 1 else -1
        if code != 0 or len(args) <= 2 or not args[2]:
            with self._lock:
                self._state = self.STATE_IDLE
            self.logger.warning(f"平台登录未完成: code={code}")
            return
        try:
            info = ctypes.cast(args[2], ctypes.POINTER(ctypes.c_char_p))
            if info[8]:
                ext_info = json.loads(info[8].decode("utf-8"))
                src_app_channel = ext_info.get("src_app_channel")
                if src_app_channel:
                    self._login_channel = str(src_app_channel)
            direct_ticket = (
                info[2].decode("utf-8", errors="replace")
                if self._login_channel != "netease" and info[2]
                else ""
            )
            with self._lock:
                if self._state != self.STATE_LOGIN_PENDING or not self._active_session:
                    return
                if self._login_channel == "netease":
                    self._state = self.STATE_AUTHENTICATED
                elif direct_ticket:
                    self._state = self.STATE_READY
                    self._ticket = direct_ticket
                    session = self._active_session
                    session["destroy_host_on_send"] = True
                else:
                    self._state = self.STATE_IDLE
            if self._login_channel == "netease":
                self._request_user_ticket()
            elif direct_ticket:
                self._flush_active_ticket()
            else:
                self.logger.error(
                    "渠道服登录成功但未返回可用 ticket: "
                    f"src_app_channel={self._login_channel}"
                )
        except Exception:
            with self._lock:
                self._state = self.STATE_IDLE
            self.logger.exception("处理平台登录结果失败")

    def accept_channel_qrcode_ticket(
        self, login_channel: str, ticket: str
    ) -> bool:
        """Consume a hosted channel QR code before MPay exchanges it."""
        login_channel = str(login_channel or "")
        ticket = str(ticket or "")
        if not login_channel or login_channel == "netease" or not ticket:
            return False
        with self._lock:
            if self._state != self.STATE_LOGIN_PENDING or not self._active_session:
                return False
            self._login_channel = login_channel
            self._ticket = ticket
            self._state = self.STATE_READY
            self._active_session["destroy_host_on_send"] = True
        self._flush_active_ticket()
        return True

    def _destroy_mpay_host_window(self):
        window = self._mpay_host_window
        self._mpay_host_window = None
        if window is None:
            return
        window.close()
        window.destroy(True, True)
        window.deleteLater()
        from PyQt6.QtWidgets import QApplication

        QApplication.processEvents()

    def _request_user_ticket(self):
        with self._lock:
            if self._state != self.STATE_AUTHENTICATED or not self._active_session:
                return
            self._state = self.STATE_TICKET_PENDING
        try:
            get_ticket = getattr(
                self._mpay, "?GetUserTicket@CMpay_Interface@Mpay@@QEAAXXZ"
            )
            get_ticket.argtypes = [ctypes.c_void_p]
            get_ticket.restype = None
            get_ticket(self._instance)
        except Exception:
            with self._lock:
                self._state = self.STATE_AUTHENTICATED
            self.logger.exception("获取平台登录 ticket 失败")

    def _on_ticket_result(self, args, generation: int | None = None):
        if generation is not None and generation != self._generation:
            return
        code = int(args[1]) if len(args) > 1 else -1
        ticket = (
            args[2].decode("utf-8", errors="replace")
            if len(args) > 2 and args[2]
            else ""
        )
        if code != 0 or not ticket:
            with self._lock:
                if self._state == self.STATE_TICKET_PENDING:
                    self._state = self.STATE_AUTHENTICATED
            self.logger.error(f"平台登录 ticket 获取失败: code={code}")
            return
        with self._lock:
            if self._state != self.STATE_TICKET_PENDING or not self._active_session:
                return
            self._state = self.STATE_READY
            self._ticket = ticket
        self._flush_active_ticket()

    def _remember_consumed_locked(self, key):
        self._consumed_sessions.add(key)

    def _flush_active_ticket(self) -> bool:
        with self._lock:
            session = self._active_session
            if (
                not session
                or session["sending"]
                or self._state != self.STATE_READY
                or not self._ticket
            ):
                return False
            session["sending"] = True
            ticket = self._ticket
            login_channel = self._login_channel

        sent = self.ipc.send_ticket(
            session["sender_hwnd"], login_channel, ticket
        )

        should_start_next = False
        next_serial = None
        destroy_host = False
        with self._lock:
            if self._active_session is not session:
                return False
            session["sending"] = False
            if not sent:
                return False
            # op14 accepted: this ticket is irreversibly consumed by this one
            # game process.  It can never be reused for another HWND/instance.
            self._ticket = ""
            self._state = self.STATE_IDLE
            self._sessions.pop(session["key"], None)
            self._remember_consumed_locked(session["key"])
            self._active_session = None
            destroy_host = bool(session.get("destroy_host_on_send"))
            next_session = self._promote_next_session_locked()
            should_start_next = next_session is not None
            if next_session is not None:
                next_serial = next_session["serial"]

        if destroy_host:
            app_state.run_on_main_thread(
                lambda: self._finish_channel_code_handoff(next_serial)
            )
        elif should_start_next:
            self._schedule_active_login()
        return True

    def _finish_channel_code_handoff(self, next_serial: int | None) -> None:
        try:
            self._release_interface()
        except Exception:
            self.logger.exception("卸载已完成渠道服登录的 MPay 运行时失败")
            return
        if next_serial is not None:
            self._start_active_login(next_serial)

    def _shutdown_mpay(self):
        try:
            self._logout()
        except Exception:
            self.logger.exception("退出 mpay 账号失败")
        try:
            self._release_interface()
        except Exception:
            self.logger.exception("释放 mpay 接口失败")
        self._destroy_mpay_host_window()

    def stop(self):
        self._liveness_stop.set()
        liveness_thread = self._liveness_thread
        if (
            liveness_thread is not None
            and liveness_thread is not threading.current_thread()
        ):
            liveness_thread.join(timeout=1)
        self._liveness_thread = None
        self.ipc.stop()
        with self._lock:
            self._state = self.STATE_IDLE
            self._ticket = ""
            self._sessions.clear()
            self._session_queue.clear()
            self._active_session = None
            self._consumed_sessions.clear()
        app_state.fever_bridge_target_game_ids.clear()
        app_state.run_on_main_thread(self._shutdown_mpay)
