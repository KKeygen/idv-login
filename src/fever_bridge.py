"""Host mpay and bridge its user ticket to Fever-managed games."""

from __future__ import annotations

import ctypes
import os
import sys
import threading

import app_state
from channelHandler.channelUtils import getShortGameId
from fever_ipc import FeverIpcHost


class FeverBridge:
    MPAY_GAME_ID = b"aecglf6ee4aaaarz-g-a50"
    MPAY_APP_CHANNEL = b"a50_sdk_cn"

    def __init__(self, logger):
        self.logger = logger
        self.ipc = FeverIpcHost(self._on_ticket_request, logger)
        self._lock = threading.RLock()
        self._pending_hwnds = []
        self._ticket = ""
        self._login_channel = "netease"
        self._mpay = None
        self._instance = None
        self._callback_refs = []
        self._callback_object = None
        self._dll_directory = None
        self._login_started = False

    def activate(self, game_id: str) -> bool:
        if sys.platform != "win32":
            return False
        short_id = getShortGameId(game_id)
        if not self.ipc.start():
            return False
        app_state.fever_bridge_target_game_ids.add(short_id)
        app_state.run_on_main_thread(self._ensure_mpay_login)
        return True

    def _ensure_mpay_login(self):
        with self._lock:
            if self._login_started:
                return
            self._login_started = True
        try:
            self._load_mpay()
            login = getattr(self._mpay, "?login@CMpay_Interface@Mpay@@QEAAXHH@Z")
            login.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            login(self._instance, 1, 0)
            self.logger.info("平台托管登录界面已启动")
        except Exception:
            with self._lock:
                self._login_started = False
            self.logger.exception("启动平台托管 mpay 登录失败")

    def _load_mpay(self):
        if self._mpay is not None:
            return
        runtime_dir = os.getcwd()
        dll_path = os.path.join(runtime_dir, "mpay.dll")
        skin_path = os.path.join(runtime_dir, "skin")
        if not os.path.isfile(dll_path):
            raise FileNotFoundError(f"未找到 mpay.dll: {dll_path}")
        if hasattr(os, "add_dll_directory"):
            self._dll_directory = os.add_dll_directory(runtime_dir)
        self._mpay = ctypes.CDLL(dll_path)
        create = getattr(self._mpay, "?Create_Interface@CMpay_Interface@Mpay@@SAPEAV12@XZ")
        create.restype = ctypes.c_void_p
        self._instance = create()
        if not self._instance:
            raise RuntimeError("创建 mpay 接口失败")

        callback_pointer = self._build_callbacks()
        set_resource = getattr(self._mpay, "?SetResPath@CMpay_Interface@Mpay@@QEAAHHPEBD@Z")
        set_resource.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
        set_resource.restype = ctypes.c_int
        set_resource(self._instance, 0, skin_path.encode("gbk"))

        window = getattr(app_state.ui_mgr, "_window", None)
        if window is None and app_state.ui_mgr is not None:
            app_state.ui_mgr._ensure_window()
            window = getattr(app_state.ui_mgr, "_window", None)
        if window is not None:
            set_option = getattr(self._mpay, "?SetOption@CMpay_Interface@Mpay@@SAXPEBDPEBX@Z")
            set_option.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            hwnd = ctypes.c_void_p(int(window.winId()))
            set_option(b"gameWndHandle", ctypes.byref(hwnd))

        init = getattr(self._mpay, "?init@CMpay_Interface@Mpay@@QEAAHPEBD0000PEAVApiCallBack@2@H@Z")
        init.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_int,
        ]
        init.restype = ctypes.c_int
        init(
            self._instance,
            self.MPAY_GAME_ID,
            b"",
            self.MPAY_APP_CHANNEL,
            b"",
            b"",
            callback_pointer,
            0,
        )

    def _build_callbacks(self):
        no_args = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        login_finish = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)
        int_void = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)
        ticket_result = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p
        )
        signatures = [
            no_args, login_finish, no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p),
            int_void, ticket_result, ticket_result, int_void, no_args, no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_byte, ctypes.c_int),
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_uint),
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint),
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p),
            int_void,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_byte, ctypes.c_char_p),
            no_args,
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p),
            ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_byte),
        ] + [no_args] * 8

        def make_callback(index, signature):
            def callback(*args):
                if index == 1:
                    self._on_login_finish(args)
                elif index == 5:
                    self._on_ticket_result(args)
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

    def _on_login_finish(self, args):
        try:
            if len(args) > 2 and args[2]:
                info = ctypes.cast(args[2], ctypes.POINTER(ctypes.c_char_p))
                login_type = info[3]
                if login_type:
                    self._login_channel = login_type.decode("utf-8", errors="replace")
            get_ticket = getattr(self._mpay, "?GetUserTicket@CMpay_Interface@Mpay@@QEAAXXZ")
            get_ticket.argtypes = [ctypes.c_void_p]
            get_ticket(self._instance)
        except Exception:
            self.logger.exception("获取平台登录 ticket 失败")

    def _on_ticket_result(self, args):
        code = int(args[1]) if len(args) > 1 else -1
        ticket = args[2].decode("utf-8", errors="replace") if len(args) > 2 and args[2] else ""
        if code != 0 or not ticket:
            self.logger.error(f"平台登录 ticket 获取失败: code={code}")
            return
        with self._lock:
            self._ticket = ticket
            pending = list(self._pending_hwnds)
            self._pending_hwnds.clear()
        for hwnd in pending:
            self.ipc.send_ticket(hwnd, self._login_channel, ticket)

    def _on_ticket_request(self, sender_hwnd: int, _instance: int, _identifier: bytes):
        with self._lock:
            ticket = self._ticket
            if not ticket and sender_hwnd not in self._pending_hwnds:
                self._pending_hwnds.append(sender_hwnd)
        if ticket:
            self.ipc.send_ticket(sender_hwnd, self._login_channel, ticket)
        else:
            app_state.run_on_main_thread(self._ensure_mpay_login)

    def stop(self):
        self.ipc.stop()
        app_state.fever_bridge_target_game_ids.clear()
