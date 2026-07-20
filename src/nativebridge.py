# coding=UTF-8
"""Browser-safe native capabilities exposed through the local HTTP API.

The Vue UI can run either inside QtWebEngine or in the user's browser.  This
module therefore does not depend on QWebChannel: long-running native actions
return a task id and are observed through the same local request handler used
by both frontends.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import uuid
from typing import Any, Dict

import app_state


class NativeTaskRegistry:
    """Small in-memory task registry for native dialogs and background work."""

    _lock = threading.RLock()
    _tasks: Dict[str, dict] = {}
    _ttl_seconds = 30 * 60

    @classmethod
    def create(cls, kind: str) -> str:
        task_id = str(uuid.uuid4())
        now = int(time.time())
        with cls._lock:
            cls._cleanup_locked(now)
            cls._tasks[task_id] = {
                "task_id": task_id,
                "kind": kind,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }
        return task_id

    @classmethod
    def update(cls, task_id: str, **values: Any) -> None:
        with cls._lock:
            task = cls._tasks.get(task_id)
            if task is None:
                return
            task.update(values)
            task["updated_at"] = int(time.time())

    @classmethod
    def finish(cls, task_id: str, *, success: bool, **values: Any) -> None:
        cls.update(
            task_id,
            status="done",
            success=bool(success),
            **values,
        )

    @classmethod
    def get(cls, task_id: str) -> dict | None:
        now = int(time.time())
        with cls._lock:
            cls._cleanup_locked(now)
            task = cls._tasks.get(task_id)
            result = dict(task) if task else None
        if not result:
            return None
        status_file = result.get("status_file")
        if status_file:
            try:
                with open(status_file, "r", encoding="utf-8") as status_handle:
                    disk_status = json.load(status_handle)
                if isinstance(disk_status, dict):
                    result.update(disk_status)
                    if disk_status.get("status") == "done":
                        cls.update(task_id, **disk_status)
            except (FileNotFoundError, OSError, ValueError, TypeError):
                pass
        return result

    @classmethod
    def has_pending_installation(cls, installation_id: str) -> bool:
        target = str(installation_id or "")
        with cls._lock:
            task_ids = [
                task_id for task_id, task in cls._tasks.items()
                if str(task.get("installation_id") or "") == target
            ]
        return any(
            (cls.get(task_id) or {}).get("status") == "pending"
            for task_id in task_ids
        )

    @classmethod
    def _cleanup_locked(cls, now: int) -> None:
        expired = [
            task_id
            for task_id, task in cls._tasks.items()
            if task.get("status") == "done"
            and now - int(task.get("updated_at", now)) > cls._ttl_seconds
        ]
        for task_id in expired:
            task = cls._tasks.pop(task_id, None) or {}
            status_file = task.get("status_file")
            if status_file:
                try:
                    os.remove(status_file)
                except OSError:
                    pass


def capabilities() -> dict:
    return {
        "success": True,
        "schema_version": 1,
        "pick_directory": True,
        "pick_executable": True,
        "inspect_path": True,
        "task_polling": True,
        "download_progress": True,
        "platform": sys.platform,
    }


def _nearest_existing_path(path: str) -> str:
    candidate = os.path.abspath(path or os.path.expanduser("~"))
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return ""
        candidate = parent
    return candidate


def inspect_path(path: str) -> dict:
    raw_path = str(path or "").strip()
    normalized = os.path.normpath(os.path.abspath(os.path.expanduser(raw_path))) if raw_path else ""
    exists = bool(normalized and os.path.exists(normalized))
    is_file = bool(exists and os.path.isfile(normalized))
    is_directory = bool(exists and os.path.isdir(normalized))
    probe_path = normalized if is_directory else os.path.dirname(normalized)
    existing_probe = _nearest_existing_path(probe_path)

    disk_total = 0
    disk_used = 0
    disk_free = 0
    if existing_probe:
        try:
            usage = shutil.disk_usage(existing_probe)
            disk_total, disk_used, disk_free = usage.total, usage.used, usage.free
        except OSError:
            pass

    writable = False
    if exists:
        writable = os.access(normalized, os.W_OK)
    elif existing_probe:
        writable = os.access(existing_probe, os.W_OK)

    root = os.path.splitdrive(normalized)[0] + os.sep if normalized else ""
    is_drive_root = bool(root and os.path.normcase(normalized) == os.path.normcase(os.path.normpath(root)))

    return {
        "success": True,
        "path": raw_path,
        "normalized_path": normalized,
        "exists": exists,
        "is_file": is_file,
        "is_directory": is_directory,
        "is_drive_root": is_drive_root,
        "writable": writable,
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
    }


def start_picker(
    kind: str,
    *,
    title: str = "",
    default_path: str = "",
    file_filter: str = "",
) -> str:
    if kind not in ("directory", "executable"):
        raise ValueError("不支持的路径选择类型")
    task_id = NativeTaskRegistry.create(f"pick-{kind}")

    def _show_dialog() -> None:
        parent = None
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QFileDialog, QWidget
            from local_handler import LocalRequestHandler

            parent = QWidget()
            parent.setWindowFlags(Qt.WindowType.Tool)
            parent.show()
            parent.raise_()
            parent.activateWindow()
            LocalRequestHandler._force_dialog_foreground(parent)

            initial = default_path or os.path.expanduser("~")
            if kind == "directory":
                selected = QFileDialog.getExistingDirectory(
                    parent,
                    title or "选择目录",
                    initial,
                    QFileDialog.Option.ShowDirsOnly
                    | QFileDialog.Option.DontResolveSymlinks,
                )
            else:
                selected, _ = QFileDialog.getOpenFileName(
                    parent,
                    title or "选择游戏启动程序",
                    initial,
                    file_filter or "游戏文件 (*.exe *.lnk);;所有文件 (*.*)",
                )
            if selected:
                NativeTaskRegistry.finish(
                    task_id,
                    success=True,
                    cancelled=False,
                    path=os.path.normpath(selected),
                )
            else:
                NativeTaskRegistry.finish(
                    task_id,
                    success=False,
                    cancelled=True,
                    path="",
                )
        except Exception as exc:
            NativeTaskRegistry.finish(
                task_id,
                success=False,
                cancelled=False,
                error=str(exc),
                path="",
            )
        finally:
            if parent is not None:
                parent.close()

    app_state.run_on_main_thread(_show_dialog)
    return task_id
