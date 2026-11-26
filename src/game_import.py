import copy
import os
import sys
import threading

from nativebridge import NativeTaskRegistry
from logutil import safe_exception


_IMPORT_LOCK = threading.Lock()


def _path_key(path):
    return os.path.normcase(os.path.normpath(str(path or "")))


def start_import(manager, *, source, game_id="", path="", distribution_id=-1, after_import=None):
    if source not in ("manual", "fever", "startup"):
        raise ValueError("不支持的导入来源")
    if source != "startup" and not game_id:
        raise ValueError("缺少游戏信息")
    if source == "manual" and (
        not os.path.isfile(path) or not str(path).lower().endswith(".exe")
    ):
        raise ValueError("请选择现有的游戏 EXE 文件")
    resolved_id = manager.find_matching_game_id(game_id) or game_id
    key = "startup" if source == "startup" else f"{resolved_id}:{_path_key(path)}:{distribution_id}"
    task_id, created = NativeTaskRegistry.create_import(
        key, source=source, game_id=resolved_id, path=path,
        distribution_id=int(distribution_id),
    )
    if created:
        threading.Thread(
            target=_run_import,
            args=(manager, task_id, source, resolved_id, path, int(distribution_id), after_import),
            name=f"game-import-{task_id[:8]}", daemon=True,
        ).start()
    return NativeTaskRegistry.get(task_id)


def _run_import(manager, task_id, source, game_id, path, distribution_id, after_import=None):
    from gamemgr import Game, GameManager, _FEVER_GAMES_CACHE_KEY, _FEVER_GAMES_CACHE_LOCK
    from envmgr import genv

    def check_cancel():
        if (NativeTaskRegistry.get(task_id) or {}).get("cancel_requested"):
            raise InterruptedError("导入已取消")

    def progress(completed, total):
        values = {"phase": "checking", "completed_files": completed, "total_files": total}
        if source != "startup" and total:
            values["progress_percent"] = min(99, int(completed * 100 / total))
        NativeTaskRegistry.update(task_id, **values)

    acquired = False
    try:
        while not acquired:
            check_cancel()
            acquired = _IMPORT_LOCK.acquire(timeout=0.1)
        check_cancel()
        records = []
        if source == "fever":
            with _FEVER_GAMES_CACHE_LOCK:
                genv.set(_FEVER_GAMES_CACHE_KEY, None, cached=False)
                records = manager.list_fever_games()
        elif source == "startup":
            records = manager.list_fever_games()
        check_cancel()
        with manager._save_lock:
            baseline = {
                gid: (copy.deepcopy(game.to_dict()), copy.deepcopy(game.to_installation_state()))
                for gid, game in list(manager.games.items())
            }
        staged = GameManager.__new__(GameManager)
        staged.logger = manager.logger
        staged._save_lock = threading.RLock()
        staged.games = {gid: Game.from_dict(data, state) for gid, (data, state) in baseline.items()}
        staged._save_games = lambda: None
        staged.list_fever_games = lambda: [dict(record) for record in records]
        NativeTaskRegistry.update(task_id, phase="checking")
        if source == "manual":
            if not staged.set_game_path(
                game_id, path, preserve_missing=True, identify=after_import is None,
                progress_callback=progress, cancel_check=check_cancel,
            ):
                raise ValueError("导入游戏路径失败")
        elif source == "fever":
            from channelHandler.channelUtils import cmp_game_id
            selected = next((record for record in records
                             if cmp_game_id(record.get("game_id"), game_id)
                             and (distribution_id == -1 or record.get("distribution_id") == distribution_id)
                             and (not path or _path_key(record.get("path")) == _path_key(path))), None)
            if not selected:
                raise ValueError("未找到可导入的 Fever 游戏记录")
            path = str(selected.get("path") or "")
            distribution_id = int(selected.get("distribution_id", -1))
            NativeTaskRegistry.update(task_id, path=path, distribution_id=distribution_id)
            game_id = staged.import_fever_game(
                game_id, distribution_id, path, create_shortcut=False,
                notify=False, refresh_registry=False,
            )
            if not game_id:
                raise ValueError("未找到可导入的 Fever 游戏记录")
        else:
            staged.import_missing_fever_games(write_markers=False, cancel_check=check_cancel)
            staged.initialize_downloadable_installation_versions(
                write_markers=False, progress_callback=progress, cancel_check=check_cancel,
            )
        check_cancel()
        changes = {}
        results = []
        requested_path_key = _path_key(path)
        requested_result = None
        for gid, game in staged.games.items():
            original_game, original_state = baseline.get(gid, ({}, {}))
            previous = original_state.get("installations", {})
            requested_game = source != "startup" and gid == game_id
            needs_commit = requested_game
            staged_installations = {}
            for iid, item in game.installations.items():
                data = item.to_dict()
                staged_installations[iid] = data
                changed = data != previous.get(item.installation_id)
                if changed:
                    needs_commit = True
                same_path = requested_game and _path_key(item.path) == requested_path_key
                requested = requested_game and (not path or same_path) and (
                    source == "manual" or distribution_id == -1
                    or item.distribution_id == distribution_id
                )
                if not changed and not requested:
                    continue
                result = {
                    "success": True, "game_id": gid, "path": item.path,
                    "installation_id": item.installation_id,
                    "distribution_id": item.distribution_id,
                    "version": item.installed_version,
                    "installed_version": item.installed_version,
                }
                results.append(result)
                if same_path and requested_result is None:
                    requested_result = result
            if needs_commit:
                changes[gid] = (game, original_game.get("name"), previous, staged_installations)
        _commit_import(manager, task_id, changes, results)
        if source == "startup":
            for item in results:
                try:
                    game = manager.get_existing_game(item["game_id"])
                    installation = game.get_installation(item["installation_id"]) if game else None
                    if installation:
                        installation.write_marker(game.game_id)
                except Exception:
                    manager.logger.warning("游戏已导入，但未能写入安装标记")
        elif source == "fever":
            com = None
            try:
                if sys.platform == "win32":
                    import pythoncom
                    pythoncom.CoInitialize()
                    com = pythoncom
                game = manager.get_existing_game(game_id)
                for item in results:
                    game.create_tool_launch_shortcut(item["path"], item["installation_id"])
            except Exception:
                manager.logger.warning("游戏已导入，但未能创建启动快捷方式")
            finally:
                if com:
                    com.CoUninitialize()
        values = {"results": results, "phase": "finished", "progress_percent": 100,
                  "cancelable": False}
        if source != "startup" and results:
            selected = requested_result if requested_result is not None else results[-1]
            if after_import and _path_key(selected["path"]) == requested_path_key:
                selected.update(after_import(game_id, selected["installation_id"], path, distribution_id))
            values.update(selected)
            values.pop("success", None)
        if source == "startup" and not results:
            values["acknowledged"] = True
        NativeTaskRegistry.finish(task_id, success=True, **values)
    except InterruptedError:
        NativeTaskRegistry.finish(task_id, success=False, cancelled=True,
                                  cancelable=False, phase="cancelled", error="导入已取消")
    except Exception as exc:
        safe_exception(manager.logger, "后台导入游戏失败")
        NativeTaskRegistry.finish(task_id, success=False, cancelable=False,
                                  phase="failed", error=str(exc))
    finally:
        if acquired:
            _IMPORT_LOCK.release()


