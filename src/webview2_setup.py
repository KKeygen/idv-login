from __future__ import annotations

import contextlib
from concurrent.futures import Future
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

MINIMUM_RUNTIME_VERSION = "136.0.3240.44"
BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
OFFLINE_URL = "https://bj.download.cycore.cn/cyejiaxiao/2026/10/22/18/026fd6c11b-d905-4cd1-9c3c-6fd7ae4237df.exe"
OFFLINE_SHA256 = "0625fb5c1e75be58a164e14f95aa58a3e5badb859b3d17c952385f3e4a9ff3e8"
_CLIENT_KEY = r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_lock = threading.Lock()
_pending = None


class RuntimeSetupError(RuntimeError):
    pass


class RuntimeRestartRequired(RuntimeSetupError):
    pass


def installed_version():
    if sys.platform != "win32":
        return ""
    import winreg
    minimum = tuple(map(int, MINIMUM_RUNTIME_VERSION.split(".")))
    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(hive, _CLIENT_KEY, 0, winreg.KEY_QUERY_VALUE | view) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
            if (isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+){3}", value)
                    and tuple(map(int, value.split("."))) >= minimum):
                return value
        except FileNotFoundError:
            continue
    return ""


def needs_install(status):
    hr = status.get("hresult")
    return (not status.get("available")
            and isinstance(hr, int) and (hr & 0xffffffff) in (0x80070002, 0x8007047E))


def _log(message):
    try:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            return
        path = Path(base) / "IDV-Login" / "installer.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


@contextlib.contextmanager
def _installation_lock():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, r"Local\IDV-Login.WebView2Setup")
    if not handle:
        raise RuntimeSetupError("无法等待 WebView2 安装任务，请稍后重试。")
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 1800 * 1000)
        acquired = result in (0, 0x80)
        if not acquired:
            raise RuntimeSetupError("另一个 WebView2 安装任务尚未完成，请稍后重试。")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _download(url, destination, expected_hash=None):
    digest = hashlib.sha256()
    deadline = time.monotonic() + 600
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=30) as response, destination.open("wb") as stream:
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeSetupError("WebView2 安装包下载超时，请检查网络后重试。")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            stream.write(chunk)
    if expected_hash and digest.hexdigest().lower() != expected_hash.lower():
        raise RuntimeSetupError("WebView2 安装包完整性校验失败，请重试。")


def _powershell(command, installer, timeout):
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = dict(os.environ, IDV_WEBVIEW2_INSTALLER=str(installer))
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", command],
            env=env, capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeSetupError("WebView2 安装任务未能按时结束，请稍后重试。") from None
    return result.returncode


def _verify_bootstrapper(path):
    command = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:IDV_WEBVIEW2_INSTALLER; "
        "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch "
        "'(^|,\\s*)O=Microsoft Corporation(,|$)') { exit 1 }"
    )
    if _powershell(command, path, 120) != 0:
        raise RuntimeSetupError("WebView2 在线安装程序的微软签名校验失败。")


_INSTALL = r"""
$ErrorActionPreference = 'Stop'
$process = $null
try {
    $process = Start-Process -FilePath $env:IDV_WEBVIEW2_INSTALLER -ArgumentList '/silent', '/install' -PassThru
    $handle = $process.Handle
    if ($process.WaitForExit(600000)) { exit $process.ExitCode }
    if (-not $process.HasExited) { $process.Kill() }
    if (-not $process.WaitForExit(10000)) { throw 'Installer termination unconfirmed' }
    exit 124
} catch {
    exit 125
} finally {
    if ($null -ne $process) { $process.Dispose() }
}
"""


def _run_installer(path):
    result = _powershell(_INSTALL, path, 630)
    _log(f"WebView2 installer exit={result}")
    if result == 124:
        raise RuntimeSetupError("WebView2 安装超时，共享更新任务可能仍在运行，请稍后重试。")
    if result == 125:
        raise RuntimeSetupError("WebView2 安装程序未能启动或确认结束，请稍后重试。")
    if result == 3010 and not installed_version():
        raise RuntimeRestartRequired("微软 WebView2 安装程序要求重启系统后才能使用运行库。")
    return result


def _prepare(progress):
    if sys.platform != "win32":
        raise RuntimeSetupError("WebView2 运行库仅支持 Windows。")
    with _installation_lock():
        version = installed_version()
        if version:
            return version
        with tempfile.TemporaryDirectory(prefix="idv-webview2-", ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            bootstrapper = root / "MicrosoftEdgeWebview2Setup.exe"
            progress("正在下载 Microsoft Edge WebView2 在线安装程序…")
            online_ready = False
            try:
                _download(BOOTSTRAPPER_URL, bootstrapper)
                _verify_bootstrapper(bootstrapper)
                online_ready = True
            except (OSError, RuntimeSetupError) as error:
                _log("WebView2 online download/verification failed: " + type(error).__name__)
            if online_ready:
                progress("正在安装 Microsoft Edge WebView2，最多等待 10 分钟…")
                _run_installer(bootstrapper)
            version = installed_version()
            if not version:
                architecture = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", "")
                if architecture.lower() != "amd64":
                    raise RuntimeSetupError("备用 WebView2 安装包仅支持 x64，请使用微软官方安装程序。")
                progress("正在下载备用 WebView2 离线安装包…")
                offline = root / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
                _download(OFFLINE_URL, offline, OFFLINE_SHA256)
                if not installed_version():
                    progress("正在安装备用 WebView2 运行库，最多等待 10 分钟…")
                    _run_installer(offline)
                version = installed_version()
            if not version:
                raise RuntimeSetupError("WebView2 尚未安装完成，请检查网络后重试；也可从微软官网安装。")
            _log("WebView2 dependency check passed")
            return version


def _prepare_worker(progress):
    with subprocess.Popen(
        [sys.executable, '-B', str(Path(__file__).resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        for line in process.stdout:
            progress(line.rstrip())
        result = process.wait()
    if result == 3010:
        raise RuntimeRestartRequired("微软 WebView2 安装程序要求重启系统后才能使用运行库。")
    if result != 0:
        raise RuntimeSetupError("准备 WebView2 运行库失败，请检查网络和安装权限后重试。")
    version = installed_version()
    if not version:
        raise RuntimeSetupError("WebView2 安装后仍未检测到可用运行库，请重试。")
    return version


def ensure_runtime(progress=None):
    global _pending
    progress = progress or (lambda message: None)
    with _lock:
        if _pending is not None:
            future, owner = _pending, False
        else:
            future = _pending = Future()
            owner = True
    if not owner:
        progress("正在等待 WebView2 运行库准备完成…")
        return future.result()
    try:
        version = installed_version() or _prepare_worker(progress)
        future.set_result(version)
        return version
    except Exception as error:
        _log("WebView2 preparation failed: " + type(error).__name__)
        failure = error if isinstance(error, RuntimeSetupError) else RuntimeSetupError(
            "准备 WebView2 运行库失败，请检查网络和安装权限后重试。")
        future.set_exception(failure)
        raise failure from None
    finally:
        with _lock:
            _pending = None


if __name__ == "__main__":
    def report(message):
        _log(message)
        try:
            if sys.stdout is not None:
                print(message, flush=True)
        except OSError:
            pass
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        _prepare(report)
    except RuntimeRestartRequired as error:
        report(str(error))
        sys.exit(3010)
    except Exception as error:
        _log("WebView2 preparation failed: " + type(error).__name__)
        report(str(error) if isinstance(error, RuntimeSetupError) else
               "准备 WebView2 运行库失败，请检查网络和安装权限后重试。")
        sys.exit(1)
