import copy
import os
import sys
import threading
import time
from pathlib import Path, PureWindowsPath

from nativebridge import NativeTaskRegistry
from logutil import safe_exception


_IMPORT_LOCK = threading.Lock()


def _path_key(path):
    return os.path.normcase(os.path.normpath(str(path or "")))


def start_import(manager, *, source, game_id="", path="", distribution_id=-1, after_import=None, wait=False):
    if source not in ("manual", "fever", "startup"):
        raise ValueError("不支持的导入来源")
    if source != "startup" and not game_id:
        raise ValueError("缺少游戏信息")
    extensions = (".exe", ".lnk") if wait else (".exe",)
    if source == "manual" and (
        not os.path.isfile(path) or not str(path).lower().endswith(extensions)
    ):
        raise ValueError("请选择现有的游戏 EXE 文件")
    resolved_id = manager.find_matching_game_id(game_id) or game_id
    distribution_id = int(distribution_id)
    if wait:
        # Older callers of the explicit download mode receive the result
        # directly. They do not get a public import task to poll.
        task_id = NativeTaskRegistry.create("game-import-sync")
        _run_import(manager, task_id, source, resolved_id, path, distribution_id, after_import)
        return NativeTaskRegistry.get(task_id)

    key = ("startup" if source == "startup" else
           f"{source}:{resolved_id}:{_path_key(path)}:{distribution_id}:{after_import is not None}")
    task_id, created = NativeTaskRegistry.create_import(
        key, source=source, game_id=resolved_id, path=path, distribution_id=distribution_id,
    )
    if created:
        threading.Thread(
            target=_run_import,
            args=(manager, task_id, source, resolved_id, path, distribution_id, after_import),
            name=f"game-import-{task_id[:8]}", daemon=True,
        ).start()
    return NativeTaskRegistry.get(task_id)