def _commit_import(manager, task_id, changes, results):
    """Persist imports before applying them to the existing runtime objects."""
    from envmgr import genv
    from gamemgr import Game, GameInstallation

    with manager._save_lock:
        targets = []
        for gid, (_, _, previous, _) in changes.items():
            live = manager.get_existing_game(gid)
            current = live.installations if live else {}
            if current.keys() != previous.keys() or any(
                getattr(current[iid], field) != value
                for iid, data in previous.items()
                for field, value in data.items()
                if field not in ("settings", "updated_at")
            ):
                raise ValueError("安装记录已在导入期间变更，请重新导入")
            targets.append((gid, live))
        for result in results:
            if not os.path.isfile(result["path"]):
                raise ValueError("游戏启动程序已在导入期间移走，请重新选择")
        NativeTaskRegistry.begin_import_commit(task_id)
        if not targets:
            return

        all_games = dict(manager.games)
        pending = []
        for gid, live in targets:
            staged_game, previous_name, previous, staged_installations = changes[gid]
            prepared = staged_game
            if live is not None:
                title = live.to_dict()
                # Normalize title settings without reconstructing the legacy path.
                title["path"] = ""
                prepared = Game.from_dict(copy.deepcopy(title), {
                    "force_fever_bridge_distributions": sorted(live.force_fever_bridge_distributions),
                })
                if live.name == previous_name:
                    prepared.name = staged_game.name
            prepared.last_used_time = max(prepared.last_used_time, staged_game.last_used_time)

            persisted = {}
            installations = {}
            patches = []
            current_installations = live.installations if live is not None else {}
            for iid, staged_data in staged_installations.items():
                current = current_installations.get(iid)
                if current is None:
                    saved = GameInstallation.from_dict(staged_data)
                    installations[iid] = saved
                else:
                    before = previous.get(iid, {})
                    delta = {
                        key: value for key, value in staged_data.items()
                        if key not in ("settings", "updated_at", "created_at")
                        and value != before.get(key)
                    }
                    data = current.to_dict()
                    data.update(delta)
                    data["updated_at"] = max(data["updated_at"], staged_data["updated_at"])
                    saved = GameInstallation.from_dict(data)
                    installations[iid] = current
                    patches.append((current, delta, saved.updated_at))
                persisted[iid] = saved
            prepared.installations = persisted
            all_games[gid] = prepared
            pending.append((gid, live, prepared, previous_name, installations, patches))

        genv.update_cached({
            manager.GAMES_CACHE_KEY: {gid: game.to_dict() for gid, game in all_games.items()},
            manager.INSTALLATIONS_CACHE_KEY: {
                gid: game.to_installation_state() for gid, game in all_games.items()
            },
        })

        for gid, live, prepared, previous_name, installations, patches in pending:
            if live is None:
                manager.games[gid] = prepared
                continue
            for current, delta, updated_at in patches:
                for key, value in delta.items():
                    setattr(current, key, value)
                current.updated_at = max(current.updated_at, updated_at)
            live.installations = installations
            if live.name == previous_name:
                live.name = prepared.name
            live.last_used_time = max(live.last_used_time, prepared.last_used_time)


def import_state(manager):
    recent = NativeTaskRegistry.import_tasks()
    candidates = []
    for record in manager.list_fever_games():
        if not os.path.isfile(str(record.get("path") or "")):
            continue
        candidates.append({**record, "source": "fever"})
    return {
        "success": True, "candidates": candidates,
        "active_tasks": [task for task in recent if not task.get("acknowledged")],
        "recent_tasks": recent, "prompt_on_startup": False,
    }
