"""Minimal Fever-compatible WM_COPYDATA host used by the launcher bridge."""

from __future__ import annotations

import ctypes
import json
import re
import struct
import sys
import threading


_XOR_TABLE = bytes.fromhex(
    "4c85667cf22497662300819a8e03cbc7901d15ea463d7bacc36fbda7277813b4"
    "8e7cee311aa2a72d09e5b315de16379c940c7f53949007a7b016534c32f641dc"
    "88e4175eb1038cd48456ee4e108c20f3648cc591ae75f343a3d64316cf9329bd"
    "cb8581c8db100d1a05f087cb1318f5c4f994eeb573d05c1e41c96f409b561846"
    "3ee0d3f8a26901170ca5c6fd2a1ec90cfea21c3de8ecf9d406220a3ff7b6da73"
    "c8c4efeffa33062499adf61ba32d230f45d4d16c59476477a653e4a52aaf62dc"
    "c1897bfb9bfd03efff932c1cd59c6e91968b19bdc82531288a1acb3a92dffa09"
    "7b0d9154addc3379851a41a25746ae6d23bb2480b155b68b297fd9d22c2e5fbc"
    "cfd1bed01575f63a0b966da3b19271a22c224c9dde51b0247b3a2bcac44763b4"
    "47c13c0cece78000a0d191f9379af2be052f21a9015a7220fb4c914499c9c676"
    "f4feba3bf9191292b92a82d7c4d0ec2562ab1761b595807b70359afb889ac1a3"
    "d314c00257684514d44c242994aec3a5ef4cc14e4173c2e324f624e2fd4dec15"
    "48e25db7c336ad807b446f9ab8ab32be23806c71a3bcbf77f0f131b572021e2a"
    "c9460e9f25aa142dc52b2dba54c10e8513c79b87a020a3013945a33aefb3894c"
    "54cf1b98055f373e877b10a6830d32bf4038420e8627677a4b5b57a5a73c039f"
    "76e505c656989efff5284b901125cb78"
)

_CLIENT_WINDOW_CLASS_RE = re.compile(
    r"^LHMW_FG_clientFGp(?P<game_id>.+)_i(?P<instance>\d+)$"
)


def _xor(data: bytes) -> bytes:
    return bytes(value ^ _XOR_TABLE[index % len(_XOR_TABLE)] for index, value in enumerate(data))


def parse_client_window_class(class_name: str):
    """Return ``(short_game_id, instance)`` for a Fever game window."""
    match = _CLIENT_WINDOW_CLASS_RE.fullmatch(str(class_name or ""))
    if not match:
        return None
    game_id = match.group("game_id").strip()
    if not game_id or "-" in game_id:
        return None
    return game_id, int(match.group("instance"))


if sys.platform == "win32":
    import ctypes.wintypes

    WM_COPYDATA = 0x004A
    HWND_MESSAGE = -3
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong,
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [
            ("dwData", ctypes.c_size_t),
            ("cbData", ctypes.c_uint),
            ("lpData", ctypes.c_void_p),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.wintypes.HINSTANCE),
            ("hIcon", ctypes.wintypes.HICON), ("hCursor", ctypes.wintypes.HICON),
            ("hbrBackground", ctypes.wintypes.HBRUSH), ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.wintypes.HICON),
        ]


