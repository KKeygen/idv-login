# coding=UTF-8
"""
Copyright (c) 2026 KKeygen

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

import json
import os
import sys
import time
import threading
from typing import Tuple
from urllib.parse import parse_qs, urlparse

from envmgr import genv
import app_state
import const
from login_stack_mgr import LoginStackManager
from cloudSync import CloudSyncManager
from cloudRes import CloudRes
from channelHandler.channelUtils import getShortGameId


class LocalRequestHandler:
    """Handles /_idv-login/* API requests locally.

    Used by both the mitmproxy addon (for game WebView requests)
    and the QtWebEngine URL scheme handler (for the standalone Qt window).

    Each call to ``handle()`` is stateless w.r.t. the handler itself;
    all persistent state lives in ``genv`` / managers.
    """

    _cloud_sync_mgr = None
    _cloud_sync_lock = threading.Lock()
    _auto_push_generation = {"value": 0}
    _pending_imports = {}  # {task_id: {"status": "pending"|"done", "success": bool}}

    def __init__(self, *, game_helper, logger):
        self.game_helper = game_helper
        self.logger = logger
        self.stack_mgr = LoginStackManager.get_instance()

        with self._cloud_sync_lock:
            if LocalRequestHandler._cloud_sync_mgr is None:
                LocalRequestHandler._cloud_sync_mgr = CloudSyncManager(logger)
        self.cloud_sync_mgr = LocalRequestHandler._cloud_sync_mgr

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self, request) -> Tuple[int, dict, bytes]:
        """Dispatch a request and return (status_code, headers, body_bytes).

        ``request`` can be a ``mitmproxy.http.Request`` or any object
        exposing ``.path``, ``.method``, ``.query`` (dict-like), and
        ``.content`` (bytes).
        """
        path_raw = getattr(request, "path", "/")
        parsed = urlparse(path_raw)
        path = parsed.path
        method = getattr(request, "method", "GET").upper()

        # Parse query parameters
        if hasattr(request, "query") and hasattr(request.query, "items"):
            args = {k: v for k, v in request.query.items()}
        else:
            qs = parsed.query
            parsed_qs = parse_qs(qs, keep_blank_values=True)
            args = {k: v[0] if len(v) == 1 else v for k, v in parsed_qs.items()}

        # Parse JSON body for POST requests
        json_body = None
        if method == "POST":
            try:
                raw = getattr(request, "content", b"")
                if isinstance(raw, memoryview):
                    raw = bytes(raw)
                json_body = json.loads(raw) if raw else {}
            except Exception:
                json_body = {}

        return self._route(path, method, args, json_body)

    # For the Qt scheme handler which uses a simpler interface
    def handle_simple(self, path: str, method: str = "GET",
                      args: dict = None, json_body: dict = None) -> Tuple[int, dict, bytes]:
        return self._route(path, method, args or {}, json_body)

    # ------------------------------------------------------------------
    # Router
    # ------------------------------------------------------------------

    def _route(self, path: str, method: str, args: dict,
               json_body: dict = None) -> Tuple[int, dict, bytes]:
        route_map = {
            "/_idv-login/health": self._health,
            "/_idv-login/manualChannels": self._manual_channels,
            "/_idv-login/list": self._list_channels,
            "/_idv-login/qrcode": self._channel_qrcode,
            "/_idv-login/cancel-qr": self._cancel_qr,
            "/_idv-login/switch": self._switch_channel,
            "/_idv-login/switch-status": self._switch_status,
            "/_idv-login/del": self._del_channel,
            "/_idv-login/rename": self._rename_channel,
            "/_idv-login/import": self._import_channel,
            "/_idv-login/import-status": self._import_status,
            "/_idv-login/setDefault": self._set_default,
            "/_idv-login/clearDefault": self._clear_default,
            "/_idv-login/get-auto-close-state": self._get_auto_close_state,
            "/_idv-login/switch-auto-close-state": self._switch_auto_close_state,
            "/_idv-login/get-game-auto-start": self._get_game_auto_start,
            "/_idv-login/set-game-auto-start": self._set_game_auto_start,
            "/_idv-login/start-game": self._start_game,
            "/_idv-login/list-games": self._list_games,
            "/_idv-login/launcher-status": self._launcher_status,
            "/_idv-login/launcher-locate": self._launcher_locate,
            "/_idv-login/launcher-install": self._launcher_install,
            "/_idv-login/launcher-update": self._launcher_update,
            "/_idv-login/launcher-update-info": self._launcher_update_info,
            "/_idv-login/launcher-import-fever": self._launcher_import_fever,
            "/_idv-login/launcher-set-default": self._launcher_set_default,
            "/_idv-login/launcher-remove-installation": self._launcher_remove_installation,
            "/_idv-login/fever-games": self._list_fever_games,
            "/_idv-login/defaultChannel": self._get_default_channel,
            "/_idv-login/get-login-delay": self._get_login_delay,
            "/_idv-login/set-login-delay": self._set_login_delay,
            "/_idv-login/cloud-sync/policy": self._cloud_sync_policy,
            "/_idv-login/cloud-sync/generate-master-key": self._cloud_sync_generate_key,
            "/_idv-login/cloud-sync/settings": self._cloud_sync_settings,
            "/_idv-login/cloud-sync/accounts": self._cloud_sync_accounts,
            "/_idv-login/cloud-sync/probe": self._cloud_sync_probe,
            "/_idv-login/cloud-sync/run": self._cloud_sync_run,
            "/_idv-login/cloud-sync/delete": self._cloud_sync_delete,
            "/_idv-login/cloud-sync/access-logs": self._cloud_sync_access_logs,
            "/_idv-login/index": self._serve_index,
            "/_idv-login/export-logs": self._export_logs,
            "/_idv-login/open-external-url": self._open_external_url,
            "/_idv-login/proxy-mode": self._get_proxy_mode,
            "/_idv-login/set-proxy-mode": self._set_proxy_mode,
            "/_idv-login/create-game-shortcut": self._create_game_shortcut,
            "/_idv-login/scan-record-setting": self._scan_record_setting,
            "/_idv-login/native-save-setting": self._native_save_setting,
            "/_idv-login/native/capabilities": self._native_capabilities,
            "/_idv-login/native/window-drag": self._native_window_drag,
            "/_idv-login/native/window-toggle-maximize": self._native_window_toggle_maximize,
            "/_idv-login/native/pick-directory": self._native_pick_directory,
            "/_idv-login/native/pick-executable": self._native_pick_executable,
            "/_idv-login/native/path-status": self._native_path_status,
            "/_idv-login/native/task-status": self._native_task_status,
            "/_idv-login/native/download-control": self._native_download_control,
            "/_idv-login/fever-bridge": self._fever_bridge_setting,

        }

        handler = route_map.get(path)
        if handler:
            try:
                return handler(args, json_body, method)
            except Exception as e:
                self.logger.exception(f"处理请求 {path} 时出错")
                return self._json_response(500, {"success": False, "error": str(e)})

        return self._json_response(404, {"error": "Not found"})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _download_control_file(task_id: str) -> str:
        return os.path.join(
            genv.get("FP_WORKDIR", os.getcwd()),
            f"download_control_{task_id}.json",
        )

    @staticmethod
    def _public_download_task(task: dict | None) -> dict | None:
        if not task:
            return None
        keys = (
            "task_id", "kind", "status", "success", "phase",
            "progress_percent", "rate", "total_bytes", "state", "stages",
            "created_at", "updated_at", "requested_action", "error",
        )
        return {key: task[key] for key in keys if key in task}

    @staticmethod
    def _json_response(status: int, data) -> Tuple[int, dict, bytes]:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        return status, headers, body

    def _health(self, args, body, method):
        return self._json_response(200, {
            "success": True,
            "status": "ok",
            "installation_model_version": 1,
        })

    @staticmethod
    def _force_dialog_foreground(widget):
        """Use Win32 API to bring a dialog widget to the foreground."""
        if sys.platform != "win32" or widget is None:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(widget.winId())
            fg_hwnd = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
            cur_tid = kernel32.GetCurrentThreadId()
            if fg_tid != cur_tid:
                user32.AttachThreadInput(fg_tid, cur_tid, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            if fg_tid != cur_tid:
                user32.AttachThreadInput(fg_tid, cur_tid, False)
        except Exception:
            pass

    @staticmethod
    def _html_response(status: int, html: str) -> Tuple[int, dict, bytes]:
        body = html.encode("utf-8") if isinstance(html, str) else html
        headers = {"Content-Type": "text/html; charset=utf-8"}
        return status, headers, body

    # -- Cloud sync helpers (ported from common_routes.py) ──

    def _default_cloud_sync_settings(self):
        return {
            "consent_ack": False,
            "remember_level": "none",
            "saved_master_key": "",
            "auto_sync": False,
            "sync_direction": "bidirectional",
            "scope_type": "all",
            "scope_game_id": "",
            "scope_uuids": [],
            "expire_time": 259200,
        }

    def _get_cloud_sync_settings(self):
        settings = genv.get("CLOUD_SYNC_SETTINGS", {})
        if not isinstance(settings, dict):
            settings = {}
        merged = self._default_cloud_sync_settings()
        merged.update(settings)
        return merged

    def _save_cloud_sync_settings(self, settings):
        genv.set("CLOUD_SYNC_SETTINGS", settings, True)

    def _resolve_master_key(self, payload):
        settings = self._get_cloud_sync_settings()
        mk = payload.get("master_key", "")
        return mk if mk else settings.get("saved_master_key", "")

    def _resolve_scope(self, payload):
        settings = self._get_cloud_sync_settings()
        return {
            "type": payload.get("scope_type", settings.get("scope_type", "all")),
            "game_id": payload.get("scope_game_id", settings.get("scope_game_id", "")),
            "uuids": (
                payload.get("scope_uuids", settings.get("scope_uuids", []))
                if isinstance(payload.get("scope_uuids", settings.get("scope_uuids", [])), list)
                else []
            ),
        }

    def _schedule_auto_push(self, reason: str):
        settings = self._get_cloud_sync_settings()
        if not settings.get("auto_sync", False) or not settings.get("consent_ack", False):
            return
        master_key = str(settings.get("saved_master_key", "") or "")
        if not master_key:
            return
        strength = self.cloud_sync_mgr.evaluate_master_key_strength(master_key)
        if not strength.get("valid", False):
            return
        scope = {
            "type": str(settings.get("scope_type", "all") or "all"),
            "game_id": str(settings.get("scope_game_id", "") or ""),
            "uuids": settings.get("scope_uuids", []) if isinstance(settings.get("scope_uuids", []), list) else [],
        }
        expire_time = int(settings.get("expire_time", 259200) or 259200)

        self._auto_push_generation["value"] += 1
        gen = self._auto_push_generation["value"]

        def _push():
            self.logger.info(f"检测到账号记录更新，准备在5秒后自动上传云同步（原因: {reason}）")
            time.sleep(5)
            if gen != self._auto_push_generation["value"]:
                return
            try:
                self.cloud_sync_mgr.push(master_key, scope, expire_time)
            except Exception:
                self.logger.exception("自动上传云同步失败")

        threading.Thread(target=_push, daemon=True).start()

    def _pick_qrcode_data(self, channel, game_id):
        """从指定渠道的二维码缓存中获取数据。"""
        cache_key = {
            "myapp": "WECHAT_QRCODE_CACHE",
            "bilibili_sdk": "BILIBILI_QRCODE_CACHE",
        }.get(channel, "WECHAT_QRCODE_CACHE")

        cache = genv.get(cache_key, {})
        if not isinstance(cache, dict) or not cache:
            return None
        if game_id and game_id in cache:
            return cache.get(game_id)
        if game_id:
            for key in cache:
                common_len = sum(
                    1 for a, b in zip(reversed(game_id), reversed(key)) if a == b
                )
                if common_len >= 3:
                    return cache[key]
        return cache.get("_default")

    @staticmethod
    def _pick_launcher_fields(launcher_data):
        if not launcher_data:
            return {}
        # 启动器配置来自公开接口，完整保留嵌套的视觉、新闻和能力数据。
        # 前端按能力读取；这里裁剪白名单会让新字段在到达前端前永久丢失。
        return dict(launcher_data)

    @staticmethod
    def _resolve_installation(game, installation_id: str = "", distribution_id: int = -1):
        if not game:
            return None
        if installation_id:
            return game.get_installation(installation_id)
        if distribution_id != -1:
            matches = [
                item for item in game.installations.values()
                if item.distribution_id == int(distribution_id)
            ]
            return matches[0] if len(matches) == 1 else None
        return game.get_installation()

    # ------------------------------------------------------------------
    # Route implementations
    # ------------------------------------------------------------------

    def _manual_channels(self, args, body, method):
        try:
            game_id = args.get("game_id", "")
            if game_id:
                data = CloudRes().get_all_by_game_id(getShortGameId(game_id))
                return self._json_response(200, data)
        except Exception:
            pass
        return self._json_response(200, const.manual_login_channels)

    def _list_channels(self, args, body, method):
        try:
            result = app_state.channels_helper.list_channels(args.get("game_id", ""))
        except Exception as e:
            result = {"error": str(e)}
        return self._json_response(200, result)

    def _channel_qrcode(self, args, body, method):
        """通用二维码状态接口，支持 myapp（微信）和 bilibili_sdk。"""
        game_id = args.get("game_id", "")
        channel = args.get("channel", "myapp")
        data = self._pick_qrcode_data(channel, game_id)
        if not data:
            return self._json_response(200, {
                "success": False, "status": "idle", "qrcode_base64": "",
            })
        return self._json_response(200, {
            "success": True,
            "status": data.get("status", "idle"),
            "qrcode_base64": data.get("qrcode_base64", ""),
            "uuid": data.get("uuid", ""),
            "ticket": data.get("ticket", ""),
            "timestamp": data.get("timestamp", 0),
        })

    def _cancel_qr(self, args, body, method):
        """取消正在进行的 B站 二维码轮询。"""
        try:
            pending = getattr(app_state.channels_helper, "_pending_login_channel", None)
            if pending and hasattr(pending, "biliLogin"):
                pending.biliLogin.cancel_qr()
                return self._json_response(200, {"success": True})
        except Exception:
            self.logger.exception("取消 QR 失败")
        return self._json_response(200, {"success": False})

    _pending_switch = {}  # {task_id: {"status": "pending"|"done", "result": any}}

    def _switch_channel(self, args, body, method):
        uuid = args.get("uuid", "")
        game_id = args.get("game_id", "")
        genv.set("CHANNEL_ACCOUNT_SELECTED", uuid)
        data = self.stack_mgr.pop_cached_qrcode_data(game_id) if game_id else None

        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
        except Exception:
            app = None

        scanner_uuid = data["uuid"] if data else "Kinich"
        scan_game_id = data["game_id"] if data else "aecfrt3rmaaaaajl-g-g37"

        if app and app.property("_main_loop_running"):
            # 异步模式
            import uuid as uuid_mod
            task_id = str(uuid_mod.uuid4())
            LocalRequestHandler._pending_switch[task_id] = {"status": "pending"}

            def do_switch():
                def on_done(result):
                    LocalRequestHandler._pending_switch[task_id] = {
                        "status": "done", "result": result
                    }

                try:
                    app_state.channels_helper.simulate_scan(
                        uuid, scanner_uuid, scan_game_id, on_complete=on_done
                    )
                except Exception:
                    self.logger.exception("异步切换渠道失败")
                    LocalRequestHandler._pending_switch[task_id] = {
                        "status": "done", "result": False
                    }

            app_state.run_on_main_thread(do_switch)
            return self._json_response(200, {"status": "pending", "task_id": task_id})
        else:
            # 同步模式
            if data:
                app_state.channels_helper.simulate_scan(uuid, data["uuid"], data["game_id"])
            else:
                app_state.channels_helper.simulate_scan(uuid, "Kinich", "aecfrt3rmaaaaajl-g-g37")
            return self._json_response(200, {"current": genv.get("CHANNEL_ACCOUNT_SELECTED")})

    def _switch_status(self, args, body, method):
        """检查异步切换渠道的状态"""
        task_id = args.get("task_id", "")
        task = LocalRequestHandler._pending_switch.get(task_id)
        if task is None:
            return self._json_response(404, {"error": "Unknown task_id"})
        result = dict(task)
        if task["status"] == "done":
            del LocalRequestHandler._pending_switch[task_id]
        return self._json_response(200, result)

    def _del_channel(self, args, body, method):
        success = app_state.channels_helper.delete(args.get("uuid", ""))
        return self._json_response(200, {"success": success})

    def _rename_channel(self, args, body, method):
        success = app_state.channels_helper.rename(
            args.get("uuid", ""), args.get("new_name", "")
        )
        return self._json_response(200, {"success": success})

    def _import_channel(self, args, body, method):
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
        except Exception:
            app = None

        if app and app.property("_main_loop_running"):
            # 异步模式：不阻塞 scheme handler，立即返回 pending
            import uuid as uuid_mod
            task_id = str(uuid_mod.uuid4())
            LocalRequestHandler._pending_imports[task_id] = {"status": "pending"}

            channel = args.get("channel", "")
            game_id = args.get("game_id", "")
            login_method = args.get("login_method", "")

            def do_import():
                def on_done(success):
                    if success is None:
                        LocalRequestHandler._pending_imports[task_id] = {
                            "status": "done", "success": False, "cancelled": True
                        }
                    else:
                        LocalRequestHandler._pending_imports[task_id] = {
                            "status": "done", "success": success
                        }

                try:
                    app_state.channels_helper.manual_import(
                        channel, game_id, on_complete=on_done,
                        login_method=login_method,
                    )
                except Exception:
                    self.logger.exception("异步导入失败")
                    LocalRequestHandler._pending_imports[task_id] = {
                        "status": "done", "success": False
                    }

            app_state.run_on_main_thread(do_import)
            return self._json_response(200, {"status": "pending", "task_id": task_id})
        else:
            # 同步模式（旧 HTTP 路径）
            success = app_state.channels_helper.manual_import(
                args.get("channel", ""), args.get("game_id", "")
            )
            return self._json_response(200, {"success": success})

    def _import_status(self, args, body, method):
        task_id = args.get("task_id", "")
        task = LocalRequestHandler._pending_imports.get(task_id)
        if task is None:
            return self._json_response(404, {"error": "Unknown task_id"})
        result = dict(task)
        if task["status"] == "done":
            del LocalRequestHandler._pending_imports[task_id]
        return self._json_response(200, result)

    def _set_default(self, args, body, method):
        try:
            game_id = args["game_id"]
            genv.set(f"auto-{game_id}", args["uuid"], True)
            return self._json_response(200, {"success": True})
        except Exception:
            self.logger.exception("设置默认账号失败")
            return self._json_response(200, {"success": False})

    def _clear_default(self, args, body, method):
        try:
            game_id = args["game_id"]
            genv.set(f"auto-{game_id}", "", True)
            return self._json_response(200, {"success": True})
        except Exception:
            return self._json_response(200, {"success": False})

    def _get_auto_close_state(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args.get("installation_id", "")
            distribution_id = int(args.get("distribution_id", -1))
            return self._json_response(200, {
                "success": True,
                "state": self.game_helper.get_auto_close_setting(
                    gid, installation_id, distribution_id
                ),
                "game_id": gid,
                "installation_id": installation_id,
                "distribution_id": distribution_id,
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _switch_auto_close_state(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args.get("installation_id", "")
            distribution_id = int(args.get("distribution_id", -1))
            new_state = not self.game_helper.get_auto_close_setting(
                gid, installation_id, distribution_id
            )
            if not self.game_helper.set_auto_close_setting(
                gid, new_state, installation_id, distribution_id
            ):
                return self._json_response(200, {
                    "success": False, "error": "游戏记录已失效，请刷新后重试"
                })
            return self._json_response(200, {
                "success": True,
                "state": new_state,
                "game_id": gid,
                "installation_id": installation_id,
                "distribution_id": distribution_id,
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _scan_record_setting(self, args, body, method):
        """开关 1：是否开启扫码记录渠道服账号功能。"""
        try:
            if method == "GET":
                enabled = genv.get("SCAN_RECORD_ENABLED", True)
                return self._json_response(200, {"success": True, "enabled": enabled})

            enabled = (body or {}).get("enabled", True)
            was_enabled = genv.get("SCAN_RECORD_ENABLED", True)

            if enabled and not was_enabled:
                self.stack_mgr._pending_login_info_stack = {}

            genv.set("SCAN_RECORD_ENABLED", bool(enabled), True)

            if not enabled:
                genv.set("NATIVE_SAVE_ENABLED", False, True)

            return self._json_response(200, {
                "success": True,
                "enabled": bool(enabled),
                "native_save_enabled": genv.get("NATIVE_SAVE_ENABLED", False),
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _native_save_setting(self, args, body, method):
        """开关 2：是否启用原生渠道服保存（is_remember 注入）。"""
        try:
            if method == "GET":
                enabled = genv.get("NATIVE_SAVE_ENABLED", False)
                scan_record = genv.get("SCAN_RECORD_ENABLED", True)
                return self._json_response(200, {
                    "success": True,
                    "enabled": enabled,
                    "scan_record_enabled": scan_record,
                })

            enabled = (body or {}).get("enabled", False)
            scan_record = genv.get("SCAN_RECORD_ENABLED", True)

            if enabled and not scan_record:
                return self._json_response(200, {
                    "success": False,
                    "error": "需要先开启扫码记录功能",
                })

            genv.set("NATIVE_SAVE_ENABLED", bool(enabled), True)
            return self._json_response(200, {"success": True, "enabled": bool(enabled)})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _get_game_auto_start(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args.get("installation_id", "")
            distribution_id = int(args.get("distribution_id", -1))
            info = self.game_helper.get_game_auto_start(
                gid, installation_id, distribution_id
            )
            return self._json_response(200, {
                "success": True, 
                "enabled": info["enabled"], 
                "path": info["path"], 
                "installation_id": info.get("installation_id", ""),
                "distribution_id": distribution_id,
                "game_id": gid,
                "independent_path_config": True
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _set_game_auto_start(self, args, body, method):
        try:
            gid = args["game_id"]
            enabled = args.get("enabled") == "true"
            update_mode = args.get("update_mode", "")
            installation_id = args.get("installation_id", "")
            distribution_id = int(args.get("distribution_id", -1))
            game_path = ""

            game = self.game_helper.get_game(gid)
            if not game and update_mode:
                return self._json_response(200, {"success": False, "error": "游戏记录不存在"})

            if update_mode == "status_only":
                if not self.game_helper.set_game_auto_start(
                    gid, enabled, installation_id, distribution_id
                ):
                    return self._json_response(200, {
                        "success": False, "error": "游戏记录已失效，请刷新后重试"
                    })
                info = self.game_helper.get_game_auto_start(
                    gid, installation_id, distribution_id
                )
                return self._json_response(200, {
                    "success": True,
                    "enabled": enabled,
                    "path": info.get("path", ""),
                    "installation_id": info.get("installation_id", ""),
                    "distribution_id": distribution_id,
                    "game_id": gid,
                })

            if enabled or update_mode == "path_only":
                try:
                    from PyQt6.QtWidgets import QApplication
                    app = QApplication.instance()
                except Exception:
                    app = None

                if app and app.property("_main_loop_running"):
                    import uuid as uuid_mod
                    task_id = str(uuid_mod.uuid4())
                    LocalRequestHandler._pending_imports[task_id] = {"status": "pending"}

                    game_helper = self.game_helper
                    logger = self.logger

                    def do_select():
                        try:
                            from PyQt6.QtWidgets import QFileDialog, QWidget
                            from PyQt6.QtCore import Qt
                            dummy_parent = QWidget()
                            dummy_parent.setWindowFlags(Qt.WindowType.Tool)
                            dummy_parent.show()
                            dummy_parent.raise_()
                            dummy_parent.activateWindow()
                            LocalRequestHandler._force_dialog_foreground(dummy_parent)
                            desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
                            sel_path, _ = QFileDialog.getOpenFileName(
                                dummy_parent,
                                "选择游戏启动程序或快捷方式",
                                desktop_path,
                                "游戏文件 (*.exe *.lnk);;所有文件 (*.*)",
                            )
                            dummy_parent.close()

                            if not sel_path:
                                LocalRequestHandler._pending_imports[task_id] = {
                                    "status": "done", "success": False, "cancelled": True,
                                    "enabled": False, "path": "",
                                }
                                return

                            name = os.path.splitext(os.path.basename(sel_path))[0]
                            if sel_path.lower().endswith(".lnk") and sys.platform == "win32":
                                import win32com.client
                                shell = win32com.client.Dispatch("WScript.Shell")
                                shortcut = shell.CreateShortcut(sel_path)
                                sel_path = shortcut.Targetpath

                            game_helper.set_game_auto_start(
                                gid, True, installation_id, distribution_id
                            )
                            game_helper.set_game_path(gid, sel_path)
                            selected_game = game_helper.get_game(gid)
                            if selected_game and (
                                not selected_game.name or selected_game.name == selected_game.game_id
                            ):
                                game_helper.rename_game(gid, name)
                            LocalRequestHandler._pending_imports[task_id] = {
                                "status": "done", "success": True,
                                "enabled": True, "path": sel_path, "game_id": gid,
                            }
                        except Exception as e:
                            logger.exception("异步选择游戏路径失败")
                            LocalRequestHandler._pending_imports[task_id] = {
                                "status": "done", "success": False, "error": str(e)
                            }

                    app_state.run_on_main_thread(do_select)
                    return self._json_response(200, {"status": "pending", "task_id": task_id})

                # 同步模式 fallback
                from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget
                from PyQt6.QtCore import Qt

                app_inst = QApplication.instance()
                if app_inst is None:
                    app_inst = QApplication(sys.argv)

                dummy_parent = QWidget()
                dummy_parent.setWindowFlags(Qt.WindowType.Tool)
                dummy_parent.show()
                dummy_parent.raise_()
                dummy_parent.activateWindow()
                self._force_dialog_foreground(dummy_parent)

                desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
                game_path, _ = QFileDialog.getOpenFileName(
                    dummy_parent,
                    "选择游戏启动程序或快捷方式",
                    desktop_path,
                    "游戏文件 (*.exe *.lnk);;所有文件 (*.*)",
                )
                dummy_parent.close()

                if not game_path:
                    return self._json_response(200, {"success": False, "error": "用户取消选择游戏路径"})

                name = os.path.splitext(os.path.basename(game_path))[0]

                if game_path.lower().endswith(".lnk") and sys.platform == "win32":
                    import win32com.client
                    shell = win32com.client.Dispatch("WScript.Shell")
                    shortcut = shell.CreateShortcut(game_path)
                    game_path = shortcut.Targetpath
                
                # 如果是仅更新路径或原本是因为开启而调用的选路径，均绑定下发 enabled=True
                enabled = True
            else:
                game_path = ""
                name = game.name if game else ""

            self.game_helper.set_game_auto_start(
                gid, enabled, installation_id, distribution_id
            )
            self.game_helper.set_game_path(gid, game_path)
            selected_game = self.game_helper.get_game(gid)
            if selected_game and (
                not selected_game.name or selected_game.name == selected_game.game_id
            ):
                self.game_helper.rename_game(gid, name)
            return self._json_response(200, {
                "success": True, "enabled": enabled, "path": game_path, "game_id": gid
            })
        except Exception as e:
            self.logger.exception(f"设置游戏 {args.get('game_id', '')} 的自动启动状态失败")
            return self._json_response(200, {"success": False, "error": str(e)})

    def _start_game(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args.get("installation_id", "")
            game = self.game_helper.get_game(gid)
            installation = self._resolve_installation(game, installation_id)
            path = installation.path if installation else ""
            if not path:
                return self._json_response(200, {"success": False, "error": "游戏路径未设置"})
            if game:
                if not game.start(installation.installation_id):
                    return self._json_response(200, {
                        "success": False,
                        "error": game.last_start_error or "游戏启动失败，请检查安装路径",
                    })
                game.last_used_time = int(time.time())
                self.game_helper._save_games()
            return self._json_response(200, {"success": True, "game_id": gid})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _list_games(self, args, body, method):
        try:
            games = self.game_helper.list_games()
            catalog_all = CloudRes().get_dynamic_game_catalog()
            saved_short_ids = {
                getShortGameId(item.get("game_id", "")) for item in games
            }
            catalog = [
                item for item in catalog_all
                if item.get("short_game_id") not in saved_short_ids
            ]
            return self._json_response(200, {
                "success": True,
                "games": games,
                "catalog": catalog,
                # New UI may enrich recorded games with public visual metadata.
                # The legacy `catalog` meaning stays unchanged for old pages.
                "catalog_all": catalog_all,
                "catalog_status": CloudRes().get_dynamic_game_catalog_status(),
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _launcher_status(self, args, body, method):
        try:
            from nativebridge import NativeTaskRegistry

            gid = args["game_id"]
            short_gid = getShortGameId(gid)
            catalog_item = CloudRes().dynamic_game_catalog.get_game(short_gid) or {}

            # Invalid paths are not useful launcher choices.  Remove them
            # immediately instead of keeping a misleading "installation".
            known_game = self.game_helper.get_existing_game(gid)
            if known_game:
                for item in list(known_game.installations.values()):
                    if item.source == "download_pending" and os.path.exists(item.path):
                        # The elevated download supervisor has persisted the
                        # marker; reconcile this process's in-memory model.
                        self.game_helper.set_game_path(gid, item.path)
                from nativebridge import NativeTaskRegistry
                invalid_ids = [
                    item.installation_id
                    for item in known_game.installations.values()
                    if (
                        (not item.path or not os.path.exists(item.path))
                        and not (
                            item.source == "download_pending"
                            and NativeTaskRegistry.has_pending_installation(
                                item.installation_id
                            )
                        )
                    )
                ]
                if invalid_ids:
                    for installation_id in invalid_ids:
                        known_game.remove_installation(installation_id)
                    self.game_helper._save_games()

            game = self.game_helper.get_existing_game(gid)
            game_for_remote = self.game_helper.get_game_or_temp(gid)
            distribution_options = game_for_remote.get_distribution_options()
            distribution_ids = game_for_remote._normalize_distribution_ids(
                distribution_options
            )
            distribution_details = []
            file_info_by_distribution = {}
            launcher_data_by_distribution = {}
            for dist_id in distribution_ids:
                launcher_data = game_for_remote.get_launcher_data_for_distribution(dist_id)
                file_info = game_for_remote.get_file_distribution_info(dist_id)
                launcher_data_by_distribution[dist_id] = launcher_data or {}
                file_info_by_distribution[dist_id] = file_info
                distribution_details.append((dist_id, launcher_data, file_info))
            if game:
                identified = False
                for item in list(game.installations.values()):
                    if item.distribution_id != -1:
                        continue
                    old_distribution_id = item.distribution_id
                    game.identify_installation_distribution(
                        item.installation_id,
                        distribution_options,
                        file_info_by_distribution,
                        launcher_data_by_distribution,
                    )
                    identified = identified or item.distribution_id != old_distribution_id
                if identified:
                    self.game_helper._save_games()
            installations = (
                [item.get_non_sensitive_data() for item in game.installations.values()]
                if game else []
            )
            fever_records = [
                item for item in self.game_helper.list_fever_games()
                if (
                    getShortGameId(item.get("game_id", "")) == short_gid
                    and os.path.isfile(str(item.get("path") or ""))
                )
            ]
            distributions = []
            for dist_id, launcher_data, file_info in distribution_details:
                active_task = NativeTaskRegistry.find_pending_download(gid, dist_id)
                target_ver = file_info.get("version_code", "") if file_info else ""
                can_download = CloudRes().is_downloadable(short_gid) and file_info is not None
                files = file_info.get("files", []) if file_info else []
                matching_installations = [
                    item for item in installations
                    if item.get("distribution_id") == dist_id
                ]
                installation = matching_installations[0] if matching_installations else None
                fever_match = next(
                    (
                        item for item in fever_records
                        if int(item.get("distribution_id", -1)) == dist_id
                    ),
                    None,
                )
                fever_path = os.path.normcase(os.path.normpath(
                    str((fever_match or {}).get("path") or "")
                ))
                installation_path = os.path.normcase(os.path.normpath(
                    str((installation or {}).get("path") or "")
                ))
                distribution_can_import = bool(
                    fever_match and fever_path and fever_path != installation_path
                )
                needs_update = bool(
                    installation
                    and installation.get("installed")
                    and can_download
                    and target_ver
                    and str(installation.get("installed_version") or "")
                    != str(target_ver)
                )
                distributions.append({
                    "distribution_id": dist_id,
                    "launcher": self._pick_launcher_fields(launcher_data),
                    "target_version": target_ver,
                    "can_download": can_download,
                    "install_requirements": {
                        "download_bytes": sum(
                            game_for_remote._extract_file_size(item)
                            for item in files
                            if isinstance(item, dict) and item.get("op", 1) == 1
                        ),
                        "file_count": sum(
                            1 for item in files
                            if isinstance(item, dict) and item.get("op", 1) == 1
                        ),
                    },
                    "installation": installation,
                    "fever": fever_match or {},
                    "can_import_fever": distribution_can_import,
                    "can_update": bool(installation and installation.get("installed")),
                    "needs_update": needs_update,
                    "active_task": self._public_download_task(active_task),
                })
            return self._json_response(200, {
                "success": True, "game_id": gid,
                "installation_model_version": 1,
                "platform_type": catalog_item.get("platform_type", "fever"),
                "catalog_app_id": catalog_item.get("catalog_app_id"),
                "game": {
                    "default_distribution": game.get_default_distribution() if game else -1,
                },
                "distributions": distributions,
            })
        except Exception as e:
            self.logger.exception("获取启动器状态失败")
            return self._json_response(200, {"success": False, "error": str(e)})

    def _launcher_locate(self, args, body, method):
        try:
            payload = body or args or {}
            gid = str(payload.get("game_id") or "")
            executable_path = os.path.normpath(str(payload.get("path") or "").strip())
            if not gid or not executable_path:
                return self._json_response(400, {
                    "success": False, "error": "缺少游戏或启动程序路径"
                })
            if not os.path.isfile(executable_path):
                return self._json_response(400, {
                    "success": False, "error": "所选游戏启动程序不存在"
                })
            if sys.platform == "win32" and not executable_path.lower().endswith((".exe", ".lnk")):
                return self._json_response(400, {
                    "success": False, "error": "请选择 .exe 或 .lnk 游戏启动程序"
                })

            if not self.game_helper.set_game_path(gid, executable_path):
                return self._json_response(500, {
                    "success": False, "error": "保存游戏路径失败"
                })
            game = self.game_helper.get_existing_game(gid)
            installation = game.get_installation() if game else None
            if installation:
                if not installation.startup_path:
                    installation.startup_path = os.path.basename(executable_path)
                launcher_data = (
                    game.get_launcher_data_for_distribution(
                        installation.distribution_id
                    )
                    if installation.distribution_id != -1
                    else None
                ) or {}
                if not installation.startup_args:
                    installation.startup_args = str(
                        launcher_data.get("startup_params") or ""
                    )
                self.game_helper._save_games()
            return self._json_response(200, {
                "success": True,
                "game_id": gid,
                "installation_id": installation.installation_id if installation else "",
                "path": executable_path,
            })
        except Exception as e:
            self.logger.exception("定位游戏失败")
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_install(self, args, body, method):
        try:
            if sys.platform != "win32":
                return self._json_response(400, {"success": False, "error": "当前平台不支持安装"})
            gid = args["game_id"]
            dist_id = int(args["distribution_id"])
            game = self.game_helper.get_game(gid)
            launcher_data = game.get_launcher_data_for_distribution(dist_id)
            if not launcher_data:
                return self._json_response(404, {"success": False, "error": "未找到启动器信息"})
            startup_path = launcher_data.get("startup_path", "")
            if not startup_path:
                return self._json_response(400, {"success": False, "error": "启动器缺少启动路径"})
            startup_parts = str(startup_path).replace("\\", "/").split("/")
            if os.path.isabs(startup_path) or ".." in startup_parts:
                return self._json_response(400, {
                    "success": False, "error": "启动路径必须位于所选安装目录内"
                })
            file_info = game.get_file_distribution_info(dist_id)
            if not file_info:
                return self._json_response(404, {"success": False, "error": "未找到文件分发信息"})
            startup_args = launcher_data.get("startup_params", "") or ""
            content_id = file_info.get("app_content_id")

            # New browser/WebEngine UI: directory selection is a separate HTTP
            # native task, so hashing and download preparation can run off the
            # Qt event loop.  Omitting target_dir keeps the released HTML flow.
            payload = body or args or {}
            requested_target_dir = str(payload.get("target_dir") or "").strip()
            if requested_target_dir:
                from nativebridge import NativeTaskRegistry, inspect_path

                path_status = inspect_path(requested_target_dir)
                if (
                    not path_status.get("exists")
                    or not path_status.get("is_directory")
                    or not path_status.get("writable")
                    or path_status.get("is_drive_root")
                ):
                    return self._json_response(400, {
                        "success": False,
                        "error": "安装目录无效、不可写，或不能直接使用磁盘根目录",
                        "path_status": path_status,
                    })
                target_dir = path_status["normalized_path"]
                task_id = NativeTaskRegistry.create("launcher-install")
                progress_file = os.path.join(
                    genv.get("FP_WORKDIR", os.getcwd()),
                    f"download_status_{task_id}.json",
                )
                control_file = self._download_control_file(task_id)
                NativeTaskRegistry.update(
                    task_id,
                    status_file=progress_file,
                    control_file=control_file,
                )
                NativeTaskRegistry.update(
                    task_id,
                    game_id=gid,
                    distribution_id=dist_id,
                    target_version=file_info.get("version_code", ""),
                    content_id=content_id,
                )
                max_conc = int(payload.get("concurrent", "4"))
                game_helper = self.game_helper
                logger = self.logger

                def install_in_background():
                    installation = None
                    created_installation = False
                    try:
                        NativeTaskRegistry.update(
                            task_id, phase="preparing", progress_percent=0
                        )
                        game_path = os.path.join(target_dir, startup_path)
                        display_name = (
                            launcher_data.get("display_name")
                            or launcher_data.get("app_name")
                            or gid
                        )
                        game_helper.rename_game(gid, display_name)
                        existing_installation_ids = set(game.installations)
                        installation = game_helper.add_game_installation(
                            game_id=gid,
                            path=game_path,
                            distribution_id=dist_id,
                            source="download_pending",
                            content_id=content_id,
                            startup_path=startup_path,
                            startup_args=startup_args,
                            set_default=True,
                        )
                        if installation is None:
                            raise RuntimeError("创建游戏安装记录失败")
                        created_installation = (
                            installation.installation_id
                            not in existing_installation_ids
                        )
                        NativeTaskRegistry.update(
                            task_id,
                            phase="checking",
                            progress_percent=0,
                            installation_id=installation.installation_id,
                            created_installation=created_installation,
                            path=game_path,
                        )
                        updated = game.try_update(
                            dist_id,
                            max_conc,
                            installation.installation_id,
                            progress_file=progress_file,
                            control_file=control_file,
                        )
                        if not updated:
                            raise RuntimeError("启动游戏下载失败")
                        if not game.last_update_async:
                            installation.source = "download"
                            sgid = getShortGameId(gid)
                            if CloudRes().is_convert_to_normal(sgid):
                                game.create_tool_launch_shortcut(
                                    installation.path,
                                    installation.installation_id,
                                )
                        game_helper._save_games()
                        task_values = {
                            "success": True,
                            "phase": "download_started" if game.last_update_async else "finished",
                            "progress_percent": 100 if not game.last_update_async else 0,
                            "path": game_path,
                            "installation_id": installation.installation_id,
                            "version": installation.installed_version,
                            "download_async": bool(game.last_update_async),
                        }
                        if game.last_update_async:
                            NativeTaskRegistry.update(task_id, **task_values)
                        else:
                            NativeTaskRegistry.finish(task_id, **task_values)
                    except Exception as exc:
                        if installation is not None and created_installation:
                            game.remove_installation(installation.installation_id)
                            game_helper._save_games()
                        logger.exception("后台安装启动器失败")
                        NativeTaskRegistry.finish(
                            task_id,
                            success=False,
                            phase="failed",
                            error=str(exc),
                        )

                threading.Thread(
                    target=install_in_background,
                    name=f"launcher-install-{task_id[:8]}",
                    daemon=True,
                ).start()
                return self._json_response(202, {
                    "success": True,
                    "status": "pending",
                    "task_id": task_id,
                })

            try:
                from PyQt6.QtWidgets import QApplication
                app = QApplication.instance()
            except Exception:
                app = None

            if app and app.property("_main_loop_running"):
                import uuid as uuid_mod
                task_id = str(uuid_mod.uuid4())
                LocalRequestHandler._pending_imports[task_id] = {"status": "pending"}

                game_helper = self.game_helper
                logger = self.logger
                max_conc = int(args.get("concurrent", "4"))

                def do_install():
                    try:
                        from PyQt6.QtWidgets import QFileDialog, QWidget
                        from PyQt6.QtCore import Qt
                        dummy = QWidget()
                        dummy.setWindowFlags(Qt.WindowType.Tool)
                        dummy.show()
                        dummy.raise_()
                        dummy.activateWindow()
                        LocalRequestHandler._force_dialog_foreground(dummy)
                        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                        target_dir = QFileDialog.getExistingDirectory(
                            dummy, "选择安装目录", desktop,
                            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
                        )
                        dummy.close()
                        if not target_dir:
                            LocalRequestHandler._pending_imports[task_id] = {
                                "status": "done", "success": False, "cancelled": True,
                                "error": "用户取消选择安装目录",
                            }
                            return
                        os.makedirs(target_dir, exist_ok=True)
                        game_path = os.path.join(target_dir, startup_path)
                        display_name = launcher_data.get("display_name") or launcher_data.get("app_name") or gid
                        game_helper.rename_game(gid, display_name)
                        existing_installation_ids = set(game.installations)
                        installation = game_helper.add_game_installation(
                            game_id=gid,
                            path=game_path,
                            distribution_id=dist_id,
                            source="download",
                            content_id=content_id,
                            startup_path=startup_path,
                            startup_args=startup_args,
                            set_default=True,
                        )
                        if installation is None:
                            raise RuntimeError("创建游戏安装记录失败")
                        updated = game.try_update(
                            dist_id, max_conc, installation.installation_id
                        )
                        if not updated and installation.installation_id not in existing_installation_ids:
                            game.remove_installation(installation.installation_id)
                            game_helper._save_games()
                        if updated and not game.last_update_async:
                            installation.source = "download"
                            sgid = getShortGameId(gid)
                            if CloudRes().is_convert_to_normal(sgid):
                                game.create_tool_launch_shortcut(
                                    installation.path, installation.installation_id
                                )
                        game_helper._save_games()
                        LocalRequestHandler._pending_imports[task_id] = {
                            "status": "done", "success": updated,
                            "path": game_path,
                            "installation_id": installation.installation_id,
                            "version": installation.installed_version,
                        }
                    except Exception as e:
                        logger.exception("异步安装启动器失败")
                        LocalRequestHandler._pending_imports[task_id] = {
                            "status": "done", "success": False, "error": str(e)
                        }

                app_state.run_on_main_thread(do_install)
                return self._json_response(200, {"status": "pending", "task_id": task_id})

            # 同步模式 fallback
            from PyQt6.QtWidgets import QApplication, QFileDialog, QWidget
            from PyQt6.QtCore import Qt
            app_inst = QApplication.instance()
            if app_inst is None:
                app_inst = QApplication(sys.argv)
            dummy = QWidget()
            dummy.setWindowFlags(Qt.WindowType.Tool)
            dummy.show()
            dummy.raise_()
            dummy.activateWindow()
            self._force_dialog_foreground(dummy)
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            target_dir = QFileDialog.getExistingDirectory(
                dummy, "选择安装目录", desktop,
                QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks,
            )
            dummy.close()
            if not target_dir:
                return self._json_response(400, {"success": False, "error": "用户取消选择安装目录"})
            os.makedirs(target_dir, exist_ok=True)
            game_path = os.path.join(target_dir, startup_path)
            display_name = launcher_data.get("display_name") or launcher_data.get("app_name") or gid
            self.game_helper.rename_game(gid, display_name)
            existing_installation_ids = set(game.installations)
            installation = self.game_helper.add_game_installation(
                game_id=gid,
                path=game_path,
                distribution_id=dist_id,
                source="download",
                content_id=content_id,
                startup_path=startup_path,
                startup_args=startup_args,
                set_default=True,
            )
            if installation is None:
                return self._json_response(500, {"success": False, "error": "创建游戏安装记录失败"})
            max_conc = int(args.get("concurrent", "4"))
            updated = game.try_update(dist_id, max_conc, installation.installation_id)
            if not updated and installation.installation_id not in existing_installation_ids:
                game.remove_installation(installation.installation_id)
                self.game_helper._save_games()
            if updated and not game.last_update_async:
                installation.source = "download"
                sgid = getShortGameId(gid)
                if CloudRes().is_convert_to_normal(sgid):
                    game.create_tool_launch_shortcut(
                        installation.path, installation.installation_id
                    )
            self.game_helper._save_games()
            return self._json_response(200, {
                "success": updated,
                "path": game_path,
                "installation_id": installation.installation_id,
                "version": installation.installed_version,
            })
        except Exception as e:
            self.logger.exception("安装启动器失败")
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_update(self, args, body, method):
        try:
            gid = args["game_id"]
            dist_id = int(args["distribution_id"])
            installation_id = args.get("installation_id", "")
            game = self.game_helper.get_existing_game(gid)
            installation = self._resolve_installation(game, installation_id, dist_id)
            if not installation or not installation.path or not os.path.exists(installation.path):
                return self._json_response(404, {"success": False, "error": "未找到已安装的游戏"})
            from nativebridge import NativeTaskRegistry

            task_id = NativeTaskRegistry.create("launcher-update")
            progress_file = os.path.join(
                genv.get("FP_WORKDIR", os.getcwd()),
                f"download_status_{task_id}.json",
            )
            control_file = self._download_control_file(task_id)
            NativeTaskRegistry.update(
                task_id,
                status_file=progress_file,
                control_file=control_file,
                phase="checking",
                progress_percent=0,
                game_id=gid,
                distribution_id=dist_id,
                installation_id=installation.installation_id,
            )
            max_conc = int(args.get("concurrent", "4"))

            def update_in_background():
                try:
                    updated = game.try_update(
                        dist_id,
                        max_conc,
                        installation.installation_id,
                        progress_file=progress_file,
                        control_file=control_file,
                    )
                    if not updated:
                        raise RuntimeError("启动游戏更新失败")
                    if not game.last_update_async:
                        sgid = getShortGameId(gid)
                        if CloudRes().is_convert_to_normal(sgid):
                            game.create_tool_launch_shortcut(
                                installation.path, installation.installation_id
                            )
                    self.game_helper._save_games()
                    values = {
                        "success": True,
                        "phase": "download_started" if game.last_update_async else "finished",
                        "progress_percent": 0 if game.last_update_async else 100,
                        "installation_id": installation.installation_id,
                        "version": installation.installed_version,
                        "download_async": bool(game.last_update_async),
                    }
                    if game.last_update_async:
                        NativeTaskRegistry.update(task_id, **values)
                    else:
                        NativeTaskRegistry.finish(task_id, **values)
                except Exception as exc:
                    self.logger.exception("后台更新游戏失败")
                    NativeTaskRegistry.finish(
                        task_id,
                        success=False,
                        phase="failed",
                        error=str(exc),
                    )

            threading.Thread(
                target=update_in_background,
                name=f"launcher-update-{task_id[:8]}",
                daemon=True,
            ).start()
            return self._json_response(202, {
                "success": True,
                "status": "pending",
                "task_id": task_id,
                "installation_id": installation.installation_id,
            })
        except Exception as e:
            self.logger.exception("更新启动器失败")
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_update_info(self, args, body, method):
        try:
            gid = args["game_id"]
            dist_id = int(args["distribution_id"])
            installation_id = args.get("installation_id", "")
            game = self.game_helper.get_existing_game(gid)
            installation = self._resolve_installation(game, installation_id, dist_id)
            if not installation or not installation.path or not os.path.exists(installation.path):
                return self._json_response(404, {"success": False, "error": "未找到已安装的游戏"})
            if str(args.get("async", "")) == "1":
                from nativebridge import NativeTaskRegistry

                task_id = NativeTaskRegistry.create("launcher-update-info")

                def calculate_update_info():
                    try:
                        stats = game.get_update_stats(
                            dist_id, installation.installation_id
                        )
                        if not stats:
                            raise RuntimeError("未找到更新信息")
                        NativeTaskRegistry.finish(
                            task_id,
                            success=True,
                            game_id=gid,
                            installation_id=installation.installation_id,
                            distribution_id=dist_id,
                            **stats,
                        )
                    except Exception as exc:
                        self.logger.exception("后台获取更新信息失败")
                        NativeTaskRegistry.finish(
                            task_id, success=False, error=str(exc)
                        )

                threading.Thread(
                    target=calculate_update_info,
                    name=f"launcher-update-info-{task_id[:8]}",
                    daemon=True,
                ).start()
                return self._json_response(202, {
                    "success": True,
                    "status": "pending",
                    "task_id": task_id,
                })
            stats = game.get_update_stats(dist_id, installation.installation_id)
            if not stats:
                return self._json_response(404, {"success": False, "error": "未找到更新信息"})
            return self._json_response(200, {
                "success": True,
                "game_id": gid,
                "installation_id": installation.installation_id,
                "distribution_id": dist_id,
                **stats,
            })
        except Exception as e:
            self.logger.exception("获取更新信息失败")
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_import_fever(self, args, body, method):
        try:
            distribution_id = int(args.get("distribution_id", -1))
            imported = self.game_helper.import_fever_game(
                args["game_id"], distribution_id, args.get("path", "")
            )
            if not imported:
                return self._json_response(404, {"success": False, "error": "未找到可导入的Fever游戏记录"})
            game = self.game_helper.get_existing_game(imported)
            return self._json_response(200, {
                "success": True,
                "game_id": imported,
                "installation_id": game.default_installation_id if game else "",
            })
        except Exception as e:
            self.logger.exception("导入Fever游戏失败")
            return self._json_response(500, {"success": False, "error": str(e)})

    def _list_fever_games(self, args, body, method):
        try:
            result = []
            for item in self.game_helper.list_fever_games():
                short_id = item.get("game_id")
                matched = self.game_helper.find_matching_game_id(short_id)
                result.append({
                    "fever_id": item.get("fever_id"),
                    "game_id": short_id,
                    "display_name": item.get("display_name"),
                    "path": item.get("path"),
                    "distribution_id": item.get("distribution_id", -1),
                    "version_code": item.get("version_code", ""),
                    "content_id": item.get("content_id"),
                    "matched_game_id": matched,
                })
            return self._json_response(200, {"success": True, "games": result})
        except Exception as e:
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_set_default(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args["installation_id"]
            if not self.game_helper.set_game_default_installation(gid, installation_id):
                return self._json_response(404, {
                    "success": False, "error": "安装记录不存在"
                })
            return self._json_response(200, {
                "success": True,
                "game_id": gid,
                "installation_id": installation_id,
            })
        except Exception as e:
            return self._json_response(500, {"success": False, "error": str(e)})

    def _launcher_remove_installation(self, args, body, method):
        try:
            gid = args["game_id"]
            installation_id = args["installation_id"]
            if not self.game_helper.remove_game_installation(gid, installation_id):
                return self._json_response(404, {
                    "success": False, "error": "安装记录不存在"
                })
            return self._json_response(200, {
                "success": True,
                "game_id": gid,
                "installation_id": installation_id,
            })
        except Exception as e:
            return self._json_response(500, {"success": False, "error": str(e)})

    def _get_default_channel(self, args, body, method):
        uuid = genv.get(f"auto-{args.get('game_id', '')}", "")
        if uuid and app_state.channels_helper.query_channel(uuid) is None:
            genv.set(f"auto-{args.get('game_id', '')}", "", True)
            uuid = ""
        return self._json_response(200, {"uuid": uuid})

    def _get_login_delay(self, args, body, method):
        installation_id = args.get("installation_id", "")
        distribution_id = int(args.get("distribution_id", -1))
        return self._json_response(200, {
            "delay": self.game_helper.get_login_delay(
                args.get("game_id", ""), installation_id, distribution_id
            ),
            "installation_id": installation_id,
            "distribution_id": distribution_id,
        })

    def _set_login_delay(self, args, body, method):
        try:
            installation_id = args.get("installation_id", "")
            distribution_id = int(args.get("distribution_id", -1))
            if not self.game_helper.set_login_delay(
                args["game_id"],
                int(args["delay"]),
                installation_id,
                distribution_id,
            ):
                return self._json_response(200, {
                    "success": False, "error": "游戏记录已失效，请刷新后重试"
                })
            return self._json_response(200, {"success": True})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    # -- Cloud sync routes ──

    def _cloud_sync_policy(self, args, body, method):
        return self._json_response(200, {
            "success": True,
            "policy": {
                "storage": "云端仅保存密文，不保存主密钥。系统使用主密钥+不同盐值派生 note_id、note密码、AES密钥。",
                "credential_levels": {
                    "none": "不记住主密钥；每次同步手动输入。",
                    "master_key": "记住主密钥；可用于自动同步。",
                },
                "permissions": {
                    "master_key": "主密钥是唯一凭证，可访问/修改/删除记录，并解密云端密文。",
                },
            },
        })

    def _cloud_sync_generate_key(self, args, body, method):
        try:
            payload = body or {}
            length = int(payload.get("length", 16) or 16)
            mk = self.cloud_sync_mgr.generate_master_key(length)
            return self._json_response(200, {
                "success": True, "master_key": mk,
                "strength": self.cloud_sync_mgr.evaluate_master_key_strength(mk),
            })
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_settings(self, args, body, method):
        if method == "GET":
            return self._json_response(200, {"success": True, "settings": self._get_cloud_sync_settings()})
        try:
            payload = body or {}
            settings = self._get_cloud_sync_settings()
            consent_ack = bool(payload.get("consent_ack", False))
            remember_level = str(payload.get("remember_level", "none"))
            if remember_level not in ("none", "master_key"):
                return self._json_response(200, {"success": False, "error": "记住密码级别无效"})
            auto_sync = bool(payload.get("auto_sync", False))
            settings["consent_ack"] = consent_ack
            if auto_sync and not consent_ack:
                return self._json_response(200, {"success": False, "error": "启用自动同步前请先同意存储与权限说明"})
            settings["remember_level"] = remember_level
            settings["auto_sync"] = auto_sync
            settings["sync_direction"] = str(payload.get("sync_direction", "bidirectional"))
            settings["scope_type"] = str(payload.get("scope_type", "all"))
            settings["scope_game_id"] = str(payload.get("scope_game_id", ""))
            settings["scope_uuids"] = (
                payload.get("scope_uuids", []) if isinstance(payload.get("scope_uuids", []), list) else []
            )
            settings["expire_time"] = int(payload.get("expire_time", 259200) or 259200)
            mk = str(payload.get("master_key", ""))
            if mk:
                strength = self.cloud_sync_mgr.evaluate_master_key_strength(mk)
                if not strength.get("valid", False):
                    return self._json_response(200, {"success": False, "error": "主密钥强度不足：至少12位且包含3类字符"})
            if remember_level == "master_key" and not mk:
                return self._json_response(200, {"success": False, "error": "选择记住主密钥时，必须提供主密钥"})
            settings["saved_master_key"] = mk if remember_level == "master_key" else ""
            self._save_cloud_sync_settings(settings)
            return self._json_response(200, {"success": True, "settings": settings})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_accounts(self, args, body, method):
        try:
            channels_path = genv.get("FP_CHANNEL_RECORD", "")
            channels = []
            if channels_path and os.path.exists(channels_path):
                with open(channels_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    if isinstance(raw, list):
                        for item in raw:
                            channels.append({
                                "uuid": item.get("uuid", ""),
                                "name": item.get("name", ""),
                                "game_id": item.get("game_id", ""),
                                "channel": (item.get("login_info", {}) or {}).get("login_channel", ""),
                            })
            return self._json_response(200, {"success": True, "accounts": channels})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_probe(self, args, body, method):
        try:
            payload = body or {}
            mk = str(payload.get("master_key", ""))
            if not mk:
                return self._json_response(200, {"success": False, "error": "主密钥不能为空"})
            return self._json_response(200, self.cloud_sync_mgr.probe_remote(mk))
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_run(self, args, body, method):
        try:
            payload = body or {}
            settings = self._get_cloud_sync_settings()
            if not settings.get("consent_ack", False) and not bool(payload.get("consent_ack", False)):
                return self._json_response(200, {"success": False, "error": "请先同意云同步存储与权限说明"})
            action = str(payload.get("action", "sync"))
            if action == "auto" and not settings.get("auto_sync", False):
                return self._json_response(200, {"success": True, "skipped": True, "reason": "auto_sync_disabled"})
            mk = self._resolve_master_key(payload)
            if not mk:
                return self._json_response(200, {"success": False, "error": "主密钥不能为空"})
            strength = self.cloud_sync_mgr.evaluate_master_key_strength(mk)
            if not strength.get("valid", False):
                return self._json_response(200, {"success": False, "error": "主密钥强度不足：至少12位且包含3类字符"})
            scope = self._resolve_scope(payload)
            expire_time = int(payload.get("expire_time", settings.get("expire_time", 259200)) or 259200)
            if action == "push":
                return self._json_response(200, self.cloud_sync_mgr.push(mk, scope, expire_time))
            if action == "pull":
                result = self.cloud_sync_mgr.pull(mk)
                self._refresh_channels_helper()
                return self._json_response(200, result)
            direction = str(payload.get("sync_direction", settings.get("sync_direction", "bidirectional")))
            if direction not in ("push", "pull", "bidirectional"):
                return self._json_response(200, {"success": False, "error": "同步方向无效"})
            steps = []
            if direction in ("pull", "bidirectional"):
                try:
                    steps.append(self.cloud_sync_mgr.pull(mk))
                    self._refresh_channels_helper()
                except Exception as e:
                    self.logger.debug("云同步拉取失败", exc_info=e)
            if direction in ("push", "bidirectional"):
                try:
                    steps.append(self.cloud_sync_mgr.push(mk, scope, expire_time))
                except Exception as e:
                    self.logger.debug("云同步推送失败", exc_info=e)
            return self._json_response(200, {"success": True, "action": "sync", "direction": direction, "steps": steps})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_delete(self, args, body, method):
        try:
            payload = body or {}
            mk = self._resolve_master_key(payload)
            if not mk:
                return self._json_response(200, {"success": False, "error": "删除云同步需要主密钥"})
            result = self.cloud_sync_mgr.delete_remote(mk)
            settings = self._get_cloud_sync_settings()
            settings["auto_sync"] = False
            settings["saved_master_key"] = ""
            settings["remember_level"] = "none"
            settings["consent_ack"] = False
            self._save_cloud_sync_settings(settings)
            return self._json_response(200, result)
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _cloud_sync_access_logs(self, args, body, method):
        try:
            payload = body or {}
            mk = self._resolve_master_key(payload)
            if not mk:
                return self._json_response(200, {"success": False, "error": "查看访问日志需要主密钥"})
            logs = self.cloud_sync_mgr.fetch_access_logs(mk)
            return self._json_response(200, {"success": True, "logs": logs})
        except Exception as e:
            return self._json_response(200, {"success": False, "error": str(e)})

    def _refresh_channels_helper(self):
        try:
            from channelmgr import ChannelManager
            app_state.channels_helper = ChannelManager()
        except Exception:
            self.logger.exception("云同步拉取后刷新账号管理器失败")

    # -- Native browser bridge ──

    def _native_capabilities(self, args, body, method):
        from nativebridge import capabilities
        result = capabilities()
        result["window_drag"] = bool(app_state.ui_mgr)
        result["window_toggle_maximize"] = bool(app_state.ui_mgr)
        return self._json_response(200, result)

    def _native_window_drag(self, args, body, method):
        ui_mgr = app_state.ui_mgr
        started = bool(ui_mgr and ui_mgr.start_system_move())
        return self._json_response(200, {
            "success": started,
            "supported": bool(ui_mgr),
        })

    def _native_window_toggle_maximize(self, args, body, method):
        ui_mgr = app_state.ui_mgr
        changed = bool(ui_mgr and ui_mgr.toggle_maximized())
        return self._json_response(200, {
            "success": changed,
            "supported": bool(ui_mgr),
        })

    def _native_pick_directory(self, args, body, method):
        from nativebridge import start_picker
        payload = body or args or {}
        task_id = start_picker(
            "directory",
            title=str(payload.get("title") or "选择安装目录"),
            default_path=str(payload.get("default_path") or ""),
        )
        return self._json_response(202, {
            "success": True,
            "status": "pending",
            "task_id": task_id,
        })

    def _native_pick_executable(self, args, body, method):
        from nativebridge import start_picker
        payload = body or args or {}
        task_id = start_picker(
            "executable",
            title=str(payload.get("title") or "选择游戏启动程序"),
            default_path=str(payload.get("default_path") or ""),
            file_filter=str(payload.get("file_filter") or ""),
        )
        return self._json_response(202, {
            "success": True,
            "status": "pending",
            "task_id": task_id,
        })

    def _native_path_status(self, args, body, method):
        from nativebridge import inspect_path
        payload = body or args or {}
        return self._json_response(200, inspect_path(str(payload.get("path") or "")))

    def _native_task_status(self, args, body, method):
        from nativebridge import NativeTaskRegistry
        task_id = str(args.get("task_id") or (body or {}).get("task_id") or "")
        task = NativeTaskRegistry.get(task_id)
        if task is None:
            return self._json_response(404, {
                "success": False,
                "error": "Unknown task_id",
            })
        if task.get("status") == "done" and not task.get("reconciled"):
            game = self.game_helper.get_existing_game(str(task.get("game_id") or ""))
            installation = (
                game.get_installation(str(task.get("installation_id") or ""))
                if game else None
            )
            if task.get("success") and installation:
                installation.distribution_id = int(
                    task.get("distribution_id", installation.distribution_id)
                )
                installation.installed_version = str(
                    task.get("target_version") or installation.installed_version
                )
                if task.get("content_id") is not None:
                    installation.content_id = task.get("content_id")
                if task.get("kind") == "launcher-install":
                    installation.source = "download"
                installation.updated_at = int(time.time())
                game.default_installation_id = installation.installation_id
                self.game_helper._save_games()
            elif (
                task.get("kind") == "launcher-install"
                and task.get("created_installation")
                and installation
            ):
                game.remove_installation(installation.installation_id)
                self.game_helper._save_games()
            NativeTaskRegistry.update(task_id, reconciled=True)
            task["reconciled"] = True
        return self._json_response(200, task)

    def _native_download_control(self, args, body, method):
        if method != "POST":
            return self._json_response(405, {
                "success": False,
                "error": "下载控制请求必须使用 POST",
            })
        from nativebridge import NativeTaskRegistry

        payload = body or {}
        task_id = str(payload.get("task_id") or "")
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("pause", "resume"):
            return self._json_response(400, {
                "success": False,
                "error": "下载控制只支持暂停或继续",
            })
        task = NativeTaskRegistry.get(task_id)
        if not task or task.get("kind") not in ("launcher-install", "launcher-update"):
            return self._json_response(404, {
                "success": False,
                "error": "未找到下载任务",
            })
        if task.get("status") != "pending":
            return self._json_response(409, {
                "success": False,
                "error": "下载任务已结束",
            })
        control_file = str(task.get("control_file") or "")
        if not control_file:
            return self._json_response(409, {
                "success": False,
                "error": "下载核心尚未完成控制初始化",
            })

        command = {
            "sequence": time.time_ns(),
            "action": action,
        }
        temporary = f"{control_file}.{threading.get_ident()}.tmp"
        try:
            os.makedirs(os.path.dirname(control_file) or ".", exist_ok=True)
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(command, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, control_file)
        except Exception as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            self.logger.exception("写入下载控制请求失败")
            return self._json_response(500, {
                "success": False,
                "error": str(exc),
            })

        phase = "正在暂停…" if action == "pause" else "正在恢复…"
        NativeTaskRegistry.update(
            task_id,
            phase=phase,
            requested_action=action,
        )
        return self._json_response(202, {
            "success": True,
            "status": "pending",
            "task_id": task_id,
            "action": action,
            "phase": phase,
        })

    def _fever_bridge_setting(self, args, body, method):
        payload = body or args or {}
        game_id = str(payload.get("game_id") or "")
        game = self.game_helper.get_existing_game(game_id) if game_id else None
        try:
            distribution_id = int(payload.get(
                "distribution_id",
                game.default_distribution if game else -1,
            ))
        except (TypeError, ValueError):
            distribution_id = -1

        cloud_res = CloudRes()
        fever_managed = bool(game_id) and cloud_res.is_fever_managed_game(
            getShortGameId(game_id), distribution_id
        )
        manual_feature = bool(game_id) and cloud_res.has_manual_game_feature(
            getShortGameId(game_id)
        )
        configurable = fever_managed and manual_feature

        if method != "GET":
            if "forced" in payload:
                valid_distributions = game.get_distributions() if game else []
                if (
                    not configurable
                    or game is None
                    or distribution_id not in valid_distributions
                ):
                    return self._json_response(400, {
                        "success": False,
                        "error": "只有带独立云配置的发烧平台游戏可以切换启动方式",
                    })
                if not self.game_helper.set_fever_bridge_forced(
                    game_id, distribution_id, bool(payload.get("forced"))
                ):
                    return self._json_response(400, {
                        "success": False, "error": "保存分发强制设置失败"
                    })

        forced = bool(
            game and game.is_fever_bridge_forced(distribution_id)
        )
        eligible_by_default = fever_managed and not manual_feature
        effective = fever_managed and (
            forced or eligible_by_default
        )
        current_target_disabled = (
            method != "GET"
            and bool(game_id)
            and not effective
            and getShortGameId(game_id) in app_state.fever_bridge_target_game_ids
        )
        if current_target_disabled and app_state.fever_bridge is not None:
            app_state.fever_bridge.stop()
            app_state.fever_bridge = None
        return self._json_response(200, {
            "success": True,
            "forced": forced,
            "effective": effective,
            "eligible_by_default": eligible_by_default,
            "configurable": configurable,
            "manual_feature": manual_feature,
            "fever_managed": fever_managed,
            "distribution_id": distribution_id,
            "active": bool(
                getattr(getattr(app_state.fever_bridge, "ipc", None), "hwnd", None)
            ),
            "name": "平台托管登录（预览）",
        })

    # -- Utility routes ──

    def _serve_index(self, args, body, method):
        try:
            version = genv.get("VERSION", "")
            if not version:
                local_path = os.path.normpath(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "index.html")
                )
                if os.path.exists(local_path):
                    with open(local_path, "r", encoding="utf-8") as f:
                        return self._html_response(200, f.read())
            cloud_page = CloudRes().get_login_page()
            if cloud_page:
                return self._html_response(200, cloud_page)
            return self._html_response(200, const.html)
        except Exception:
            return self._html_response(200, const.html)

    def _export_logs(self, args, body, method):
        """Export diagnostic logs: save to file and open containing folder."""
        try:
            from debugmgr import DebugMgr
            data = DebugMgr.export_debug_info_json() if DebugMgr.is_windows() else {}
            log_dir = genv.get("FP_WORKDIR")
            log_path = os.path.join(log_dir, "log.txt")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n\n" + json.dumps(data, ensure_ascii=False, indent=2))
            if os.path.exists(log_path):
                import subprocess
                subprocess.Popen(["explorer", "/select,", log_path])
                return self._json_response(200, {"success": True, "path": log_path})
            return self._json_response(404, {"success": False, "error": "日志文件不存在"})
        except Exception as e:
            return self._json_response(500, {"success": False, "error": str(e)})

    def _open_external_url(self, args, body, method):
        """使用系统默认浏览器打开外部 URL。"""
        url = args.get("url", "")
        if url and url.startswith(("http://", "https://")):
            import webbrowser
            webbrowser.open(url)
            return self._json_response(200, {"success": True})
        return self._json_response(400, {"success": False, "error": "invalid url"})

    def _get_proxy_mode(self, args, body, method):
        """获取当前代理模式 (global/process)。"""
        mode = genv.get("proxy_mode", "global")
        return self._json_response(200, {"success": True, "mode": mode})

    def _set_proxy_mode(self, args, body, method):
        """设置代理模式 (global/process/compat)。"""
        mode = body.get("mode", "") if body else ""
        if mode not in ("global", "process", "compat"):
            return self._json_response(400, {"success": False, "error": "无效的模式，应为 global、process 或 compat"})
        genv.set("proxy_mode", mode, True)
        self.logger.info(f"代理模式已切换为: {mode}")
        return self._json_response(200, {"success": True, "mode": mode})

    def _create_game_shortcut(self, args, body, method):
        """为指定游戏创建桌面快捷方式（通过工具启动）。"""
        game_id = body.get("game_id", "") if body else args.get("game_id", "")
        installation_id = (
            body.get("installation_id", "") if body else args.get("installation_id", "")
        )
        if not game_id:
            return self._json_response(400, {"success": False, "error": "缺少 game_id 参数"})
        
        game = self.game_helper.get_existing_game(game_id)
        if not game:
            return self._json_response(404, {"success": False, "error": f"未找到游戏: {game_id}"})
        
        # 尝试使用游戏路径作为图标来源
        installation = self._resolve_installation(game, installation_id)
        icon_source = (
            installation.path
            if installation and installation.path and os.path.exists(installation.path)
            else ""
        )
        
        success = game.create_tool_launch_shortcut(
            icon_source, installation.installation_id if installation else ""
        )
        if success:
            return self._json_response(200, {"success": True, "message": "快捷方式创建成功"})
        else:
            return self._json_response(500, {"success": False, "error": "快捷方式创建失败"})
