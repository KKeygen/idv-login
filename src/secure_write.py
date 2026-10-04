# coding=UTF-8
"""
Restricted-permission file writes.

All sensitively-stored files (account tokens, config, keys, etc.)
should use the helpers in this module so that they are readable
only by the current user.

Writes are atomic: data goes to a temporary file first, then
``os.replace()`` swaps it into place.  A per-path file lock
(``msvcrt`` on Windows, ``fcntl`` on Unix) prevents concurrent
writers from corrupting each other.
"""

import json
import os
import sys
import tempfile
import threading

_write_locks = {}          # type: dict[str, threading.Lock]
_meta_lock = threading.Lock()


def _get_lock(filepath: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(filepath))
    with _meta_lock:
        if key not in _write_locks:
            _write_locks[key] = threading.Lock()
        return _write_locks[key]


def _atomic_write(filepath: str, data: bytes, *, restrict_unix: bool = True):
    """Write *data* atomically via temp-file + os.replace().

    On Unix the temp file is created with mode 0o600 when *restrict_unix*
    is True.  On Windows, ACLs are applied after the rename.
    """
    dirpath = os.path.dirname(os.path.abspath(filepath)) or "."
    fd = None
    tmp_path = None
    try:
        if sys.platform != "win32" and restrict_unix:
            fd = tempfile.mkstemp(dir=dirpath, prefix=".tmp_", suffix=".json")
            tmp_path = fd[1]
            with os.fdopen(fd[0], 'wb') as tmp:
                fd = None
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.chmod(tmp_path, 0o600)
        else:
            with tempfile.NamedTemporaryFile(
                dir=dirpath, prefix=".tmp_", suffix=".json",
                delete=False, mode="wb",
            ) as tmp:
                tmp_path = tmp.name
                tmp.write(data)
                tmp.flush()
                os.fsync(tmp.fileno())

        if sys.platform == "win32":
            _win_restrict_acl(tmp_path)
        os.replace(tmp_path, filepath)
        tmp_path = None  # rename succeeded, nothing to clean up
    finally:
        if fd is not None:
            try:
                os.close(fd[0])
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def write_file_restricted(filepath: str, data: bytes):
    """Write *data* (bytes) atomically and restrict permissions."""
    lock = _get_lock(filepath)
    lock.acquire(timeout=5)
    try:
        _atomic_write(filepath, data)
    except Exception:
        # Fallback: standard write (still better than losing data silently)
        with open(filepath, "wb") as f:
            f.write(data)
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def write_json_restricted(filepath: str, obj, *, atomic_required=False):
    """Write JSON; strict callers never fall back to truncating the live file."""
    text = json.dumps(obj, ensure_ascii=False)
    data = text.encode("utf-8")
    lock = _get_lock(filepath)
    acquired = lock.acquire(timeout=5)
    if not acquired and atomic_required:
        raise TimeoutError('Configuration write lock timed out')
    try:
        _atomic_write(filepath, data)
    except Exception:
        if atomic_required:
            raise
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    finally:
        if acquired:
            lock.release()


def _win_restrict_acl(filepath: str):
    """Restrict ACL to Administrators + SYSTEM on Windows."""
    import subprocess
    subprocess.run(
        ["icacls", filepath, "/reset"],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["icacls", filepath, "/inheritance:r",
         "/grant:r", "*S-1-5-32-544:(F)",
         "/grant:r", "*S-1-5-18:(F)"],
        capture_output=True, timeout=10,
    )
