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
        with cls._lock:
            control_file = str((cls._tasks.get(task_id) or {}).get("control_file") or "")
        if control_file:
            try:
                os.remove(control_file)
            except OSError:
                pass

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
    def find_pending_download(cls, game_id: str, distribution_id: int) -> dict | None:
        target_game = str(game_id or "")
        try:
            target_distribution = int(distribution_id)
        except (TypeError, ValueError):
            return None
        with cls._lock:
            task_ids = [
                task_id
                for task_id, task in cls._tasks.items()
                if task.get("kind") in ("launcher-install", "launcher-update")
                and str(task.get("game_id") or "") == target_game
                and int(task.get("distribution_id", -1)) == target_distribution
            ]
        pending = [
            task for task_id in task_ids
            if (task := cls.get(task_id)) and task.get("status") == "pending"
        ]
        return max(pending, key=lambda item: int(item.get("created_at", 0))) if pending else None

    @classmethod
    def create_download(cls, kind, game_id, distribution_id, **values):
        """Reserve a distribution before a worker can start another download."""
        from envmgr import genv

        with cls._lock:
            pending = cls.find_pending_download(game_id, distribution_id)
            if pending:
                return pending["task_id"], False
            task_id = cls.create(kind)
            workdir = genv.get("FP_WORKDIR", os.getcwd())
            cls.update(
                task_id, game_id=game_id, distribution_id=distribution_id,
                status_file=os.path.join(workdir, f"download_status_{task_id}.json"),
                control_file=os.path.join(workdir, f"download_control_{task_id}.json"),
                phase="preparing", progress_percent=0, **values,
            )
            return task_id, True

    @classmethod
    def create_import(cls, key: str, **values) -> tuple[str, bool]:
        with cls._lock:
            cls._cleanup_locked(int(time.time()))
            for task in cls._tasks.values():
                if (task.get("kind") == "game-import" and task.get("import_key") == key
                        and not task.get("acknowledged")):
                    return task["task_id"], False
            task_id = cls.create("game-import")
            cls.update(task_id, import_key=key, cancelable=True, phase="preparing",
                       results=[], **values)
            return task_id, True

    @classmethod
    def import_tasks(cls) -> list[dict]:
        with cls._lock:
            cls._cleanup_locked(int(time.time()))
            return [dict(task) for task in cls._tasks.values()
                    if task.get("kind") == "game-import"]

    @classmethod
    def control_import(cls, task_id: str, action: str) -> dict:
        with cls._lock:
            task = cls._tasks.get(task_id)
            if not task or task.get("kind") != "game-import":
                raise KeyError("未找到导入任务")
            if action == "cancel":
                if task.get("status") != "pending" or not task.get("cancelable"):
                    raise ValueError("导入已经提交或结束，无法取消")
                cls.update(task_id, cancel_requested=True)
            elif action == "acknowledge":
                if task.get("status") != "done":
                    raise ValueError("导入仍在后台进行，关闭向导不会取消任务")
                cls.update(task_id, acknowledged=True)
            else:
                raise ValueError("不支持的导入控制操作")
            return dict(cls._tasks[task_id])

    @classmethod
    def begin_import_commit(cls, task_id: str) -> None:
        with cls._lock:
            task = cls._tasks[task_id]
            if task.get("cancel_requested"):
                raise InterruptedError("导入已取消")
            cls.update(task_id, cancelable=False, phase="saving")

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
            for file_key in ("status_file", "control_file"):
                task_file = task.get(file_key)
                if not task_file:
                    continue
                try:
                    os.remove(task_file)
                except OSError:
                    pass


def reconcile_download(manager, task):
    """Reflect a terminal supervisor result into the existing live record."""
    if (task.get("kind") not in ("launcher-install", "launcher-update")
            or task.get("status") != "done"):
        return task
    task_id = task["task_id"]
    with manager._save_lock:
        task = NativeTaskRegistry.get(task_id) or task
        if task.get("reconciled"):
            return task
        game = manager.get_existing_game(task.get("game_id") or "")
        installation_id = task.get("installation_id")
        installation = game.get_installation(installation_id) if game and installation_id else None
        expected_path = task.get("path")
        same_scope = (
            installation and expected_path
            and os.path.normcase(os.path.realpath(installation.path))
            == os.path.normcase(os.path.realpath(expected_path))
            and installation.distribution_id == task.get("distribution_id")
        )
        if same_scope:
            if task.get("success"):
                target_version = task.get("target_version")
                if target_version:
                    installation.installed_version = str(target_version)
                    if task.get("content_id") is not None:
                        installation.content_id = task["content_id"]
                    installation.source = "download"
                    installation.updated_at = int(time.time())
                    manager._save_games()
            elif task.get("kind") == "launcher-install" and task.get("created_installation"):
                manager.remove_game_installation(game.game_id, installation_id)
        NativeTaskRegistry.update(task_id, reconciled=True)
        return {**task, "reconciled": True}


