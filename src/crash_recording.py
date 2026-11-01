# coding=UTF-8

import faulthandler
import os
import sys
import time


_APP_DIRECTORY = "IdentityV-login-helper"
_PREFIX = "native-fault-"
_KEEP_LOGS = 20
_KEEP_SECONDS = 7 * 24 * 60 * 60
_fault_fd = None
_fault_path = None


def _log_directories():
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local and os.path.isabs(local):
            yield os.path.join(local, _APP_DIRECTORY, "crashes")
        if os.path.isabs(home):
            yield os.path.join(home, "AppData", "Local", _APP_DIRECTORY, "crashes")
    elif sys.platform == "darwin":
        if os.path.isabs(home):
            yield os.path.join(home, "Library", "Logs", _APP_DIRECTORY)
    else:
        state = os.environ.get("XDG_STATE_HOME")
        if state and os.path.isabs(state):
            yield os.path.join(state, _APP_DIRECTORY, "crashes")
        if os.path.isabs(home):
            yield os.path.join(home, ".local", "state", _APP_DIRECTORY, "crashes")

    import tempfile
    yield tempfile.mkdtemp(prefix="idv-native-fault-")


def _lock_posix_log(fd):
    if os.name == "posix":
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _prune_old_logs(directory):
    try:
        with os.scandir(directory) as entries:
            logs = sorted(
                (entry.name for entry in entries
                 if entry.name.startswith(_PREFIX) and entry.name.endswith(".log")
                 and entry.is_file(follow_symlinks=False)),
                reverse=True,
            )
        cutoff = time.time() - _KEEP_SECONDS
        for name in logs[_KEEP_LOGS:]:
            path = os.path.join(directory, name)
            fd = None
            try:
                if os.stat(path, follow_symlinks=False).st_mtime >= cutoff:
                    continue
                if os.name == "posix":
                    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
                    _lock_posix_log(fd)
                os.unlink(path)
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)
    except OSError:
        pass


def enable_crash_recording():
    global _fault_fd, _fault_path
    if _fault_fd is not None:
        return _fault_path

    fd = None
    path = None
    try:
        for directory in _log_directories():
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                name = f"{_PREFIX}{time.time_ns():020d}-{os.getpid()}.log"
                path = os.path.join(directory, name)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
                _lock_posix_log(fd)
                header = (
                    "IdentityV-login-helper native-fault recorder\n"
                    f"Started UTC: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
                    f"PID: {os.getpid()}; Python: {sys.version_info.major}."
                    f"{sys.version_info.minor}.{sys.version_info.micro}; OS: {sys.platform}\n"
                    "A header alone is not evidence of a crash. Windows exception\n"
                    "records can also describe subsequently handled exceptions.\n\n"
                ).encode("ascii")
                while header:
                    written = os.write(fd, header)
                    if written == 0:
                        raise OSError("No progress writing fault-log header")
                    header = header[written:]
                break
            except OSError:
                if fd is not None:
                    os.close(fd)
                    fd = None
        if fd is None:
            return None

        try:
            faulthandler.enable(file=fd, all_threads=True)
        except (OSError, RuntimeError):
            faulthandler.disable()
            os.close(fd)
            return None
        _fault_fd, _fault_path = fd, path
        _prune_old_logs(os.path.dirname(path))
        return path
    except OSError:
        return None