class FeverIpcHost:
    CLASS_NAME = "LHMW_FG_Main"

    def __init__(self, on_ticket_request, logger):
        self.on_ticket_request = on_ticket_request
        self.logger = logger
        self.hwnd = None
        self._wndproc = None
        self._thread = None
        self._ready = threading.Event()
        self._error = ""

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self.hwnd:
            return True
        self._error = ""
        self._ready.clear()
        self._thread = threading.Thread(target=self._message_loop, name="fever-ipc", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._error:
            raise RuntimeError(self._error)
        return bool(self.hwnd)

    def _message_loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            user32.FindWindowExW.restype = ctypes.wintypes.HWND
            user32.FindWindowExW.argtypes = [
                ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                ctypes.c_wchar_p, ctypes.c_wchar_p,
            ]
            user32.CreateWindowExW.restype = ctypes.wintypes.HWND
            user32.CreateWindowExW.argtypes = [
                ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.wintypes.HWND, ctypes.wintypes.HMENU,
                ctypes.wintypes.HINSTANCE, ctypes.c_void_p,
            ]
            user32.DefWindowProcW.restype = ctypes.c_longlong
            user32.DefWindowProcW.argtypes = [
                ctypes.wintypes.HWND, ctypes.c_uint,
                ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
            ]
            kernel32.GetModuleHandleW.restype = ctypes.wintypes.HINSTANCE
            existing = user32.FindWindowExW(HWND_MESSAGE, None, self.CLASS_NAME, self.CLASS_NAME)
            if existing:
                raise RuntimeError("发烧平台正在运行，不能同时启用平台模拟")

            @WNDPROC
            def wndproc(hwnd, message, wparam, lparam):
                if message == WM_COPYDATA:
                    return self._on_copydata(wparam, lparam)
                if message == 0x0010:
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == 0x0002:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._wndproc = wndproc
            instance = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
            wc.hInstance = instance
            wc.lpszClassName = self.CLASS_NAME
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom and kernel32.GetLastError() != 1410:
                raise OSError(f"RegisterClassExW failed: {kernel32.GetLastError()}")
            self.hwnd = user32.CreateWindowExW(
                0, self.CLASS_NAME, self.CLASS_NAME, 0,
                0, 0, 0, 0, HWND_MESSAGE, 0, instance, 0,
            )
            if not self.hwnd:
                raise OSError(f"CreateWindowExW failed: {kernel32.GetLastError()}")
            user32.ChangeWindowMessageFilterEx(self.hwnd, WM_COPYDATA, 1, None)
            self._ready.set()
            message = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = str(exc)
            self.logger.exception("启动发烧平台 IPC 失败")
            self._ready.set()
        finally:
            self.hwnd = None

    def _on_copydata(self, sender_hwnd, lparam):
        cds = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
        raw = ctypes.string_at(cds.lpData, cds.cbData)
        return self._handle_packet(sender_hwnd, raw, int(cds.dwData))

    def _handle_packet(self, sender_hwnd, raw: bytes, dw_data: int = 0):
        if len(raw) < 12:
            self.logger.debug(
                "收到过短的 Fever IPC 包: "
                f"dwData={dw_data}, sender=0x{int(sender_hwnd):x}, bytes={len(raw)}"
            )
            return 0
        length, opcode, flags = struct.unpack("<iii", raw[:12])
        payload = _xor(raw[12:])
        packet_summary = (
            f"op={opcode}, dwData={dw_data}, sender=0x{int(sender_hwnd):x}, "
            f"declared={length}, flags={flags}, bytes={len(raw)}"
        )
        if opcode != 13:
            # Observe every currently unknown opcode without logging payloads.
            self.logger.debug(f"收到未知 Fever IPC: {packet_summary}")
        try:
            if opcode == 13 and len(payload) >= 260:
                self.logger.debug(f"收到 Fever ticket 请求: {packet_summary}")
                payload_instance = struct.unpack("<i", payload[:4])[0] - 101
                identifier = payload[4:260]
                sender = self._resolve_client_sender(sender_hwnd)
                if sender is None:
                    return 0
                process_id, game_id, instance = sender
                if payload_instance != instance:
                    self.logger.warning(
                        "忽略实例编号不一致的 Fever ticket 请求: "
                        f"class={instance}, payload={payload_instance}"
                    )
                    return 0
                accepted = self.on_ticket_request(
                    int(sender_hwnd), process_id, game_id, instance, identifier
                )
                return 0 if accepted is False else 1
        except Exception:
            self.logger.exception(f"处理游戏 IPC op{opcode} 失败")
        return 0

    def _resolve_client_sender(self, sender_hwnd):
        class_name = self._get_window_class_name(sender_hwnd)
        identity = parse_client_window_class(class_name)
        if identity is None:
            self.logger.warning(
                f"忽略未知 Fever 客户端窗口类: {class_name or '(empty)'}"
            )
            return None
        game_id, instance = identity
        process_id = self._get_window_process_id(sender_hwnd)
        return process_id, game_id, instance

    @staticmethod
    def _get_window_class_name(hwnd) -> str:
        if sys.platform != "win32":
            return ""
        buffer = ctypes.create_unicode_buffer(256)
        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [
            ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int,
        ]
        user32.GetClassNameW.restype = ctypes.c_int
        length = user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value[:length] if length else ""

    @staticmethod
    def _get_window_process_id(hwnd) -> int:
        if sys.platform != "win32":
            return 0
        process_id = ctypes.wintypes.DWORD()
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)

    def send_ticket(self, target_hwnd: int, login_channel: str, ticket: str) -> bool:
        if sys.platform != "win32" or not self.hwnd:
            return False
        payload = json.dumps(
            {"login_channel": login_channel or "netease", "ticket": ticket},
            ensure_ascii=False,
        ).encode("utf-8")
        packet = struct.pack("<iii", len(payload), 14, 0) + _xor(payload)
        buffer = ctypes.create_string_buffer(packet)
        cds = COPYDATASTRUCT(14, len(packet), ctypes.cast(buffer, ctypes.c_void_p))
        result = ctypes.c_size_t()
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.restype = ctypes.c_longlong
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.wintypes.HWND, ctypes.c_uint,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
        ]
        user32.SendMessageTimeoutW(
            target_hwnd, WM_COPYDATA, self.hwnd, ctypes.addressof(cds),
            0x0002, 5000, ctypes.byref(result),
        )
        return bool(result.value)

    def stop(self):
        if sys.platform == "win32" and self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, 0x0010, 0, 0)
