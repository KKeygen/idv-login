import copy
import os
import sys
import threading

from nativebridge import NativeTaskRegistry
from logutil import safe_exception


_IMPORT_LOCK = threading.Lock()


def _path_key(path):
    return os.path.normcase(os.path.normpath(str(path or "")))


def prepare_import_installation(
    game, installation, previous=None, *, requested_distribution_id=-1,
    cancel_check=None,
):
    """Resolve one explicit import using the exact startup and Fever state."""
    from fever_install_state import canonical_content_id, confirmed_install_root, read_install_states

    previous = previous or {}
    recorded_distribution = installation.distribution_id
    bound_distribution = (
        requested_distribution_id if requested_distribution_id >= 0 else recorded_distribution
    )
    # Importing registry/marker metadata is not itself proof of the current
    # version. Preserve a previous record only for this exact installation.
    if (_path_key(previous.get("path")) == _path_key(installation.path)
            and previous.get("distribution_id") == recorded_distribution):
        installation.installed_version = str(previous.get("installed_version") or "")
    else:
        installation.installed_version = ""

    if requested_distribution_id >= 0 and recorded_distribution not in (-1, requested_distribution_id):
        return {"state_status": "distribution_mismatch", "distribution_verified": False}
    options = list(dict.fromkeys(game.get_distributions()))
    if bound_distribution >= 0:
        options = [bound_distribution] if bound_distribution in options else []
    if not options or len(options) > 16:
        return {"state_status": "unresolved_distribution", "distribution_verified": False}

    matches = []
    for distribution_id in options:
        if cancel_check:
            cancel_check()
        launcher = game.get_launcher_data_for_distribution(distribution_id) or {}
        startup_path = launcher.get("startup_path")
        root = confirmed_install_root(installation.path, startup_path)
        if not root:
            continue
        manifest = game.get_file_distribution_info(distribution_id, refresh=True) or {}
        recorded_content = (
            canonical_content_id(installation.content_id)
            if recorded_distribution == distribution_id else ""
        )
        content_id = recorded_content or canonical_content_id(manifest.get("app_content_id"))
        if not content_id:
            continue
        matches.append({
            "id": distribution_id, "content": content_id, "root": root,
            "startup": startup_path, "args": str(launcher.get("startup_params") or ""),
            "target": str(manifest.get("version_code") or ""),
        })

    by_root = {}
    for candidate in matches:
        by_root.setdefault(candidate["root"], set()).add(candidate["content"])
    states = {
        root: read_install_states(root, ids, cancel_check=cancel_check)
        for root, ids in by_root.items()
    }
    for candidate in matches:
        candidate["state"], candidate["version"] = states[candidate["root"]][candidate["content"]]
    if bound_distribution < 0:
        # A filename or a one-item catalog alone is insufficient to assign an
        # arbitrary local path. Require a unique observed app-content match.
        matches = [
            candidate for candidate in matches
            if candidate["state"] in ("installed", "staged", "no_version")
        ]
    if len(matches) != 1:
        return {"state_status": "unresolved_distribution", "distribution_verified": False}

    match = matches[0]
    game._claim_distribution(installation, match["id"])
    installation.content_id = int(match["content"])
    installation.startup_path = match["startup"]
    if not installation.startup_args:
        installation.startup_args = match["args"]
    if match["version"]:
        installation.installed_version = match["version"]
        if installation.source == "download_pending":
            installation.source = "download"
    return {
        "state_status": match["state"],
        "distribution_verified": True,
        "detected_installed_version": match["version"],
        "target_version": match["target"],
    }