def finish_download_process(manager, task_id, process):
    """Observe the final supervisor write even when no browser polls."""
    process.wait()
    task = NativeTaskRegistry.get(task_id)
    if task and task.get("status") != "done":
        NativeTaskRegistry.finish(
            task_id, success=False, phase="failed", error="下载进程未返回最终结果",
        )
        task = NativeTaskRegistry.get(task_id)
    if task:
        reconcile_download(manager, task)


def start_launcher_update(
    manager, game, installation, distribution_id, concurrent, *, repair=False, imported=False,
    unknown_origin=False,
):
    """Start an update/repair for exactly one already-committed installation."""
    from logutil import safe_exception

    with manager._save_lock:
        if (manager.get_existing_game(game.game_id) is not game
                or game.get_installation(installation.installation_id) is not installation
                or installation.distribution_id != distribution_id):
            raise ValueError("安装记录已变更，请重新选择")
        path = installation.path
        root = game.get_root_path(installation.installation_id)
        if imported and not repair and (not installation.installed_version or not os.path.isfile(path)):
            raise ValueError("导入完成后安装路径或已确认版本发生变化")
        repair = repair or not installation.installed_version or not os.path.isfile(path)
        task_id, created = NativeTaskRegistry.create_download(
            "launcher-update", game.game_id, distribution_id,
            installation_id=installation.installation_id, path=path,
            operation="repair" if repair else "update",
        )
        if not created:
            pending = NativeTaskRegistry.get(task_id) or {}
            if pending.get("installation_id") != installation.installation_id:
                raise ValueError("该分发已有其他安装的下载任务")
            return task_id, False
        task = NativeTaskRegistry.get(task_id)

    def run():
        try:
            if imported:
                from fever_install_state import confirmed_install_root, normalized_path
                launcher = game.get_launcher_data_for_distribution(distribution_id) or {}
                confirmed_root = confirmed_install_root(path, launcher.get("startup_path"))
                if (distribution_id not in game.get_distributions() or not confirmed_root
                        or confirmed_root != normalized_path(os.path.realpath(root))
                        or installation.path != path
                        or game.get_root_path(installation.installation_id) != root):
                    raise ValueError("无法用分发的启动程序确认更新或修复目录")
            # An imported version is already committed. Only this separate
            # download operation may promote it to the target version.
            NativeTaskRegistry.update(task_id, phase="checking" if repair else "preparing")
            result = game.try_update(
                distribution_id, concurrent, installation.installation_id,
                progress_file=task["status_file"], control_file=task["control_file"],
                repair=repair, unknown_origin=unknown_origin,
                allow_repair_fallback=not imported or repair,
            )
            if result is None:
                raise RuntimeError("启动游戏修复失败" if repair else "启动游戏更新失败")
            if result.process is not None:
                NativeTaskRegistry.update(
                    task_id, success=True, phase="download_started",
                    version=installation.installed_version, download_async=True,
                )
                finish_download_process(manager, task_id, result.process)
            else:
                if result.verified_version:
                    with manager._save_lock:
                        current = game.get_installation(installation.installation_id)
                        if (current is not installation or current.distribution_id != distribution_id
                                or current.path != path
                                or game.get_root_path(current.installation_id) != root):
                            raise ValueError("修复期间安装目录或分发已变更")
                        current.installed_version = result.verified_version
                        current.content_id = result.content_id
                        if current.source == "download_pending":
                            current.source = "download"
                        current.updated_at = int(time.time())
                        current.write_marker(game.game_id)
                        manager._save_games()
                NativeTaskRegistry.finish(
                    task_id, success=True, phase="finished", progress_percent=100,
                    version=installation.installed_version, download_async=False,
                    post_update_actions=result.post_update_actions,
                )
        except Exception as exc:
            safe_exception(manager.logger, "后台处理游戏更新/修复失败")
            NativeTaskRegistry.finish(task_id, success=False, phase="failed", error=str(exc))

    try:
        threading.Thread(target=run, name=f"launcher-update-{task_id[:8]}", daemon=True).start()
    except Exception as exc:
        NativeTaskRegistry.finish(task_id, success=False, phase="failed", error=str(exc))
        raise
    return task_id, True


def capabilities() -> dict:
    return {
        "success": True,
        "schema_version": 1,
        "pick_directory": True,
        "pick_executable": True,
        "inspect_path": True,
        "task_polling": True,
        "download_progress": True,
        "download_pause_resume": True,
        "import_tasks_v1": True,
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
        "volume_root": root,
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
