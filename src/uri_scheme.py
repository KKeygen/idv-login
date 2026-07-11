# coding=UTF-8
"""
Copyright (c) 2026 KKeygen

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
from __future__ import annotations

import json
import os
import sys
import threading

from logutil import setup_logger

logger = setup_logger()

# Named-pipe / socket path used for single-instance signaling.
_PIPE_NAME = r"\\.\pipe\idvlogin_uri_signal"
_SOCKET_PATH_UNIX = "/tmp/idvlogin_uri_signal.sock"


def register_uri_scheme(executable_path: str | None = None) -> bool:
    """Register the ``idvlogin://`` URI scheme so that the OS opens our
    application when a user or WebView navigates to an ``idvlogin://``
    URL (e.g. from a QR-code redirect).

    On Windows this adds entries under
    ``HKEY_CURRENT_USER\\Software\\Classes\\idvlogin``.

    The registration is intentionally under HKCU so that no admin
    rights are needed and the entry is removed when the user's
    profile is cleaned up.

    Returns ``True`` on success.
    """
    if sys.platform != "win32":
        logger.debug("URI Scheme 注册仅支持 Windows 平台")
        return False

    if executable_path is None:
        if getattr(sys, "frozen", False):
            executable_path = sys.executable
        else:
            executable_path = os.path.abspath(sys.argv[0])
            # For .py/.pyc/.pyw scripts, prepend the interpreter
            if executable_path.endswith((".py", ".pyc", ".pyw")):
                executable_path = f'{sys.executable}" "{executable_path}'

    try:
        import winreg

        base = r"Software\Classes\idvlogin"

        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, base)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:IDV Login Protocol")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(key)

        cmd_key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, base + r"\shell\open\command"
        )
        winreg.SetValueEx(
            cmd_key,
            "",
            0,
            winreg.REG_SZ,
            f'"{executable_path}" --uri "%1"',
        )
        winreg.CloseKey(cmd_key)

        logger.info("已注册 idvlogin:// URI Scheme")
        return True
    except Exception as e:
        logger.warning(f"注册 URI Scheme 失败: {e}")
        return False


def unregister_uri_scheme() -> bool:
    """Remove the ``idvlogin://`` URI scheme registration."""
    if sys.platform != "win32":
        return False

    try:
        import winreg

        base = r"Software\Classes\idvlogin"
        # Delete keys bottom-up
        for sub in (
            base + r"\shell\open\command",
            base + r"\shell\open",
            base + r"\shell",
            base,
        ):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except FileNotFoundError:
                pass
        logger.info("已注销 idvlogin:// URI Scheme")
        return True
    except Exception as e:
        logger.warning(f"注销 URI Scheme 失败: {e}")
        return False


def parse_uri(uri: str) -> dict:
    """Parse an ``idvlogin://`` URI into a dict of parameters.

    Example::

        parse_uri("idvlogin://open?game_id=h55")
        # => {"action": "open", "game_id": "h55"}
    """
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(uri)
    params: dict = {}
    params["action"] = parsed.hostname or parsed.netloc or "open"
    qs = parse_qs(parsed.query, keep_blank_values=True)
    for k, v in qs.items():
        params[k] = v[0] if len(v) == 1 else v
    return params


# ------------------------------------------------------------------
# Single-instance signaling
# ------------------------------------------------------------------

def signal_running_instance(
    game_id: str, action: str = "open", installation_id: str = ""
) -> bool:
    """Try to send *game_id* to an already-running instance.

    On Windows we use a named pipe; on Unix a domain socket.
    Returns ``True`` if a running instance was found and signaled.
    """
    payload = json.dumps({
        "game_id": game_id,
        "action": action,
        "installation_id": installation_id,
    }).encode("utf-8")
    if sys.platform == "win32":
        return _signal_via_named_pipe(payload)
    else:
        return _signal_via_unix_socket(payload)


def start_uri_listener(callback):
    """Start a background thread that listens for incoming URI signals.

    *callback* is called with ``(action: str, game_id: str)`` whenever another
    process sends a signal via :func:`signal_running_instance`.
    """
    t = threading.Thread(
        target=_listener_thread,
        args=(callback,),
        name="uri-listener",
        daemon=True,
    )
    t.start()
    return t


# -- Windows named-pipe helpers ----------------------------------------