def queue_import_download(manager, game_id, installation_id, path, details):
    """Start one update or fallback repair only after the import is committed."""
    from nativebridge import start_launcher_update

    if sys.platform != "win32":
        return {"download_status": "skipped", "download_error": "当前平台不支持更新或修复"}
    if not details.get("distribution_verified"):
        return {"download_status": "skipped", "download_error": "已导入，但无法确认游戏分发或安装目录"}
    installed_version = details.get("detected_installed_version")
    repair = not installed_version
    operation = "repair" if repair else "update"
    try:
        with manager._save_lock:
            game = manager.get_existing_game(game_id)
            installation = game.get_installation(installation_id) if game and installation_id else None
            if (not installation or _path_key(installation.path) != _path_key(path)
                    or not os.path.isfile(installation.path)
                    or (installed_version and installation.installed_version != installed_version)):
                return {"download_status": "skipped", "download_error": "已导入，但安装记录已经变更"}
            distribution_id = installation.distribution_id
            pending = NativeTaskRegistry.find_pending_download(game.game_id, distribution_id)
            if pending and (
                pending.get("operation") != operation
                or pending.get("installation_id") != installation_id
                or _path_key(pending.get("path")) != _path_key(path)
            ):
                return {"download_status": "skipped", "download_error": "该分发已有下载任务"}
            if not pending and installed_version and installed_version == details.get("target_version"):
                return {"download_operation": "update", "download_status": "up_to_date"}
            task_id, created = start_launcher_update(
                manager, game, installation, distribution_id, 4, repair=repair,
                imported=True, unknown_origin=repair,
            )
        status = "queued" if created else "already_pending"
        return {
            "download_operation": operation, "download_task_id": task_id, "download_status": status,
            f"{operation}_task_id": task_id, f"{operation}_status": status,
        }
    except Exception:
        safe_exception(manager.logger, "游戏已导入，但启动自动更新或修复失败")
        message = "游戏已导入，但未能启动自动更新或修复"
        return {
            "download_operation": operation, "download_status": "failed", "download_error": message,
            f"{operation}_status": "failed", f"{operation}_error": message,
        }


def start_import(manager, *, source, game_id="", path="", distribution_id=-1, update_after_import=False):
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
            args=(manager, task_id, source, resolved_id, path, int(distribution_id), update_after_import),
            name=f"game-import-{task_id[:8]}", daemon=True,
        ).start()
    return NativeTaskRegistry.get(task_id)