def _plan_download(game, path, requested_id, records, *, from_fever):
    from channelHandler.channelUtils import cmp_game_id
    from gamemgr import GameInstallation, _FEVER_APP_STATE_DIRECTORY

    if not path.lower().endswith(".exe"):
        return None, "已登记所选路径，只有游戏 EXE 可以验证和修复", None
    distributions = list(dict.fromkeys(game.get_distributions()))
    file_info = {}
    launchers = {}

    def target(dist_id):
        if dist_id not in distributions:
            return None
        if dist_id not in file_info:
            file_info[dist_id] = game.get_file_distribution_info(dist_id) or {}
        info = file_info[dist_id]
        return info if info.get("app_content_id") and info.get("version_code") else None

    def matches_content(info, content_id):
        return info and (content_id in (None, "") or str(info["app_content_id"]) == str(content_id))

    def launcher_for(dist_id):
        if dist_id not in launchers:
            launchers[dist_id] = game.get_launcher_data_for_distribution(dist_id) or {}
        return launchers[dist_id]

    def matching_startup(candidate):
        parts = PureWindowsPath(str(candidate or ""))
        if not parts.parts or parts.drive or parts.root or ".." in parts.parts:
            return None
        root = game._root_from_executable(path, str(parts))
        if _path_key(os.path.join(root, *parts.parts)) == _path_key(path):
            return str(parts), root
        return None

    same_path = {r["distribution_id"]: r for r in records
                 if r.get("distribution_id", -1) != -1
                 and cmp_game_id(r.get("game_id"), game.game_id)
                 and _path_key(r.get("path")) == _path_key(path)}
    selected = None
    if from_fever:
        # The registry record determines what to register even when online
        # metadata is unavailable. Only a compatible manifest can start IPC.
        selected = next(iter(same_path.values())) if len(same_path) == 1 else same_path.get(requested_id)
        if selected:
            info = target(selected["distribution_id"])
            if not info:
                return None, "无法获取该 Fever 分发的下载清单，已保留官方安装记录", selected
            if not matches_content(info, selected.get("content_id")):
                return None, "Fever 安装记录与分发清单的内容不一致，只登记了路径", None

    if selected is None:
        official = {dist: record for dist, record in same_path.items()
                    if matches_content(target(dist), record.get("content_id"))}
        selected = next(iter(official.values())) if len(official) == 1 else official.get(requested_id)
    if from_fever and selected is None:
        return None, "无法确定该安装的可下载分发，只登记了路径", None

    dist_id = selected["distribution_id"] if selected else requested_id
    marker = None
    state_startup = ""
    if selected is None:
        saved = GameInstallation.read_marker(path)
        if (saved and cmp_game_id(str(saved.get("game_id") or ""), game.game_id)
                and str(saved.get("installed_version") or saved.get("version") or "").strip()
                and saved.get("content_id")):
            marked_dist = game._coerce_distribution_id(saved.get("distribution_id"))
            if matches_content(target(marked_dist), saved["content_id"]):
                dist_id = marked_dist
                marker = saved

        # Migrated Fever installs may keep their APP state without a registry
        # record. Use it to identify the distribution, not to certify the files.
        state_roots = {_path_key(root) for root in Path(path).parents
                       if (root / _FEVER_APP_STATE_DIRECTORY).is_dir()}
        if state_roots:
            matches = []
            for candidate_id in distributions:
                startup = matching_startup(launcher_for(candidate_id).get("startup_path"))
                if not startup or _path_key(startup[1]) not in state_roots:
                    continue
                info = target(candidate_id)
                if info and game.read_fever_version(startup[1], info["app_content_id"]):
                    matches.append((candidate_id, startup[0]))
                    if len(matches) > 1:
                        break
            if len(matches) == 1:
                state_distribution, state_startup = matches[0]
                if state_distribution != dist_id:
                    marker = None
                dist_id = state_distribution

        if dist_id == -1:
            candidates = distributions
            if len(candidates) > 1:
                candidates = [dist for dist in candidates
                              if matching_startup(launcher_for(dist).get("startup_path"))]
            if len(candidates) == 1:
                dist_id = candidates[0]

    info = target(dist_id)
    if not info:
        return None, "无法确定该安装的可下载分发，只登记了路径", None
    launcher = launcher_for(dist_id)
    startup_paths = [state_startup, (selected or {}).get("startup_path"), (marker or {}).get("startup_path"),
                     launcher.get("startup_path")]
    install_root = (selected or {}).get("install_root")
    if install_root:
        try:
            startup_paths.insert(0, os.path.relpath(path, install_root))
        except ValueError:
            pass
    configured = [candidate for candidate in startup_paths if candidate]
    for candidate in configured or [os.path.basename(path)]:
        startup = matching_startup(candidate)
        if startup:
            break
    else:
        return None, "所选安装路径与游戏启动程序配置不匹配，未启动下载", selected if from_fever else None
    startup_path, root = startup

    origin = ""
    if from_fever:
        origin = game.read_fever_version(root, info["app_content_id"]) or str(
            selected.get("version_code") or ""
        ).strip()
    return {
        "distribution_id": dist_id, "content_id": info["app_content_id"],
        "startup_path": startup_path,
        "startup_args": str((selected or {}).get("startup_args") or launcher.get("startup_params") or ""),
        "origin_version": origin, "target_version": str(info["version_code"]),
    }, "", None


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
        if source == "fever" or (source == "manual" and after_import):
            with _FEVER_GAMES_CACHE_LOCK:
                genv.set(_FEVER_GAMES_CACHE_KEY, None, cached=False)
                records = manager.list_fever_games()
        elif source == "startup":
            records = manager.list_fever_games()
        check_cancel()
        with manager._save_lock:
            games = manager.games
            if source != "startup":
                # An explicit import only reads or changes the selected title.
                target_id = manager.find_matching_game_id(game_id)
                games = {target_id: games[target_id]} if target_id is not None else {}
            baseline = {
                gid: (copy.deepcopy(game.to_dict()), copy.deepcopy(game.to_installation_state()))
                for gid, game in games.items()
            }
        staged = GameManager.__new__(GameManager)
        staged.logger = manager.logger
        staged._save_lock = threading.RLock()
        staged.games = {gid: Game.from_dict(data, state) for gid, (data, state) in baseline.items()}
        staged._save_games = lambda: None
        staged.list_fever_games = lambda: [dict(record) for record in records]
        NativeTaskRegistry.update(task_id, phase="checking")
        plan, skip_reason, fever_registration = None, "", None
        if source == "manual":
            options = {"read_marker": False} if after_import else {}
            if not staged.set_game_path(
                game_id, path, preserve_missing=True, identify=after_import is None,
                progress_callback=progress, cancel_check=check_cancel, **options,
            ):
                raise ValueError("导入游戏路径失败")
            if after_import:
                game = staged.get_existing_game(game_id)
                plan, skip_reason, _ = _plan_download(game, path, distribution_id, records, from_fever=False)
        elif source == "fever":
            from channelHandler.channelUtils import cmp_game_id
            matching = [record for record in records
                        if cmp_game_id(record.get("game_id"), game_id)
                        and (not path or _path_key(record.get("path")) == _path_key(path))]
            eligible = [record for record in matching if
                        distribution_id == -1 or record.get("distribution_id") == distribution_id]
            selected = next(iter(eligible), None)
            if not selected and after_import and path and matching:
                selected = matching[0]
            if not selected:
                raise ValueError("未找到可导入的 Fever 游戏记录")
            if after_import and not path and len(eligible) != 1:
                raise ValueError("存在多个 Fever 安装，请选择要导入的路径")
            path = str(selected.get("path") or "")
            if after_import:
                game = staged.get_game_or_temp(game_id)
                plan, skip_reason, fever_registration = _plan_download(
                    game, path, distribution_id, matching, from_fever=True,
                )
            if after_import and not (plan or fever_registration):
                if not staged.set_game_path(game_id, path, identify=False, preserve_missing=True, read_marker=False):
                    raise ValueError("导入游戏路径失败")
                distribution_id = -1
            else:
                record = plan or fever_registration or selected
                distribution_id = int(record.get("distribution_id", -1))
                game_id = staged.import_fever_game(
                    game_id, distribution_id, path, create_shortcut=False,
                    notify=False, refresh_registry=False,
                )
                if not game_id:
                    raise ValueError("未找到可导入的 Fever 游戏记录")
            NativeTaskRegistry.update(task_id, path=path, distribution_id=distribution_id)
        else:
            staged.import_missing_fever_games(write_markers=False, cancel_check=check_cancel)
            staged.initialize_downloadable_installation_versions(
                write_markers=False, progress_callback=progress, cancel_check=check_cancel,
            )
        if after_import and source != "startup" and not fever_registration:
            game = staged.get_existing_game(game_id)
            installation = next(
                item for item in game.installations.values() if _path_key(item.path) == _path_key(path)
            )
            # Never persist an unverified manual version, including a marker's
            # apparent target version, before the repair has checked the files.
            installation.installed_version = plan["origin_version"] if plan else ""
            installation.content_id = plan["content_id"] if plan else None
            distribution = plan["distribution_id"] if plan else -1
            if plan:
                installation.startup_path = plan["startup_path"]
                if source == "fever" or installation.distribution_id != distribution or not installation.startup_args:
                    installation.startup_args = plan["startup_args"]
            game._claim_distribution(installation, distribution)
            installation.updated_at = int(time.time())
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
        elif source == "fever" and (plan or fever_registration or not after_import):
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
                if plan is None:
                    selected.update(download_status="skipped", download_error=skip_reason)
                elif plan["origin_version"] == plan["target_version"]:
                    selected.update(download_status="up_to_date", download_operation="update")
                else:
                    selected.update(after_import(game_id, selected["installation_id"], path, plan))
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

        if genv.update_cached({
            manager.GAMES_CACHE_KEY: {gid: game.to_dict() for gid, game in all_games.items()},
            manager.INSTALLATIONS_CACHE_KEY: {
                gid: game.to_installation_state() for gid, game in all_games.items()
            },
        }) is False:
            raise OSError("无法保存导入的游戏配置")

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
        candidates.append({**{k: v for k, v in record.items() if k != "install_root"}, "source": "fever"})
    return {
        "success": True, "candidates": candidates,
        "active_tasks": [task for task in recent if not task.get("acknowledged")],
        "recent_tasks": recent, "prompt_on_startup": False,
    }
