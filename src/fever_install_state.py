"""Read the Fever download core's installed state without writing game files."""

import json
import ntpath
import os
import re
import stat

_STATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}_app\.state\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\Z")
_CONTENT_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_MAX_STATE_BYTES = 4 * 1024 * 1024
_MAX_DIR_ENTRIES = 256
_MAX_STATE_FILES = 16


def normalized_path(path):
    return os.path.normcase(os.path.abspath(path))


def confirmed_install_root(executable, startup_path):
    """The configured relative startup must identify the exact selected file."""
    if not isinstance(startup_path, str) or not isinstance(executable, str):
        return ""
    if not os.path.isfile(executable) or ntpath.splitdrive(startup_path)[0]:
        return ""
    parts = startup_path.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        return ""
    full_exe = normalized_path(executable)
    root = full_exe
    for _ in parts:
        root = os.path.dirname(root)
    if normalized_path(os.path.join(root, *parts)) != full_exe:
        return ""
    root = normalized_path(os.path.realpath(root))
    real_exe = normalized_path(os.path.realpath(full_exe))
    if (os.path.dirname(root) == root or not os.path.isdir(root)
            or normalized_path(os.path.join(root, *parts)) != real_exe):
        return ""
    return root


def canonical_content_id(value):
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = str(value)
        if _CONTENT_ID.fullmatch(value):
            return value
    return ""


def _read_one_state(path):
    # Refuse links, devices, and oversized input. In particular, do not read
    # other game files just to decide which version is installed.
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("Invalid app state file")
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    )
    with os.fdopen(os.open(path, flags), "rb") as handle:
        current = os.fstat(handle.fileno())
        if not stat.S_ISREG(current.st_mode) or not os.path.samestat(info, current):
            raise ValueError("App state changed while opening")
        raw = handle.read(_MAX_STATE_BYTES + 1)
    if len(raw) > _MAX_STATE_BYTES:
        raise ValueError("App state exceeds limit")
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("Invalid app state")
    installed = state.get("installed_contents")
    staged = state.get("staged_contents")
    installed = {} if installed is None else installed
    staged = {} if staged is None else staged
    if not isinstance(installed, dict) or not isinstance(staged, dict):
        raise ValueError("Invalid app state contents")
    return installed, staged


def read_install_states(root, content_ids, cancel_check=None):
    """Return (status, installed_version) for each known app content ID.

    Do not assume an IDV shortcut, distribution ID, or registry key is the
    Oracle filename gameId. Only uniquely matching, in-root state is usable.
    A staged match can identify the content, but is never a version source.
    """
    ids = {value for raw in content_ids if (value := canonical_content_id(raw))}
    default = {value: ("missing", "") for value in ids}
    folder = os.path.join(root, ".dlstorage", "downloading")
    if not ids or not root:
        return default
    if normalized_path(os.path.realpath(folder)) != normalized_path(folder):
        return {value: ("unreadable", "") for value in ids}
    matches = {value: [] for value in ids}
    state_count = 0
    try:
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if cancel_check:
                    cancel_check()
                if index >= _MAX_DIR_ENTRIES:
                    raise ValueError("Too many download state directory entries")
                if not _STATE_NAME.fullmatch(entry.name):
                    continue
                state_count += 1
                if state_count > _MAX_STATE_FILES:
                    raise ValueError("Too many app state files")
                installed, staged = _read_one_state(entry.path)
                for value in ids:
                    if value in installed:
                        item = installed[value]
                        version = item.get("Version") if isinstance(item, dict) else None
                        matches[value].append(
                            ("installed", version) if isinstance(version, str)
                            and _VERSION.fullmatch(version) else ("no_version", "")
                        )
                    elif value in staged:
                        matches[value].append(("staged", ""))
    except InterruptedError:
        raise
    except FileNotFoundError:
        return default
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {value: ("unreadable", "") for value in ids}
    return {
        value: found[0] if len(found) == 1 else ("ambiguous", "") if found else default[value]
        for value, found in matches.items()
    }