def _run_import(manager, task_id, source, game_id, path, distribution_id, update_after_import):
    from gamemgr import Game, GameInstallation, GameManager, _FEVER_GAMES_CACHE_KEY, _FEVER_GAMES_CACHE_LOCK
    from envmgr import genv

    def check_cancel():
        if (NativeTaskRegistry.get(task_id) or {}).get("cancel_requested"):
            raise InterruptedError("导入已取消")

    acquired = False
    try:
        while not acquired:
            check_cancel()
            acquired = _IMPORT_LOCK.acquire(timeout=0.1)
        check_cancel()
        records = []
        if source in ("fever", "manual"):
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
        import_details = {}
        if source == "manual":
            if not staged.set_game_path(
                game_id, path, preserve_missing=True,
                cancel_check=check_cancel, import_details=import_details,
                distribution_id=distribution_id,
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
                import_details=import_details, cancel_check=check_cancel,
            )
            if not game_id:
                raise ValueError("未找到可导入的 Fever 游戏记录")
        else:
            staged.import_missing_fever_games(write_markers=False, cancel_check=check_cancel)
            staged.initialize_downloadable_installation_versions(
                write_markers=False, cancel_check=check_cancel,
            )
        check_cancel()
        changes = {}
        results = []
        selected_result = None
        for gid, game in staged.games.items():
            old_state = baseline.get(gid, ({}, {}))[1].get("installations", {})
            changed = [item for item in game.installations.values()
                       if item.to_dict() != old_state.get(item.installation_id)]
            if changed or (source != "startup" and gid == game_id):
                changes[gid] = game
            for item in game.installations.values():
                selected = source != "startup" and gid == game_id and (
                    not path or _path_key(item.path) == _path_key(path)
                ) and (distribution_id == -1 or item.distribution_id == distribution_id)
                if item in changed or selected:
                    results.append({
                        "success": True, "game_id": gid, "path": item.path,
                        "installation_id": item.installation_id,
                        "distribution_id": item.distribution_id,
                        "version": item.installed_version,
                        "installed_version": item.installed_version,
                    })
                    if selected:
                        selected_result = results[-1]
        with manager._save_lock:
            if source != "startup" and selected_result is None:
                raise ValueError("未找到本次导入的安装记录")
            if selected_result:
                pending = NativeTaskRegistry.find_pending_download(
                    selected_result["game_id"], selected_result["distribution_id"],
                )
                if pending:
                    raise ValueError("该分发正在下载或修复，请等到任务结束后再导入")
            for gid in changes:
                live = manager.get_existing_game(gid)
                current = live.to_installation_state().get("installations", {}) if live else {}
                previous = baseline.get(gid, ({}, {}))[1].get("installations", {})
                def identity(records):
                    return {iid: {key: value for key, value in data.items()
                                  if key not in ("settings", "updated_at")}
                            for iid, data in records.items()}
                if identity(current) != identity(previous):
                    raise ValueError("安装记录已在导入期间变更，请重新导入")
            for item in results:
                if not os.path.isfile(item["path"]):
                    raise ValueError("游戏启动程序已在导入期间移走，请重新选择")
            NativeTaskRegistry.begin_import_commit(task_id)
            committed = {}
            deltas = {}
            for gid, staged_game in changes.items():
                live = manager.get_existing_game(gid)
                prepared = Game.from_dict(
                    copy.deepcopy(live.to_dict()), copy.deepcopy(live.to_installation_state())
                ) if live else staged_game
                merged = {}
                for iid, item in staged_game.installations.items():
                    before = baseline.get(gid, ({}, {}))[1].get("installations", {}).get(iid, {})
                    current = live.installations.get(iid) if live else None
                    item_data = item.to_dict()
                    delta = {key: value for key, value in item_data.items()
                             if key not in ("settings", "updated_at", "created_at")
                             and (not current or value != before.get(key))}
                    deltas[(gid, iid)] = delta
                    data = current.to_dict() if current else item_data
                    data.update(delta)
                    data["updated_at"] = max(data["updated_at"], item.updated_at)
                    merged[iid] = GameInstallation.from_dict(data)
                prepared.installations = merged
                if live and live.name == baseline.get(gid, ({}, {}))[0].get("name"):
                    prepared.name = staged_game.name
                prepared.last_used_time = max(prepared.last_used_time, staged_game.last_used_time)
                committed[gid] = prepared
            if committed:
                all_games = {**manager.games, **committed}
                genv.update_cached({
                    manager.GAMES_CACHE_KEY: {gid: game.to_dict() for gid, game in all_games.items()},
                    manager.INSTALLATIONS_CACHE_KEY: {
                        gid: game.to_installation_state() for gid, game in all_games.items()
                    },
                })
            for gid, prepared in committed.items():
                live = manager.get_existing_game(gid)
                if live is None:
                    manager.games[gid] = prepared
                    continue
                merged = {}
                for iid, item in prepared.installations.items():
                    current = live.installations.get(iid)
                    if current:
                        for key, value in deltas[(gid, iid)].items():
                            setattr(current, key, value)
                        current.updated_at = max(current.updated_at, item.updated_at)
                        item = current
                    merged[iid] = item
                live.installations = merged
                if live.name == baseline.get(gid, ({}, {}))[0].get("name"):
                    live.name = prepared.name
                live.last_used_time = max(live.last_used_time, prepared.last_used_time)
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
        if selected_result:
            selected_result.update(import_details)
            if update_after_import:
                selected_result.update(queue_import_download(
                    manager, selected_result["game_id"], selected_result["installation_id"],
                    selected_result["path"], import_details,
                ))
            values.update(selected_result)
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