def _signal_via_named_pipe(payload: bytes) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        INVALID_HANDLE = ctypes.c_void_p(-1).value

        # 设置 argtypes 和 restype 确保 ctypes 正确处理参数和返回值
        # restype 必须设为 c_void_p，否则 64 位 Python 下
        # 默认 c_int 返回 -1，与 INVALID_HANDLE (0xFFFFFFFFFFFFFFFF) 不等，
        # 导致无监听器时也误判为连接成功。
        ctypes.windll.kernel32.CreateFileW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
            wt.DWORD, wt.DWORD, wt.HANDLE
        ]
        ctypes.windll.kernel32.CreateFileW.restype = ctypes.c_void_p
        ctypes.windll.kernel32.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
            ctypes.POINTER(wt.DWORD), ctypes.c_void_p
        ]
        ctypes.windll.kernel32.WriteFile.restype = wt.BOOL
        ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.CloseHandle.restype = wt.BOOL

        handle = ctypes.windll.kernel32.CreateFileW(
            _PIPE_NAME, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
        )
        if handle == INVALID_HANDLE:
            return False  # No listener running
        try:
            written = wt.DWORD(0)
            ctypes.windll.kernel32.WriteFile(
                handle, payload, len(payload), ctypes.byref(written), None
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _signal_via_unix_socket(payload: bytes) -> bool:
    import socket as _socket
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(_SOCKET_PATH_UNIX)
        sock.sendall(payload)
        sock.close()
        return True
    except Exception:
        return False


def _listener_thread(callback):
    if sys.platform == "win32":
        _listener_named_pipe(callback)
    else:
        _listener_unix_socket(callback)


def _listener_named_pipe(callback):
    """Listen on a Windows named pipe for incoming URI signals."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        import time

        PIPE_ACCESS_INBOUND = 0x00000001
        PIPE_TYPE_BYTE = 0x00000000
        PIPE_WAIT = 0x00000000
        INVALID_HANDLE = ctypes.c_void_p(-1).value
        ERROR_PIPE_BUSY = 231

        # 构建安全描述符：NULL DACL 允许所有用户（含非提权进程）连接管道
        # 这对于 URI scheme 调用至关重要：当主程序以管理员身份运行时，
        # 浏览器触发的进程通常是非提权的，需要 NULL DACL 才能连接管道。
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL),
            ]

        # SECURITY_DESCRIPTOR 在 64 位系统上需要约 40 字节（包含指针字段）
        # 使用 64 字节以确保在所有平台上安全
        sd = ctypes.c_buffer(64)
        ctypes.windll.advapi32.InitializeSecurityDescriptor(
            ctypes.byref(sd), 1  # SECURITY_DESCRIPTOR_REVISION
        )
        ctypes.windll.advapi32.SetSecurityDescriptorDacl(
            ctypes.byref(sd), True, None, False  # NULL DACL = allow all
        )
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = ctypes.addressof(sd)
        sa.bInheritHandle = False

        # 设置 argtypes 和 restype 确保 ctypes 正确处理参数和返回值
        ctypes.windll.kernel32.CreateNamedPipeW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD,
            wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p
        ]
        ctypes.windll.kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
        ctypes.windll.kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ctypes.windll.kernel32.ConnectNamedPipe.restype = wt.BOOL
        ctypes.windll.kernel32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.DisconnectNamedPipe.restype = wt.BOOL

        retry_count = 0
        max_retries = 5

        while True:
            handle = ctypes.windll.kernel32.CreateNamedPipeW(
                _PIPE_NAME,
                PIPE_ACCESS_INBOUND,
                PIPE_TYPE_BYTE | PIPE_WAIT,
                1,     # max instances
                4096,  # out buffer
                4096,  # in buffer
                0,     # default timeout
                ctypes.byref(sa),
            )
            if handle == INVALID_HANDLE:
                err = ctypes.windll.kernel32.GetLastError()
                if err == ERROR_PIPE_BUSY and retry_count < max_retries:
                    # 管道正忙（可能是之前的进程未正常清理），等待后重试
                    retry_count += 1
                    logger.debug(f"命名管道忙，等待重试 ({retry_count}/{max_retries})")
                    time.sleep(0.5 * retry_count)  # 指数退避
                    continue
                logger.warning(f"创建命名管道失败，错误码: {err}")
                return
            
            # 管道创建成功，重置重试计数器
            retry_count = 0

            connected = ctypes.windll.kernel32.ConnectNamedPipe(handle, None)
            if connected or ctypes.windll.kernel32.GetLastError() == 535:  # ERROR_PIPE_CONNECTED
                try:
                    buf = ctypes.create_string_buffer(4096)
                    read = wt.DWORD(0)
                    ctypes.windll.kernel32.ReadFile(
                        handle, buf, 4096, ctypes.byref(read), None
                    )
                    data = buf.raw[: read.value]
                    if data:
                        msg = json.loads(data.decode("utf-8", errors="replace"))
                        game_id = msg.get("game_id", "")
                        action = msg.get("action", "open")
                        installation_id = msg.get("installation_id", "")
                        callback(action, game_id, installation_id)
                except Exception:
                    logger.debug("读取命名管道数据失败", exc_info=True)

            ctypes.windll.kernel32.DisconnectNamedPipe(handle)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("命名管道监听线程异常退出", exc_info=True)


def _listener_unix_socket(callback):
    """Listen on a Unix domain socket for incoming URI signals."""
    import socket as _socket
    try:
        if os.path.exists(_SOCKET_PATH_UNIX):
            os.unlink(_SOCKET_PATH_UNIX)
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.bind(_SOCKET_PATH_UNIX)
        sock.listen(1)

        while True:
            conn, _ = sock.accept()
            try:
                data = conn.recv(4096)
                if data:
                    msg = json.loads(data.decode("utf-8", errors="replace"))
                    game_id = msg.get("game_id", "")
                    action = msg.get("action", "open")
                    installation_id = msg.get("installation_id", "")
                    callback(action, game_id, installation_id)
            except Exception:
                logger.debug("读取Unix socket数据失败", exc_info=True)
            finally:
                conn.close()
    except Exception:
        logger.debug("Unix socket监听线程异常退出", exc_info=True)
    finally:
        try:
            os.unlink(_SOCKET_PATH_UNIX)
        except Exception:
            pass
