from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import re
import sys
import winreg
from comtypes import COMObject, COMError
import webview2_com_interfaces as I

from webview2_setup import MINIMUM_RUNTIME_VERSION as MINIMUM_VERSION
E_FAIL = -2147467259
E_ACCESSDENIED = -2147024891
POLICY_BLOCKED = -2147023636
ole32 = C.WinDLL('ole32')
ole32.CoTaskMemAlloc.argtypes = [C.c_size_t]
ole32.CoTaskMemAlloc.restype = C.c_void_p
ole32.CoTaskMemFree.argtypes = [C.c_void_p]
ole32.CoTaskMemFree.restype = None


def check(hr):
    value = C.c_int32(hr).value
    if value < 0:
        raise COMError(value, 'WebView2 operation failed', None)


def take_string(address):
    if not address:
        return ''
    try:
        return C.wstring_at(address)
    finally:
        ole32.CoTaskMemFree(address)


def _string_get(value):
    def get(self, this, out):
        if not out:
            return -2147467261
        data = C.create_unicode_buffer(value)
        ptr = ole32.CoTaskMemAlloc(C.sizeof(data))
        if not ptr:
            return -2147024882
        C.memmove(ptr, data, C.sizeof(data)); out[0] = ptr
        return 0
    return get


def _scalar_get(value):
    def get(self, this, out):
        if not out:
            return -2147467261
        out[0] = value
        return 0
    return get


def _deny(self, this, value):
    return E_ACCESSDENIED


class EnvironmentOptions(COMObject):
    _com_interfaces_ = [I.ICoreWebView2EnvironmentOptions, I.ICoreWebView2EnvironmentOptions2,
                        I.ICoreWebView2EnvironmentOptions7]
    get_AdditionalBrowserArguments = _string_get('--no-proxy-server')
    get_Language = _string_get('zh-CN')
    get_TargetCompatibleBrowserVersion = _string_get(MINIMUM_VERSION)
    get_AllowSingleSignOnUsingOSPrimaryAccount = _scalar_get(0)
    get_ExclusiveUserDataFolderAccess = _scalar_get(1)
    get_ChannelSearchKind = _scalar_get(0)
    get_ReleaseChannels = _scalar_get(1)
    put_AdditionalBrowserArguments = put_Language = put_TargetCompatibleBrowserVersion = _deny
    put_AllowSingleSignOnUsingOSPrimaryAccount = put_ExclusiveUserDataFolderAccess = _deny
    put_ChannelSearchKind = put_ReleaseChannels = _deny


def reject_runtime_overrides():
    names = ('BROWSER_EXECUTABLE_FOLDER', 'USER_DATA_FOLDER', 'ADDITIONAL_BROWSER_ARGUMENTS',
             'RELEASE_CHANNEL_PREFERENCE', 'RELEASE_CHANNELS', 'CHANNEL_SEARCH_KIND',
             'PIPE_FOR_SCRIPT_DEBUGGER', 'WAIT_FOR_SCRIPT_DEBUGGER')
    if any(os.environ.get('WEBVIEW2_' + name) for name in names):
        check(POLICY_BLOCKED)
    identities = [Path(sys.executable).name, '*']
    shell = C.WinDLL('shell32')
    explicit = C.c_void_p()
    shell.GetCurrentProcessExplicitAppUserModelID.argtypes = [C.POINTER(C.c_void_p)]
    shell.GetCurrentProcessExplicitAppUserModelID.restype = C.c_int32
    if shell.GetCurrentProcessExplicitAppUserModelID(C.byref(explicit)) >= 0:
        value = take_string(explicit.value)
        if value: identities.append(value)
    kernel = C.WinDLL('kernel32')
    get_id = kernel.GetCurrentApplicationUserModelId
    get_id.argtypes = [C.POINTER(W.UINT), W.LPWSTR]
    get_id.restype = W.LONG
    length = W.UINT()
    result = get_id(C.byref(length), None)
    if result == 122:
        if not 0 < length.value <= 32768: check(E_FAIL)
        buf = C.create_unicode_buffer(length.value)
        result = get_id(C.byref(length), buf)
        if result: check(0x80070000 | result)
        if buf.value: identities.append(buf.value)
    elif result not in (0, 15703):
        check(0x80070000 | result)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            for option in ('BrowserExecutableFolder', 'ChannelSearchKind', 'ReleaseChannels',
                           'AdditionalBrowserArguments', 'UserDataFolder'):
                try:
                    key = winreg.OpenKey(hive, 'Software\\Policies\\Microsoft\\Edge\\WebView2\\' + option,
                                         0, winreg.KEY_QUERY_VALUE | view)
                except FileNotFoundError:
                    continue
                with key:
                    query = C.WinDLL('advapi32').RegQueryValueExW
                    query.argtypes = [W.HKEY,W.LPCWSTR,C.c_void_p,C.c_void_p,C.c_void_p,C.POINTER(W.DWORD)]
                    query.restype = W.LONG
                    for name in identities:
                        if name == '*' and option == 'UserDataFolder': continue
                        size = W.DWORD()
                        result = query(int(key), name, None, None, None, C.byref(size))
                        if result in (0,234): check(POLICY_BLOCKED)
                        if result != 2: check(0x80070000 | result)


_loader = None

def load_loader():
    global _loader
    if _loader is None:
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        path = root / 'native/webview2/WebView2Loader.dll'
        dll = C.WinDLL(str(path), winmode=0x100 | 0x800)
        dll.CreateCoreWebView2EnvironmentWithOptions.argtypes = [C.c_wchar_p,C.c_wchar_p,
            C.POINTER(I.ICoreWebView2EnvironmentOptions),C.POINTER(I.ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)]
        dll.CreateCoreWebView2EnvironmentWithOptions.restype = C.c_int32
        dll.GetAvailableCoreWebView2BrowserVersionStringWithOptions.argtypes = [C.c_wchar_p,
            C.POINTER(I.ICoreWebView2EnvironmentOptions),C.POINTER(C.c_void_p)]
        dll.GetAvailableCoreWebView2BrowserVersionStringWithOptions.restype = C.c_int32
        dll.CompareBrowserVersions.argtypes = [C.c_wchar_p,C.c_wchar_p,C.POINTER(C.c_int)]
        dll.CompareBrowserVersions.restype = C.c_int32
        _loader = dll
    return _loader


def detect_runtime():
    reject_runtime_overrides()
    loader = load_loader(); options = EnvironmentOptions(); out = C.c_void_p()
    options_pointer = options.QueryInterface(I.ICoreWebView2EnvironmentOptions)
    hr = loader.GetAvailableCoreWebView2BrowserVersionStringWithOptions(None, options_pointer, C.byref(out))
    version = take_string(out.value)
    check(hr)
    if not re.fullmatch(r'\d+(?:\.\d+){3}',version): check(0x80070032)
    comparison=C.c_int()
    check(loader.CompareBrowserVersions(version,MINIMUM_VERSION,C.byref(comparison)))
    return version, C.c_int32(0x8007047e).value if comparison.value < 0 else 0
