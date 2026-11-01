from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


class BrowserProfileError(RuntimeError):
    pass


_MARKER = '.idv-browser-profile.json'
_MAGIC = {'owner': 'idv-login', 'kind': 'webview2-login', 'version': 1}
_ENTROPY = b'idv-login.webview2.cookies.v1'


def validate_profile_path(value):
    path = Path(os.path.abspath(os.fspath(value)))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BrowserProfileError('登录资料目录不能是符号链接或重解析点。')
    return path


def _atomic(path, value):
    validate_profile_path(path)
    fd, name = tempfile.mkstemp(prefix='.idv-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _restrict(path):
    if sys.platform != 'win32':
        os.chmod(path, 0o700)
        return
    import win32api
    import win32con
    import win32security
    import ntsecuritycon
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        acl = win32security.ACL()
        flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
    finally:
        token.Close()


class ProfileLease:
    def __init__(self, path, persistent=True):
        self.persistent = bool(persistent)
        self.path = validate_profile_path(path) if persistent else Path(tempfile.mkdtemp(prefix='idv-login-'))
        self._lock = None

    def prepare(self):
        p = self.path
        validate_profile_path(p)
        if p.exists():
            marker = p / _MARKER
            if marker.exists():
                validate_profile_path(marker)
                if marker.stat().st_size > 1024 or json.loads(marker.read_text('utf-8')) != _MAGIC:
                    raise BrowserProfileError('登录资料目录的归属标记无效。')
            elif any(p.iterdir()):
                raise BrowserProfileError('不会使用已有的普通浏览器资料。请选择新的独立登录目录。')
        p.mkdir(parents=True, exist_ok=True)
        _restrict(p)
        lockpath = validate_profile_path(p / '.idv-login.lock')
        self._lock = open(lockpath, 'a+b')
        try:
            self._lock.seek(0)
            if os.fstat(self._lock.fileno()).st_size == 0:
                self._lock.write(b'0'); self._lock.flush(); self._lock.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close(); self._lock = None
            raise BrowserProfileError('该账号的登录浏览器仍在使用，请先关闭后重试。') from None
        _atomic(p / _MARKER, json.dumps(_MAGIC).encode())
        return p

    @property
    def needs_cookie_clear(self):
        return (self.path / '.idv-clear-pending').exists()

    def mark_cookies_cleared(self):
        _atomic(self.path / '.idv-clear-pending', b'1')
        with contextlib.suppress(FileNotFoundError):
            (self.path / '.idv-session-cookies.dpapi').unlink()

    def confirm_cookies_cleared(self):
        with contextlib.suppress(FileNotFoundError):
            (self.path / '.idv-clear-pending').unlink()

    def load_cookies(self):
        path = validate_profile_path(self.path / '.idv-session-cookies.dpapi')
        if not self.persistent or self.needs_cookie_clear or not path.exists():
            return []
        if sys.platform != 'win32':
            raise BrowserProfileError('持久登录资料仅支持 Windows DPAPI。')
        if path.stat().st_size > 16 * 1024 * 1024:
            raise BrowserProfileError('登录状态文件无效，请强制退登后重试。')
        import win32crypt
        try:
            plain = win32crypt.CryptUnprotectData(path.read_bytes(), _ENTROPY, None, None, 1)[1]
            value = json.loads(plain)
            if not isinstance(value, list):
                raise ValueError
            return value
        except Exception:
            raise BrowserProfileError('无法解密此 Windows 用户的登录状态，请强制退登后重试。') from None

    def save_cookies(self, cookies):
        if not self.persistent or self.needs_cookie_clear:
            return
        if sys.platform != 'win32':
            raise BrowserProfileError('持久登录资料仅支持 Windows DPAPI。')
        import win32crypt
        plain = json.dumps(cookies, ensure_ascii=False).encode('utf-8')
        encrypted = win32crypt.CryptProtectData(plain, 'IDV login cookies', _ENTROPY, None, None, 1)
        _atomic(self.path / '.idv-session-cookies.dpapi', encrypted)

    def close(self):
        if self._lock:
            self._lock.close(); self._lock = None
        if not self.persistent:
            shutil.rmtree(self.path, ignore_errors=True)
