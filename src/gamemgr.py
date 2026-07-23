# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

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
import ctypes
import shutil
import subprocess
import os
import sys
import time
import json
import base64
import shlex
import threading
import uuid
from typing import Optional, List, Dict, Tuple
import xxhash
from envmgr import genv
import app_state
from logutil import setup_logger
from cloudRes import CloudRes
from channelHandler.channelUtils import getShortGameId, cmp_game_id

_FEVER_GAMES_CACHE_KEY = "_fever_games_registry_cache_v1"
_FEVER_GAMES_CACHE_LOCK = threading.RLock()


def calculate_xxh64(file_path):
    h = xxhash.xxh64() # 初始化 64位 对象
    with open(file_path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class GameInstallation:
    """A concrete local installation of a game.

    A game may have multiple distributions and multiple local paths.  Runtime
    state that belongs to one concrete installation must live here instead of
    on the title-level ``Game`` record.
    """

    MARKER_FILENAME = ".idv-login-installation.json"

    def __init__(
        self,
        installation_id: str,
        path: str = "",
        distribution_id: int = -1,
        installed_version: str = "",
        source: str = "manual",
        content_id=None,
        startup_path: str = "",
        startup_args: str = "",
        settings=None,
        created_at: int = 0,
        updated_at: int = 0,
    ) -> None:
        self.installation_id = str(installation_id or uuid.uuid4())
        self.path = self._normalize_path(path)
        try:
            self.distribution_id = int(distribution_id)
        except (TypeError, ValueError):
            self.distribution_id = -1
        self.installed_version = str(installed_version or "")
        self.source = str(source or "manual")
        self.content_id = content_id
        self.startup_path = str(startup_path or "")
        self.startup_args = str(startup_args or "")
        self.settings = dict(settings) if isinstance(settings, dict) else {}
        now = int(time.time())
        self.created_at = int(created_at or now)
        self.updated_at = int(updated_at or self.created_at)

    @staticmethod
    def _normalize_path(path: str) -> str:
        value = str(path or "")
        if sys.platform == "win32":
            return value.replace("\\", "/")
        return value

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            installation_id=data.get("installation_id", ""),
            path=data.get("path", ""),
            distribution_id=data.get("distribution_id", -1),
            installed_version=data.get("installed_version", data.get("version", "")),
            source=data.get("source", "manual"),
            content_id=data.get("content_id"),
            startup_path=data.get("startup_path", ""),
            startup_args=data.get("startup_args", ""),
            settings=data.get("settings"),
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
        )

    def to_dict(self) -> dict:
        return {
            "installation_id": self.installation_id,
            "path": self.path,
            "distribution_id": self.distribution_id,
            "installed_version": self.installed_version,
            "source": self.source,
            "content_id": self.content_id,
            "startup_path": self.startup_path,
            "startup_args": self.startup_args,
            "settings": dict(self.settings),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def get_non_sensitive_data(self) -> dict:
        data = self.to_dict()
        data["installed"] = bool(self.path and os.path.exists(self.path))
        return data

    @classmethod
    def read_marker(cls, executable_path: str) -> Optional[dict]:
        root = os.path.dirname(str(executable_path or ""))
        marker_path = os.path.join(root, cls.MARKER_FILENAME)
        if not root or not os.path.isfile(marker_path):
            return None
        try:
            with open(marker_path, "r", encoding="utf-8") as marker_file:
                data = json.load(marker_file)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def write_marker(self, game_id: str) -> bool:
        root = os.path.dirname(self.path or "")
        if not root or not os.path.isdir(root):
            return False
        marker_path = os.path.join(root, self.MARKER_FILENAME)
        temp_path = marker_path + ".tmp"
        payload = {
            "schema_version": 1,
            "game_id": game_id,
            **self.to_dict(),
        }
        try:
            with open(temp_path, "w", encoding="utf-8") as marker_file:
                json.dump(payload, marker_file, ensure_ascii=False, indent=2)
                marker_file.write("\n")
            os.replace(temp_path, marker_path)
            return True
        except Exception:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            return False

class Game:
    def __init__(
        self,
        game_id: str,
        name: str = "",
        path: str = "",
        should_auto_start: bool = False,
        auto_close_after_login: bool = False,
        login_delay: int = 6,
        last_used_time: int = 0,
        version: str = "",
        default_distribution: int = -1,
        installation_state=None,
        installations=None,
        default_installation_id: str = "",
        force_fever_bridge_distributions=None,
    ) -> None:
        self.game_id = game_id
        self.name = name if name else game_id
        self.should_auto_start = should_auto_start
        self.auto_close_after_login = auto_close_after_login
        self.login_delay = login_delay
        self.last_used_time = last_used_time or int(time.time())
        self.logger = setup_logger()
        self.legacy_projection_merged = False
        self.installations: Dict[str, GameInstallation] = {}
        if isinstance(installation_state, dict):
            installations = installation_state.get("installations", {})
            default_installation_id = installation_state.get(
                "default_installation_id", ""
            )
            force_fever_bridge_distributions = installation_state.get(
                "force_fever_bridge_distributions", []
            )
        self.force_fever_bridge_distributions = {
            self._coerce_distribution_id(item)
            for item in (force_fever_bridge_distributions or [])
            if self._coerce_distribution_id(item) != -1
        }
        if isinstance(installations, dict):
            raw_installations = installations.values()
        elif isinstance(installations, list):
            raw_installations = installations
        else:
            raw_installations = []
        for raw in raw_installations:
            if not isinstance(raw, dict):
                continue
            installation = GameInstallation.from_dict(raw)
            self.installations[installation.installation_id] = installation

        # Migrate the legacy single-path record once.  The deterministic UUID
        # prevents duplicate installations if an old cache is loaded again.
        if not self.installations and path:
            legacy_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"idv-login:{game_id}:{default_distribution}:{GameInstallation._normalize_path(path)}",
            ))
            legacy = GameInstallation(
                installation_id=legacy_id,
                path=path,
                distribution_id=default_distribution,
                installed_version=version,
                source="legacy",
            )
            self.installations[legacy.installation_id] = legacy

        self.default_installation_id = str(default_installation_id or "")
        if self.default_installation_id not in self.installations:
            self.default_installation_id = ""
        if not self.default_installation_id and self.installations:
            matching = next(
                (
                    item for item in self.installations.values()
                    if item.distribution_id == self._coerce_distribution_id(default_distribution)
                ),
                None,
            )
            self.default_installation_id = (
                matching.installation_id if matching else next(iter(self.installations))
            )
        self._enforce_distribution_uniqueness()
        if isinstance(installation_state, dict):
            self.legacy_projection_merged = self._merge_legacy_projection(
                path,
                version,
                default_distribution,
                installation_state.get("legacy_projection"),
            )
        self.last_update_async = False
        self.last_start_error = ""
        self.active_installation_id = ""

    @staticmethod
    def _coerce_distribution_id(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    def get_installation(self, installation_id: str = "") -> Optional[GameInstallation]:
        target_id = str(installation_id or self.default_installation_id or "")
        return self.installations.get(target_id)

    def _merge_legacy_projection(
        self,
        path: str,
        version: str,
        distribution_id: int,
        previous_projection=None,
    ) -> bool:
        """Merge changes made by a downgraded client into the v1 state.

        The legacy fields are a writable projection of the default installation.
        If an old release changes them, the next new release adopts those changes
        instead of silently restoring stale v1 data.
        """
        legacy_path = GameInstallation._normalize_path(path)
        legacy_dist = self._coerce_distribution_id(distribution_id)
        previous = previous_projection if isinstance(previous_projection, dict) else None
        previous_path = GameInstallation._normalize_path(
            previous.get("path", "") if previous else ""
        )
        path_changed = previous is None or legacy_path != previous_path
        version_changed = previous is None or str(version or "") != str(
            previous.get("version", "") or ""
        )
        distribution_changed = previous is None or legacy_dist != self._coerce_distribution_id(
            previous.get("default_distribution", -1)
        )
        if not (path_changed or version_changed or distribution_changed):
            return False
        default = self.get_installation()
        changed = False
        if path_changed and legacy_path:
            matching = next(
                (
                    item for item in self.installations.values()
                    if os.path.normcase(os.path.normpath(item.path or ""))
                    == os.path.normcase(os.path.normpath(legacy_path))
                ),
                None,
            )
            if matching is not None:
                changed = self.default_installation_id != matching.installation_id
                self.default_installation_id = matching.installation_id
                default = matching
            else:
                invalid_ids = [
                    item.installation_id
                    for item in self.installations.values()
                    if not item.path or not os.path.exists(item.path)
                ]
                for installation_id in invalid_ids:
                    self.remove_installation(installation_id)
                default = self.add_installation(
                    legacy_path, source="manual", set_default=True
                )
                changed = True
        elif path_changed and not legacy_path and default is not None:
            self.remove_installation(default.installation_id)
            default = self.get_installation()
            changed = True
        if default is None:
            return changed
        if version_changed and default.installed_version != str(version or ""):
            default.installed_version = str(version or "")
            changed = True
        if distribution_changed and default.distribution_id != legacy_dist:
            self._claim_distribution(default, legacy_dist)
            changed = True
        return changed

    def get_installation_for_distribution(self, distribution_id: int) -> Optional[GameInstallation]:
        dist_id = self._coerce_distribution_id(distribution_id)
        default = self.get_installation()
        if default and default.distribution_id == dist_id:
            return default
        return next(
            (item for item in self.installations.values() if item.distribution_id == dist_id),
            None,
        )

    def resolve_installation(
        self, installation_id: str = "", distribution_id: int = -1
    ) -> Optional[GameInstallation]:
        if installation_id:
            return self.installations.get(str(installation_id))
        dist_id = self._coerce_distribution_id(distribution_id)
        if dist_id != -1:
            return self.get_installation_for_distribution(dist_id)
        if self.active_installation_id in self.installations:
            return self.installations[self.active_installation_id]
        return self.get_installation()

    def get_installation_setting(
        self,
        key: str,
        legacy_value,
        installation_id: str = "",
        distribution_id: int = -1,
    ):
        installation = self.resolve_installation(installation_id, distribution_id)
        if installation and key in installation.settings:
            return installation.settings[key]
        return legacy_value

    def set_installation_setting(
        self,
        key: str,
        value,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> Optional[GameInstallation]:
        installation = self.resolve_installation(installation_id, distribution_id)
        if installation is None:
            return None
        installation.settings[key] = value
        installation.updated_at = int(time.time())
        return installation

    def get_auto_start_installation(self) -> Optional[GameInstallation]:
        configured = next(
            (
                item for item in self.installations.values()
                if item.settings.get("auto_start") is True
            ),
            None,
        )
        if configured is not None:
            return configured
        return self.get_installation() if self.should_auto_start else None

    def _enforce_distribution_uniqueness(self) -> None:
        """Collapse historical duplicates for concrete cloud distributions."""
        by_distribution = {}
        for item in list(self.installations.values()):
            if item.distribution_id == -1:
                continue
            by_distribution.setdefault(item.distribution_id, []).append(item)
        for records in by_distribution.values():
            if len(records) < 2:
                continue
            keeper = next(
                (
                    item for item in records
                    if item.installation_id == self.default_installation_id
                ),
                None,
            ) or max(
                records,
                key=lambda item: (item.updated_at, item.created_at),
            )
            for item in records:
                if item is keeper:
                    continue
                self.installations.pop(item.installation_id, None)
                if self.default_installation_id == item.installation_id:
                    self.default_installation_id = keeper.installation_id

    def _claim_distribution(
        self, installation: GameInstallation, distribution_id: int
    ) -> None:
        """Assign a concrete distribution exclusively to *installation*."""
        dist_id = self._coerce_distribution_id(distribution_id)
        if dist_id != -1:
            for item in list(self.installations.values()):
                if item is installation or item.distribution_id != dist_id:
                    continue
                self.installations.pop(item.installation_id, None)
                if self.default_installation_id == item.installation_id:
                    self.default_installation_id = installation.installation_id
        installation.distribution_id = dist_id

    def add_installation(
        self,
        path: str,
        distribution_id: int = -1,
        installed_version: str = "",
        source: str = "manual",
        content_id=None,
        startup_path: str = "",
        startup_args: str = "",
        installation_id: str = "",
        set_default: bool = True,
    ) -> GameInstallation:
        normalized_path = GameInstallation._normalize_path(path)
        dist_id = self._coerce_distribution_id(distribution_id)
        existing = None
        if installation_id:
            existing = self.installations.get(str(installation_id))
        if existing is None and normalized_path:
            existing = next(
                (
                    item for item in self.installations.values()
                    if os.path.normcase(os.path.normpath(item.path or ""))
                    == os.path.normcase(os.path.normpath(normalized_path))
                ),
                None,
            )
        if existing is None and dist_id != -1:
            existing = self.get_installation_for_distribution(dist_id)
        now = int(time.time())
        if existing is None:
            existing = GameInstallation(
                installation_id=installation_id or str(uuid.uuid4()),
                path=normalized_path,
                distribution_id=dist_id,
                installed_version=installed_version,
                source=source,
                content_id=content_id,
                startup_path=startup_path,
                startup_args=startup_args,
            )
            self.installations[existing.installation_id] = existing
        else:
            existing.path = normalized_path or existing.path
            if dist_id != -1:
                self._claim_distribution(existing, dist_id)
            if installed_version:
                existing.installed_version = str(installed_version)
            if source:
                existing.source = str(source)
            if content_id is not None:
                existing.content_id = content_id
            if startup_path:
                existing.startup_path = str(startup_path)
            if startup_args:
                existing.startup_args = str(startup_args)
            existing.updated_at = now
        if set_default or not self.default_installation_id:
            self.default_installation_id = existing.installation_id
        self.last_used_time = now
        return existing

    def remove_installation(self, installation_id: str) -> bool:
        target_id = str(installation_id or "")
        if target_id not in self.installations:
            return False
        del self.installations[target_id]
        if self.default_installation_id == target_id:
            self.default_installation_id = next(iter(self.installations), "")
        return True

    def set_default_installation(self, installation_id: str) -> bool:
        target_id = str(installation_id or "")
        if target_id not in self.installations:
            return False
        self.default_installation_id = target_id
        self.last_used_time = int(time.time())
        return True

    @property
    def path(self) -> str:
        installation = self.get_installation()
        return installation.path if installation else ""

    @path.setter
    def path(self, value: str) -> None:
        installation = self.get_installation()
        if installation:
            installation.path = GameInstallation._normalize_path(value)
            installation.updated_at = int(time.time())
        elif value:
            self.add_installation(value, source="manual")

    @property
    def version(self) -> str:
        installation = self.get_installation()
        return installation.installed_version if installation else ""

    @version.setter
    def version(self, value: str) -> None:
        installation = self.get_installation()
        if installation:
            installation.installed_version = str(value or "")
            installation.updated_at = int(time.time())

    @property
    def default_distribution(self) -> int:
        installation = self.get_installation()
        return installation.distribution_id if installation else -1

    @default_distribution.setter
    def default_distribution(self, value: int) -> None:
        installation = self.get_installation()
        if installation:
            self._claim_distribution(
                installation, self._coerce_distribution_id(value)
            )
            installation.updated_at = int(time.time())

    @classmethod
    def from_dict(cls, data: dict, installation_state=None):
        if installation_state is None:
            installation_state = data.get("installation_state_v1")
        return cls(
            game_id=data.get("game_id", ""),
            name=data.get("name", ""),
            path=data.get("path", ""),
            should_auto_start=data.get("should_auto_start", False),
            auto_close_after_login=data.get("auto_close_after_login", True),
            last_used_time=data.get("last_used_time", int(time.time())),
            login_delay=data.get("login_delay", 6),
            version=data.get("version", ""),
            default_distribution=data.get("default_distribution", -1),
            installation_state=installation_state,
            # Compatibility with development builds that briefly wrote these
            # fields at the game-record root.
            installations=data.get("installations"),
            default_installation_id=data.get("default_installation_id", ""),
        )

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "name": self.name,
            "path": self.path,
            "should_auto_start": self.should_auto_start,
            "auto_close_after_login": self.auto_close_after_login,
            "last_used_time": self.last_used_time,
            "login_delay": self.login_delay,
            "version": self.version,
            "default_distribution": self.default_distribution,
        }

    def to_installation_state(self) -> dict:
        return {
            "schema_version": 1,
            "default_installation_id": self.default_installation_id,
            "legacy_projection": {
                "path": self.path,
                "version": self.version,
                "default_distribution": self.default_distribution,
            },
            "installations": {
                installation_id: installation.to_dict()
                for installation_id, installation in self.installations.items()
            },
            "force_fever_bridge_distributions": sorted(
                self.force_fever_bridge_distributions
            ),
        }

    def get_non_sensitive_data(self) -> dict:
        return {
            "game_id": self.game_id,
            "name": self.name,
            "last_used_time": self.last_used_time,
            "should_auto_start": self.should_auto_start,
            "path": self.path,
            "default_installation_id": self.default_installation_id,
            "installations": [
                installation.get_non_sensitive_data()
                for installation in self.installations.values()
            ],
            "force_fever_bridge_distributions": sorted(
                self.force_fever_bridge_distributions
            ),
        }

    def is_fever_bridge_forced(self, distribution_id: int) -> bool:
        return self._coerce_distribution_id(
            distribution_id
        ) in self.force_fever_bridge_distributions

    def set_fever_bridge_forced(self, distribution_id: int, forced: bool) -> bool:
        dist_id = self._coerce_distribution_id(distribution_id)
        if dist_id == -1:
            return False
        if forced:
            self.force_fever_bridge_distributions.add(dist_id)
        else:
            self.force_fever_bridge_distributions.discard(dist_id)
        return True

    def should_use_fever_bridge(self, installation: GameInstallation) -> bool:
        forced = self.is_fever_bridge_forced(installation.distribution_id)
        if not genv.get("FEVER_BRIDGE_ENABLED", False) and not forced:
            return False
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        return bool(
            cloud_res.is_fever_managed_game(
                short_game_id, installation.distribution_id
            )
            and (
                forced
                or not cloud_res.has_manual_game_feature(short_game_id)
            )
        )

    def start(self, installation_id: str = ""):
        self.last_start_error = ""
        installation = self.get_installation(installation_id)
        game_path = installation.path if installation else ""
        if not game_path or not os.path.exists(game_path):
            self.logger.error(f"游戏路径无效或不存在: {game_path}")
            self.last_start_error = "游戏路径无效或不存在"
            return False
        self.active_installation_id = installation.installation_id
        use_fever_bridge = self.should_use_fever_bridge(installation)
        if use_fever_bridge:
            from fever_bridge import FeverBridge
            if app_state.fever_bridge is None:
                app_state.fever_bridge = FeverBridge(self.logger)
            try:
                if not app_state.fever_bridge.activate(self.game_id):
                    self.logger.error("平台托管登录仅支持 Windows")
                    self.last_start_error = "平台托管登录仅支持 Windows"
                    return False
            except Exception:
                self.logger.exception("启动平台托管登录失败")
                self.last_start_error = "平台托管登录启动失败；请确认真实发烧平台未在运行且 mpay 资源完整"
                return False
        start_args = ""
        if use_fever_bridge and installation.distribution_id != -1:
            # A Fever-hosted launch must use the distribution's launcher
            # parameters.  The installation record may contain the manually
            # maintained standalone parameters (including --is_multi_start)
            # from an older hotfix; those parameters deliberately bypass the
            # Fever IPC path and must not leak into a hosted launch.
            launcher_data = self.get_launcher_data_for_distribution(
                installation.distribution_id
            ) or {}
            start_args = str(launcher_data.get("startup_params") or "")
        elif not use_fever_bridge:
            # A normal launch must prefer the manually maintained cloud
            # parameters so an imported Fever record cannot make the game
            # believe the real Fever client is present.
            start_args = str(
                CloudRes().get_start_argument(getShortGameId(self.game_id)) or ""
            )
            if not start_args:
                start_args = installation.startup_args if installation else ""
        if sys.platform == "win32":
            # 规范化路径
            game_path = os.path.normpath(game_path)
            game_dir = os.path.dirname(game_path)
            
            # 设置进程的工作目录为游戏所在目录
            startupinfo = subprocess.STARTUPINFO()
            # 隐藏命令行窗口
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
            
            # 设置环境变量，模拟资源管理器启动
            env = os.environ.copy()
            env['COMSPEC'] = os.environ.get('COMSPEC', '%SystemRoot%\\system32\\cmd.exe')
            env['SYSTEMROOT'] = os.environ.get('SYSTEMROOT', '%SystemRoot%')
            
            # 注入代理环境变量（mitmproxy 代理模式）
            proxy_mgr = app_state.proxy_mgr
            if proxy_mgr:
                proxy_env = proxy_mgr.get_proxy_env()
                env.update(proxy_env)
            
            try:
                # 使用 cmd.exe /c start 彻底脱离父子进程关系，同时保留环境变量的独立引入
                cmd_line = f'cmd.exe /s /c "start "" /d "{game_dir}" "{game_path}"'
                if start_args:
                    cmd_line += f" {start_args}"
                cmd_line += '"' 
                
                # 设置创建标志：DETACHED_PROCESS (0x00000008) 和 CREATE_NEW_PROCESS_GROUP (0x00000200)
                creationflags = 0x00000008 | 0x00000200
                
                subprocess.Popen(
                    cmd_line,
                    cwd=game_dir,
                    env=env,
                    shell=False,
                    startupinfo=startupinfo,
                    creationflags=creationflags
                )
                self.logger.info(f"成功使用 cmd start 启动游戏: {game_path}")
            except Exception as e:
                self.logger.warning(f"cmd start启动失败，尝试使用ShellExecuteEx作为备选方案1: {str(e)}")
                try:
                    # 备用方案1: ShellExecuteExW
                    proxy_env = proxy_mgr.get_proxy_env() if proxy_mgr else {}
                    original_env = {}
                    try:
                        for k, v in proxy_env.items():
                            original_env[k] = os.environ.get(k)
                            os.environ[k] = v

                        import ctypes
                        SEE_MASK_NOCLOSEPROCESS = 0x00000040
                        SEE_MASK_NOASYNC = 0x00000100
                        
                        class SHELLEXECUTEINFO(ctypes.Structure):
                            _fields_ = [
                                ("cbSize", ctypes.c_uint32),
                                ("fMask", ctypes.c_ulong),
                                ("hwnd", ctypes.c_void_p),
                                ("lpVerb", ctypes.c_wchar_p),
                                ("lpFile", ctypes.c_wchar_p),
                                ("lpParameters", ctypes.c_wchar_p),
                                ("lpDirectory", ctypes.c_wchar_p),
                                ("nShow", ctypes.c_int),
                                ("hInstApp", ctypes.c_void_p),
                                ("lpIDList", ctypes.c_void_p),
                                ("lpClass", ctypes.c_wchar_p),
                                ("hkeyClass", ctypes.c_void_p),
                                ("dwHotKey", ctypes.c_uint32),
                                ("hIcon", ctypes.c_void_p),
                                ("hProcess", ctypes.c_void_p)
                            ]

                        shell_info = SHELLEXECUTEINFO()
                        shell_info.cbSize = ctypes.sizeof(shell_info)
                        shell_info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
                        shell_info.lpVerb = "open"
                        shell_info.lpFile = game_path
                        shell_info.lpDirectory = game_dir
                        shell_info.lpParameters = start_args if start_args else None
                        shell_info.nShow = 1  # SW_SHOWNORMAL
                        
                        shell32 = ctypes.WinDLL('shell32.dll')
                        result = shell32.ShellExecuteExW(ctypes.byref(shell_info))
                        
                        if not result:
                            raise Exception("ShellExecuteExW返回失败")
                        self.logger.info(f"成功使用ShellExecuteEx启动游戏: {game_path}")
                    finally:
                        for k, v in original_env.items():
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v
                except Exception as e2:
                    self.logger.exception(f"ShellExecuteEx启动失败，使用普通 Popen 备选启动2: {str(e2)}")
                    cmd = [game_path] + (shlex.split(start_args) if start_args else [])
                    subprocess.Popen(
                        cmd,
                        cwd=game_dir,
                        env=env,
                        shell=False,
                        startupinfo=startupinfo,
                        creationflags=0x00000008 | 0x00000200
                    )
        else:
            env = os.environ.copy()
            proxy_mgr = app_state.proxy_mgr
            if proxy_mgr:
                proxy_env = proxy_mgr.get_proxy_env()
                env.update(proxy_env)
            cmd = [game_path] + (shlex.split(start_args) if start_args else [])
            subprocess.Popen(cmd, env=env, shell=False)
        return True

    def _get_shortcut_dir(self) -> Optional[str]:
        if sys.platform != "win32":
            return None
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.exists(base_dir):
            return base_dir
        return os.path.dirname(os.path.abspath(self.path)) if self.path else None

    def _build_unique_shortcut_path(self, shortcut_dir: str, base_name: str) -> str:
        safe_name = str(base_name or "").strip() or self.game_id
        candidate = os.path.join(shortcut_dir, f"{safe_name}.lnk")
        if not os.path.exists(candidate):
            return candidate
        suffix = 2
        while True:
            candidate = os.path.join(shortcut_dir, f"{safe_name}-{suffix}.lnk")
            if not os.path.exists(candidate):
                return candidate
            suffix += 1

    def _verify_created_shortcut(self, shortcut_path: str, expected_target: str, expected_args: str, expected_working_dir: str) -> bool:
        if not os.path.exists(shortcut_path):
            self.logger.error(f"快捷方式创建后不存在: {shortcut_path}")
            return False
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")

            game_path = shortcut_path
            if game_path.lower().endswith(".lnk"):
                shortcut = shell.CreateShortcut(game_path)
                game_path = (shortcut.Targetpath or "").strip()
                shortcut_args = (shortcut.Arguments or "").strip()
                shortcut_working_dir = (shortcut.WorkingDirectory or "").strip()
            else:
                shortcut_args = ""
                shortcut_working_dir = ""

            expected_target_norm = os.path.normcase(os.path.normpath(expected_target or ""))
            actual_target_norm = os.path.normcase(os.path.normpath(game_path or ""))
            expected_working_norm = os.path.normcase(os.path.normpath(expected_working_dir or ""))
            actual_working_norm = os.path.normcase(os.path.normpath(shortcut_working_dir or ""))

            if actual_target_norm != expected_target_norm:
                self.logger.error(f"快捷方式目标不匹配: expected={expected_target}, actual={game_path}")
                return False
            expected_args_normalized = (expected_args or "").strip()
            if (shortcut_args or "") != expected_args_normalized:
                self.logger.error(f"快捷方式参数不匹配: expected={expected_args_normalized}, actual={shortcut_args}")
                return False
            if actual_working_norm != expected_working_norm:
                self.logger.error(f"快捷方式工作目录不匹配: expected={expected_working_dir}, actual={shortcut_working_dir}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"验证快捷方式失败: {e}")
            return False

    def _find_existing_tool_launch_shortcut(self, shortcut_dir: str, expected_target: str, expected_args: str) -> str:
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            expected_target_norm = os.path.normcase(os.path.normpath(expected_target or ""))
            expected_args_normalized = (expected_args or "").strip()
            for name in os.listdir(shortcut_dir):
                if not name.lower().endswith(".lnk"):
                    continue
                shortcut_path = os.path.join(shortcut_dir, name)
                try:
                    shortcut = shell.CreateShortcut(shortcut_path)
                    actual_target = os.path.normcase(os.path.normpath((shortcut.Targetpath or "").strip()))
                    actual_args = (shortcut.Arguments or "").strip()
                    if actual_target == expected_target_norm and actual_args == expected_args_normalized:
                        return shortcut_path
                except Exception:
                    continue
        except Exception as e:
            self.logger.debug(f"扫描已有工具快捷方式失败: {e}")
        return ""

    def create_tool_launch_shortcut(
        self, icon_source_path: str = "", installation_id: str = ""
    ) -> bool:
        """创建通过工具启动游戏的桌面快捷方式。
        
        快捷方式目标为工具启动脚本，参数为 --uri "idvlogin://start?game_id=xxx"，
        图标来自 icon_source_path（如游戏 exe）或工具的 icon.ico。
        """
        if sys.platform != "win32":
            return False
        
        # 查找 点我启动工具.bat：从当前脚本目录开始，向上最多找 3 层
        # 打包后布局: {app}/python-embed/python.exe, {app}/src/*, {app}/点我启动工具.bat
        # 开发环境布局: {dev}/src/*, {dev}/tools/点我启动工具.bat
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bat_path = ""
        search_dir = script_dir
        for _ in range(4):
            candidate = os.path.join(search_dir, "点我启动工具.bat")
            if os.path.exists(candidate):
                bat_path = candidate
                break
            # 也检查 tools/ 子目录（开发环境）
            candidate_tools = os.path.join(search_dir, "tools", "点我启动工具.bat")
            if os.path.exists(candidate_tools):
                bat_path = candidate_tools
                break
            search_dir = os.path.dirname(search_dir)
        
        if not bat_path:
            self.logger.error("未找到 点我启动工具.bat")
            return False
        
        # 桌面路径
        try:
            import ctypes.wintypes
            CSIDL_DESKTOP = 0
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_DESKTOP, 0, 0, buf)
            desktop_path = buf.value
        except Exception:
            desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        
        if not os.path.exists(desktop_path):
            self.logger.error(f"桌面路径不存在: {desktop_path}")
            return False
        
        try:
            import win32com.client
            
            # 获取游戏名称
            name_from_launcher = ""
            if genv.get("launcher_data_cache", {}) and isinstance(genv.get("launcher_data_cache", {}), dict):
                launcher_data = genv.get("launcher_data_cache", {}).get(str(self.default_distribution), {})
                if isinstance(launcher_data, dict):
                    display_name = launcher_data.get("display_name", "")
                    if display_name:
                        name_from_launcher = display_name
            name = name_from_launcher if name_from_launcher else (self.name if self.name else self.game_id)
            name = f"{name}(IDV-LOGIN)"
            
            shell = win32com.client.Dispatch("WScript.Shell")
            installation = self.get_installation(installation_id)
            if installation:
                uri_arg = (
                    f'idvlogin://start?game_id={self.game_id}'
                    f'&installation_id={installation.installation_id}'
                )
            else:
                uri_arg = f'idvlogin://start?game_id={self.game_id}'
            expected_args = f'--uri "{uri_arg}"'
            existing_shortcut = self._find_existing_tool_launch_shortcut(desktop_path, bat_path, expected_args)
            if existing_shortcut:
                self.logger.info(f"已存在工具启动快捷方式，跳过创建: {existing_shortcut}")
                return True

            shortcut_path = self._build_unique_shortcut_path(desktop_path, name)
            shortcut = shell.CreateShortCut(shortcut_path)
            
            # 目标和参数 - 使用 bat 文件
            shortcut.Targetpath = bat_path
            shortcut.Arguments = expected_args
            shortcut.WorkingDirectory = os.path.dirname(bat_path)
            shortcut.Description = f"通过登录助手启动 {name}"
            
            # 图标：优先使用游戏 exe，否则使用 icon.ico
            icon_ico = os.path.join(os.path.dirname(bat_path), "icon.ico")
            if icon_source_path and os.path.exists(icon_source_path):
                icon_path = icon_source_path
            elif os.path.exists(icon_ico):
                icon_path = icon_ico
            else:
                icon_path = bat_path
            shortcut.IconLocation = f"{icon_path},0"
            
            shortcut.save()
            self.logger.info(f"创建工具启动快捷方式成功: {shortcut_path}")
            return True
        except Exception as e:
            self.logger.error(f"创建工具启动快捷方式失败: {e}")
            return False

    def get_root_path(self, installation_id: str = "") -> str:
        """获取游戏根目录"""
        installation = self.get_installation(installation_id)
        if not installation or not installation.path:
            return ""
        startup_path = str(installation.startup_path or "").replace("\\", "/")
        relative_parts = [
            part for part in startup_path.split("/")
            if part not in ("", ".")
        ]
        if relative_parts and ".." not in relative_parts:
            root = installation.path
            for _part in relative_parts:
                root = os.path.dirname(root)
            return root
        return os.path.dirname(installation.path)
    
    def _normalize_distribution_ids(self, distributions: List) -> List[int]:
        result = []
        for dist in distributions:
            dist_id = None
            if isinstance(dist, dict):
                dist_id = dist.get("distribution_id")
                if dist_id is None:
                    dist_id = dist.get("app_id")
            else:
                dist_id = dist
            if dist_id is None:
                continue
            try:
                result.append(int(dist_id))
            except (TypeError, ValueError):
                continue
        return result

    def identify_installation_distribution(
        self,
        installation_id: str,
        distribution_options: List,
        file_info_by_distribution: Dict[int, dict],
        launcher_data_by_distribution: Optional[Dict[int, dict]] = None,
        force: bool = False,
    ) -> int:
        """Identify and apply the closest cloud distribution for an install.

        The closest manifest wins by mismatched-file count.  Ties use the
        first cloud-configured distribution.  When manifests cannot be scored,
        the first distribution is still selected.  The owning ``GameManager``
        is responsible for persisting the mutated installation.
        """
        installation = self.get_installation(installation_id)
        if installation is None or (installation.distribution_id != -1 and not force):
            return installation.distribution_id if installation else -1
        if not installation.path or not os.path.exists(installation.path):
            return -1

        ordered_distribution_ids = list(dict.fromkeys(
            self._normalize_distribution_ids(distribution_options or [])
        ))
        if not ordered_distribution_ids:
            return installation.distribution_id

        candidates = []
        seen = set()
        for index, option in enumerate(distribution_options or []):
            dist_ids = self._normalize_distribution_ids([option])
            if not dist_ids or dist_ids[0] in seen:
                continue
            dist_id = dist_ids[0]
            seen.add(dist_id)
            file_info = file_info_by_distribution.get(dist_id)
            files = file_info.get("files", []) if isinstance(file_info, dict) else []
            if not isinstance(files, list) or not files:
                continue
            candidates.append({
                "distribution_id": dist_id,
                "files": files,
                "index": index,
            })
        launcher_data_by_distribution = launcher_data_by_distribution or {}
        selected_distribution_id = ordered_distribution_ids[0]
        mismatch_count = None
        hash_cache = {}
        scored = []
        try:
            for candidate in candidates:
                launcher_data = launcher_data_by_distribution.get(
                    candidate["distribution_id"], {}
                )
                startup_path = str(
                    launcher_data.get("startup_path")
                    or installation.startup_path
                    or os.path.basename(installation.path)
                )
                _, mismatched = self.version_check(
                    candidate["files"],
                    installation.installation_id,
                    hash_cache,
                    root_path=self._root_from_executable(
                        installation.path, startup_path
                    ),
                )
                scored.append((
                    len(mismatched),
                    candidate["index"],
                    candidate["distribution_id"],
                ))
        except OSError as exc:
            self.logger.warning(f"识别游戏分发时读取文件失败: {exc}")
            scored = []

        if scored:
            mismatch_count, _, selected_distribution_id = min(
                scored, key=lambda item: (item[0], item[1])
            )

        selected_file_info = file_info_by_distribution.get(
            selected_distribution_id, {}
        )
        selected_launcher_data = launcher_data_by_distribution.get(
            selected_distribution_id, {}
        )
        self._claim_distribution(installation, selected_distribution_id)
        if isinstance(selected_file_info, dict):
            if selected_file_info.get("version_code") is not None:
                installation.installed_version = str(
                    selected_file_info.get("version_code") or ""
                )
            if selected_file_info.get("app_content_id") is not None:
                installation.content_id = selected_file_info.get("app_content_id")
        if isinstance(selected_launcher_data, dict):
            startup_path = str(selected_launcher_data.get("startup_path") or "")
            if self._is_safe_startup_path_for_executable(
                installation.path, startup_path
            ):
                installation.startup_path = startup_path
            elif not installation.startup_path:
                installation.startup_path = os.path.basename(installation.path)
            installation.startup_args = str(
                selected_launcher_data.get("startup_params") or ""
            )
        installation.updated_at = int(time.time())
        score_text = (
            f"{mismatch_count} 个文件不匹配"
            if mismatch_count is not None
            else "无法比较清单，使用首个分发"
        )
        self.logger.info(
            f"已识别安装记录 {installation.installation_id} 的分发: "
            f"{selected_distribution_id}（{score_text}）"
        )
        return selected_distribution_id

    @staticmethod
    def _is_safe_startup_path_for_executable(
        executable_path: str, startup_path: str
    ) -> bool:
        parts = [
            part for part in str(startup_path or "").replace("\\", "/").split("/")
            if part not in ("", ".")
        ]
        return bool(
            parts
            and ".." not in parts
            and not os.path.isabs(startup_path)
            and os.path.normcase(parts[-1])
            == os.path.normcase(os.path.basename(executable_path))
        )

    @classmethod
    def _root_from_executable(cls, executable_path: str, startup_path: str) -> str:
        if not cls._is_safe_startup_path_for_executable(
            executable_path, startup_path
        ):
            return os.path.dirname(executable_path)
        root_path = executable_path
        parts = [
            part for part in str(startup_path).replace("\\", "/").split("/")
            if part not in ("", ".")
        ]
        for _part in parts:
            root_path = os.path.dirname(root_path)
        return root_path

    def get_distributions(self) -> List[int]:
        """获取游戏可用的分发ID列表"""
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        distributions = cloud_res.get_download_distributions(short_game_id)
        return self._normalize_distribution_ids(distributions)
        
    def get_launcher_data_for_distribution(self, distribution_id: int) -> Optional[dict]:
        """获取指定分发ID的启动器数据"""
        cache = genv.get("launcher_data_cache", {})
        if isinstance(cache, dict):
            cached_data = cache.get(str(distribution_id))
            if isinstance(cached_data, dict) and cached_data:
                return cached_data
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        distributions = cloud_res.get_download_distributions(short_game_id)
        distribution_ids = self._normalize_distribution_ids(distributions)
        if distribution_ids and distribution_id not in distribution_ids:
            return None
        import requests
        try:
            url=f"https://loadingbaycn.webapp.163.com/app/v1/game_library/app?force=1&app_id={distribution_id}"
            headers={
                "User-Agent": "",
                "channel": "mkt-h55",
            }
            session = requests.Session()
            session.trust_env = False
            response=session.get(url,headers=headers,timeout=10)
            if response.status_code!=200 or response.json().get("code")!=200:
                self.logger.error(f"请求启动器信息失败，状态码: {response.status_code}")
                return None
            data = response.json().get("data", {})
            if isinstance(cache, dict) and isinstance(data, dict) and data:
                cache[str(distribution_id)] = data
                genv.set("launcher_data_cache", cache, cached=False)
            return data
        except Exception as e:
            self.logger.exception(f"请求启动器信息失败: {str(e)}")
        return None
    
    def get_file_distribution_info(self, distribution_id: int) -> Optional[dict]:
        """获取指定分发ID的文件分发信息"""
        cache = genv.get("file_distribution_info_cache", {})
        if not isinstance(cache, dict):
            cache = {}
        cache_key = str(distribution_id)
        cached_data = cache.get(cache_key)
        if isinstance(cached_data, dict) and cached_data:
            return cached_data
        try:
            #https://loadingbaycn.webapp.163.com/app/v1/file_distribution/download_app?app_id=
            import requests
            url=f"https://loadingbaycn.webapp.163.com/app/v1/file_distribution/download_app?app_id={distribution_id}"
            headers={
                "User-Agent": "",
                "channel": "mkt-h55"
            }
            session = requests.Session()
            session.trust_env = False
            response=session.get(url,headers=headers,timeout=10)
            if response.status_code!=200 or response.json().get("code")!=200:
                return None
            data = response.json().get("data", {}).get("main_content", {})
            if isinstance(data, dict) and data:
                cache[cache_key] = data
                # Remote launcher data is immutable for one tool process.  Do
                # not persist it across launches, where a newer cloud version
                # may legitimately exist.
                genv.set("file_distribution_info_cache", cache, cached=False)
            return data
        except Exception as e:
            self.logger.exception(f"请求文件分发信息失败: {str(e)}")
            return None
            
    def try_update(
        self,
        distribution_id: int,
        max_concurrent_files: int,
        installation_id: str = "",
        progress_file: str = "",
        control_file: str = "",
    ) -> bool:
        """尝试将游戏更新到指定分发ID的版本"""
        self.last_update_async = False
        installation = self.get_installation(installation_id)
        if installation is None:
            self.logger.error("未找到要更新的游戏安装记录")
            return False
        dist_id = self._coerce_distribution_id(distribution_id)
        if installation.distribution_id not in (-1, dist_id):
            self.logger.error(
                f"安装记录分发不匹配: installation={installation.distribution_id}, request={dist_id}"
            )
            return False
        download_root = self.get_root_path(installation.installation_id)
        if not download_root or not os.path.exists(download_root):
            self.logger.error(f"游戏路径无效或不存在: {installation.path}")
            return False
        file_distribution_info = self.get_file_distribution_info(dist_id)
        if not file_distribution_info:
            self.logger.error(f"未找到分发ID {dist_id} 的文件分发信息")
            return False
        files = file_distribution_info.get("files", [])
        directories = file_distribution_info.get("directories", [])
        check_result, to_update = self.version_check(files, installation.installation_id)
        if check_result:
            installation.distribution_id = dist_id
            installation.installed_version = file_distribution_info.get(
                "version_code", installation.installed_version
            )
            installation.content_id = file_distribution_info.get(
                "app_content_id", installation.content_id
            )
            installation.updated_at = int(time.time())
            installation.write_marker(self.game_id)
            self.logger.info(f"游戏已是最新版本，无需更新")
            return True
        if not to_update:
            installation.distribution_id = dist_id
            installation.installed_version = file_distribution_info.get(
                "version_code", installation.installed_version
            )
            installation.updated_at = int(time.time())
            installation.write_marker(self.game_id)
            return True
        
        repair_paths = []
        for item in to_update:
            rel_path = item.get("path", "")
            if rel_path:
                repair_paths.append(rel_path.replace("\\", "/"))
        repair_list_path = self._create_repair_list_file(repair_paths)
        if not repair_list_path:
            self.logger.error("创建repair列表文件失败")
            return False
        
        task_data = {
            "download_root": download_root,
            "concurrent_files": max_concurrent_files,
            "directories": directories,
            "files": to_update,
            "version_code": file_distribution_info.get(
                "version_code", installation.installed_version
            ),
            "game_id": self.game_id,
            "installation_id": installation.installation_id,
            "distribution_id": dist_id,
            "content_id":file_distribution_info.get("app_content_id"),
            "repair_list_path": repair_list_path,
            "progress_file": os.path.abspath(progress_file) if progress_file else "",
            "control_file": os.path.abspath(control_file) if control_file else "",
            "original_version": installation.installed_version,
            "start_args": installation.startup_args
            or CloudRes().get_start_argument(getShortGameId(self.game_id))
            or ""
        }
        task_file_path = self._create_download_task_file(task_data)
        if not task_file_path:
            self.logger.error("创建下载任务文件失败")
            return False
        if not self._spawn_download_process(task_file_path):
            self.logger.error("启动下载子进程失败")
            return False
        self.last_update_async = True
        return True

    def _create_download_task_file(self, task_data: dict) -> Optional[str]:
        try:
            workdir = genv.get("FP_WORKDIR", os.getcwd())
            os.makedirs(workdir, exist_ok=True)
            token = base64.urlsafe_b64encode(os.urandom(6)).decode("utf-8").rstrip("=")
            filename = f"download_task_{self.game_id}_{int(time.time())}_{token}.json"
            task_file_path = os.path.join(workdir, filename)
            with open(task_file_path, "w", encoding="utf-8") as f:
                json.dump(task_data, f, ensure_ascii=False)
            return task_file_path
        except Exception as e:
            self.logger.exception(f"创建下载任务文件失败: {e}")
            return None

    def _create_repair_list_file(self, repair_paths: List[str]) -> Optional[str]:
        try:
            workdir = genv.get("FP_WORKDIR", os.getcwd())
            os.makedirs(workdir, exist_ok=True)
            token = base64.urlsafe_b64encode(os.urandom(6)).decode("utf-8").rstrip("=")
            filename = f"repair_{self.game_id}_{int(time.time())}_{token}.txt"
            file_path = os.path.join(workdir, filename)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("\n".join(repair_paths))
            return file_path
        except Exception as e:
            self.logger.exception(f"创建repair列表文件失败: {e}")
            return None

    def _spawn_download_process(self, task_file_path: str) -> bool:
        try:
            if getattr(sys, "frozen", False):
                command = [os.path.abspath(sys.executable), "--download", task_file_path]
            else:
                command = [
                    sys.executable,
                    os.path.abspath(sys.argv[0]),
                    "--download",
                    task_file_path,
                ]
            script_dir = genv.get("SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            )
            # The main process is already elevated during initialize().  A
            # second ShellExecute("runas") only creates a visible supervisor
            # console, so launch the download supervisor directly and silent.
            subprocess.Popen(
                command,
                cwd=script_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True
        except Exception as e:
            self.logger.exception(f"启动下载子进程失败: {e}")
            return False
    def need_update(self, distribution_id: int, installation_id: str = "") -> bool:
        """检查游戏是否需要更新到指定分发ID的版本"""
        installation = self.get_installation(installation_id)
        if not installation or not installation.path or not os.path.exists(installation.path):
            return False
        if not CloudRes().is_downloadable(getShortGameId(self.game_id)):
            return False
        file_distribution_info = self.get_file_distribution_info(distribution_id)
        if not file_distribution_info:
            self.logger.error(f"未找到分发ID {distribution_id} 的文件分发信息")
            return False
        files = file_distribution_info.get("files", [])
        _check_result, to_update = self.version_check(files, installation.installation_id)
        return not check_result

    def version_check(
        self,
        files: List[dict],
        installation_id: str = "",
        hash_cache: Optional[Dict[str, str]] = None,
        root_path: str = "",
    ) -> Tuple[bool, List[dict]]:
        """检查游戏版本是否匹配, 返回需要更新的文件列表"""
        root_path = root_path or self.get_root_path(installation_id)
        if not root_path or not os.path.exists(root_path):
            return False, files
        to_update = []
        for file_info in files:
            #file_info中的是相对路径
            if file_info.get("op",1)!=1:
                continue
            file_path = os.path.normpath(
                os.path.join(root_path, file_info.get("path", ""))
            )
            if not os.path.exists(file_path):
                to_update.append(file_info)
                continue
            #计算xxh64值
            if hash_cache is not None and file_path in hash_cache:
                local_xxh64 = hash_cache[file_path]
            else:
                local_xxh64 = calculate_xxh64(file_path)
                if hash_cache is not None:
                    hash_cache[file_path] = local_xxh64
            if local_xxh64 != file_info.get("xxh", ""):
                to_update.append(file_info)
        return len(to_update) == 0, to_update

    def _extract_file_size(self, file_info: dict) -> int:
        for key in ["size", "file_size", "filesize", "length", "fileSize"]:
            value = file_info.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    continue
        return 0

    def get_update_stats(
        self, distribution_id: int, installation_id: str = ""
    ) -> Optional[dict]:
        installation = self.get_installation(installation_id)
        if installation is None:
            return None
        dist_id = self._coerce_distribution_id(distribution_id)
        if installation.distribution_id not in (-1, dist_id):
            return None
        download_root = self.get_root_path(installation.installation_id)
        if not download_root or not os.path.exists(download_root):
            return None
        file_distribution_info = self.get_file_distribution_info(dist_id)
        if not file_distribution_info:
            return None
        files = file_distribution_info.get("files", [])
        check_result, to_update = self.version_check(files, installation.installation_id)
        download_bytes = sum(self._extract_file_size(item) for item in to_update)
        usage = shutil.disk_usage(download_root)
        target_version = str(file_distribution_info.get("version_code", "") or "")
        return {
            "needs_update": bool(
                target_version
                and str(installation.installed_version or "") != target_version
            ),
            "download_bytes": download_bytes,
            "file_count": len(to_update),
            "disk_free_bytes": usage.free,
            "disk_total_bytes": usage.total,
            "target_version": target_version,
        }

    def is_downloadable_fever(self) -> bool:
        """检查游戏是否有Fever版本可供下载"""
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        return cloud_res.is_downloadable(short_game_id)
    
    def get_distribution_options(self) -> List[dict]:
        """获取游戏的分发选项"""
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        return cloud_res.get_download_distributions(short_game_id)
    
    def get_default_distribution(self) -> int:
        """获取游戏的默认分发ID"""
        return self.default_distribution
    def set_default_distribution(self, distribution_id: int=-1) -> None:
        """设置游戏的默认分发ID"""
        if distribution_id==-1:
            distributions = self.get_distributions()
            if distributions:
                self.default_distribution = distributions[0]
            else:
                self.default_distribution = -1
        else:
            self.default_distribution = distribution_id
    
    def get_version(self) -> str:
        """获取游戏版本号"""
        return self.version
    
    def can_convert_to_normal(self) -> bool:
        """检查游戏是否可以转换为普通版本"""
        cloud_res = CloudRes()
        short_game_id = getShortGameId(self.game_id)
        return cloud_res.is_convert_to_normal(short_game_id)
    

class GameManager:
    GAMES_CACHE_KEY = "game_settings"
    INSTALLATIONS_CACHE_KEY = "game_installation_settings_v1"
    
    def __init__(self):
        self.logger = setup_logger()
        self.games: Dict[str, Game] = {}
        self._fever_import_thread = None
        self._load_games()

    def _load_games(self):
        """从缓存中加载游戏设置"""
        try:
            game_settings = genv.get(self.GAMES_CACHE_KEY, {})
            installation_settings = genv.get(self.INSTALLATIONS_CACHE_KEY, {})
            if not isinstance(installation_settings, dict):
                installation_settings = {}
            migrated = False
            if game_settings and isinstance(game_settings, dict):
                for game_id, game_data in game_settings.items():
                    if not isinstance(game_data, dict):
                        continue
                    installation_state = installation_settings.get(game_id)
                    if installation_state is None:
                        installation_state = game_data.get("installation_state_v1")
                    if installation_state is None and game_data.get("installations"):
                        installation_state = {
                            "schema_version": 1,
                            "default_installation_id": game_data.get(
                                "default_installation_id", ""
                            ),
                            "installations": game_data.get("installations", {}),
                        }
                    if installation_state is None and game_data.get("path"):
                        migrated = True
                    game = Game.from_dict(game_data, installation_state)
                    if game.legacy_projection_merged:
                        migrated = True
                    if (
                        isinstance(installation_state, dict)
                        and "legacy_projection" not in installation_state
                    ):
                        migrated = True
                    if game_data.get("installation_state_v1") or game_data.get("installations"):
                        migrated = True
                    self.games[game_id] = game
            if migrated:
                self._save_games()
                self.logger.info("已将旧版单路径游戏记录迁移为安装实例模型")
        except Exception as e:
            self.logger.exception(f"加载游戏设置失败: {str(e)}")
            # 初始化空数据以恢复
            genv.set(self.GAMES_CACHE_KEY, {}, cached=True)

    def _save_games(self):
        """保存游戏设置到缓存"""
        try:
            game_settings = {game_id: game.to_dict() for game_id, game in self.games.items()}
            installation_settings = {
                game_id: game.to_installation_state()
                for game_id, game in self.games.items()
            }
            genv.set(self.GAMES_CACHE_KEY, game_settings, cached=True)
            genv.set(self.INSTALLATIONS_CACHE_KEY, installation_settings, cached=True)
        except Exception as e:
            self.logger.exception(f"保存游戏设置失败: {str(e)}")

    def get_game(self, game_id: str) -> Optional[Game]:
        """获取指定游戏ID的游戏信息"""
        if not game_id:
            return None
        if game_id not in self.games:
            for key in self.games.keys():
                if cmp_game_id(key, game_id):
                    return self.games.get(key)
        # 如果游戏ID不存在，则创建一个新的游戏记录
        if game_id not in self.games:
            self.games[game_id] = Game(game_id=game_id)
            self._save_games()
            
        return self.games.get(game_id)

    def get_existing_game(self, game_id: str) -> Optional[Game]:
        if not game_id:
            return None
        if game_id in self.games:
            return self.games.get(game_id)
        for key in self.games.keys():
            if cmp_game_id(key, game_id):
                return self.games.get(key)
        return None

    def find_matching_game_id(self, game_id: str) -> Optional[str]:
        if not game_id:
            return None
        for key in self.games.keys():
            if cmp_game_id(key, game_id):
                return key
        return None

    def get_game_or_temp(self, game_id: str) -> Optional[Game]:
        game = self.get_existing_game(game_id)
        if game:
            return game
        return Game(game_id=game_id)

    def list_games(self) -> List[dict]:
        """列出所有已保存的游戏信息"""
        return sorted(
            [game.get_non_sensitive_data() for game in self.games.values()],
            key=lambda x: x["last_used_time"],
            reverse=True
        )

    def add_game_installation(
        self,
        game_id: str,
        path: str,
        distribution_id: int = -1,
        installed_version: str = "",
        source: str = "manual",
        content_id=None,
        startup_path: str = "",
        startup_args: str = "",
        installation_id: str = "",
        set_default: bool = True,
    ) -> Optional[GameInstallation]:
        if not game_id or not path:
            return None
        game = self.get_game(game_id)
        if game is None:
            return None
        installation = game.add_installation(
            path=path,
            distribution_id=distribution_id,
            installed_version=installed_version,
            source=source,
            content_id=content_id,
            startup_path=startup_path,
            startup_args=startup_args,
            installation_id=installation_id,
            set_default=set_default,
        )
        self._save_games()
        return installation

    def identify_game_installation(
        self,
        game_id: str,
        installation_id: str,
        *,
        force: bool = False,
        persist: bool = True,
    ) -> int:
        """Classify one local install against every cloud distribution."""
        game = self.get_existing_game(game_id)
        installation = game.get_installation(installation_id) if game else None
        if installation is None:
            return -1
        if installation.distribution_id != -1 and not force:
            return installation.distribution_id
        distribution_options = game.get_distribution_options()
        distribution_ids = game._normalize_distribution_ids(distribution_options)
        if not distribution_ids:
            return installation.distribution_id
        file_info_by_distribution = {}
        launcher_data_by_distribution = {}
        for dist_id in distribution_ids:
            launcher_data_by_distribution[dist_id] = (
                game.get_launcher_data_for_distribution(dist_id) or {}
            )
            file_info_by_distribution[dist_id] = (
                game.get_file_distribution_info(dist_id) or {}
            )
        before = installation.to_dict()
        result = game.identify_installation_distribution(
            installation.installation_id,
            distribution_options,
            file_info_by_distribution,
            launcher_data_by_distribution,
            force=force,
        )
        if persist and installation.to_dict() != before:
            self._save_games()
        return result

    def set_game_path(self, game_id: str, path: str) -> bool:
        """选择游戏路径，同时保留其他已知安装。

        同一路径会复用已有安装；带标记的路径会恢复安装身份。对于有云端
        分发清单的游戏，会立即重新按哈希最接近原则识别分发与当前版本。
        """
        if not game_id:
            return False
        game = self.get_game(game_id)
        if game is None:
            return False
        normalized = GameInstallation._normalize_path(path)
        marker = GameInstallation.read_marker(normalized)
        installation = None
        if marker and cmp_game_id(marker.get("game_id", ""), game_id):
            installation = game.add_installation(
                path=normalized,
                distribution_id=marker.get("distribution_id", -1),
                installed_version=marker.get("installed_version", ""),
                source=marker.get("source", "download"),
                content_id=marker.get("content_id"),
                startup_path=marker.get("startup_path", os.path.basename(normalized)),
                startup_args=marker.get("startup_args", ""),
                installation_id=marker.get("installation_id", ""),
                set_default=True,
            )
        else:
            matching = next(
                (
                    item for item in game.installations.values()
                    if os.path.normcase(os.path.normpath(item.path or ""))
                    == os.path.normcase(os.path.normpath(normalized or ""))
                ),
                None,
            )
            if matching:
                game.set_default_installation(matching.installation_id)
                installation = matching
            elif normalized:
                invalid_ids = [
                    item.installation_id
                    for item in game.installations.values()
                    if not item.path or not os.path.exists(item.path)
                ]
                for installation_id in invalid_ids:
                    game.remove_installation(installation_id)
                installation = game.add_installation(
                    normalized,
                    source="manual",
                    startup_path=os.path.basename(normalized),
                    set_default=True,
                )
            else:
                default = game.get_installation()
                if default:
                    default.path = ""
                    default.updated_at = int(time.time())
        if installation is not None:
            if not installation.startup_path:
                installation.startup_path = os.path.basename(normalized)
            # A concrete distribution on an existing path is the persisted
            # result of an earlier classification.  Trust it instead of
            # hashing the game again whenever the same path is selected or
            # reconciled by launcher-status.
            if installation.distribution_id == -1:
                self.identify_game_installation(
                    game_id,
                    installation.installation_id,
                    force=False,
                    persist=False,
                )
        game.last_used_time = int(time.time())
        self._save_games()
        return True

    def set_installation_path(
        self, game_id: str, installation_id: str, path: str
    ) -> bool:
        game = self.get_existing_game(game_id)
        installation = game.get_installation(installation_id) if game else None
        if installation is None:
            return False
        installation.path = GameInstallation._normalize_path(path)
        installation.updated_at = int(time.time())
        game.last_used_time = installation.updated_at
        self._save_games()
        return True

    def set_game_auto_start(
        self,
        game_id: str,
        should_auto_start: bool,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> bool:
        """Set auto-start for one distribution installation."""
        if not game_id:
            return False
            
        game = self.get_game(game_id)
        if game:
            installation = game.resolve_installation(
                installation_id, distribution_id
            )
            explicit_scope = bool(installation_id) or (
                Game._coerce_distribution_id(distribution_id) != -1
            )
            if explicit_scope and installation is None:
                return False
            if installation is not None:
                for item in game.installations.values():
                    if item is not installation:
                        item.settings["auto_start"] = False
                        item.updated_at = int(time.time())
                installation.settings["auto_start"] = bool(should_auto_start)
                installation.updated_at = int(time.time())
            game.should_auto_start = should_auto_start
            game.last_used_time = int(time.time())
            self._save_games()
            return True
        return False

    def get_game_auto_start(
        self,
        game_id: str,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> dict:
        """Get auto-start for one distribution installation."""
        game = self.get_game(game_id)
        if game:
            installation = game.resolve_installation(
                installation_id, distribution_id
            )
            explicit_scope = bool(installation_id) or (
                Game._coerce_distribution_id(distribution_id) != -1
            )
            if explicit_scope and installation is None:
                return {"enabled": False, "path": "", "installation_id": ""}
            return {
                "enabled": bool(game.get_installation_setting(
                    "auto_start",
                    game.should_auto_start,
                    installation_id,
                    distribution_id,
                )),
                "path": installation.path if installation else "",
                "installation_id": (
                    installation.installation_id if installation else ""
                ),
            }
        return {"enabled": False, "path": "", "installation_id": ""}

    def start_game(self, game_id: str, installation_id: str = "") -> bool:
        """启动游戏"""
        game = self.get_game(game_id)
        installation = game.get_installation(installation_id) if game else None
        if not installation or not installation.path or not os.path.exists(installation.path):
            self.logger.error(
                f"游戏路径无效或不存在: {installation.path if installation else '未设置'}"
            )
            return False
        try:
            if not game.start(installation.installation_id):
                return False
            game.last_used_time = int(time.time())
            self._save_games()
            self.logger.info(f"游戏 {game_id} 启动成功")
            return True
        except Exception as e:
            self.logger.exception(f"启动游戏失败: {str(e)}")
            return False

    def rename_game(self, game_id: str, new_name: str) -> bool:
        """重命名游戏"""
        if not game_id or not new_name:
            return False
            
        game = self.get_game(game_id)
        if game:
            game.name = new_name
            game.last_used_time = int(time.time())
            self._save_games()
            return True
        return False

    def set_game_default_distribution(self, game_id: str, distribution_id: int) -> bool:
        if not game_id:
            return False
        game = self.get_game(game_id)
        if game:
            installation = game.get_installation_for_distribution(distribution_id)
            if installation:
                game.set_default_installation(installation.installation_id)
            else:
                default = game.get_installation()
                if default is None:
                    return False
                default.distribution_id = Game._coerce_distribution_id(distribution_id)
                default.updated_at = int(time.time())
            game.last_used_time = int(time.time())
            self._save_games()
            return True
        return False

    def set_game_default_installation(
        self, game_id: str, installation_id: str
    ) -> bool:
        game = self.get_existing_game(game_id)
        if not game or not game.set_default_installation(installation_id):
            return False
        self._save_games()
        return True

    def remove_game_installation(self, game_id: str, installation_id: str) -> bool:
        game = self.get_existing_game(game_id)
        if not game or not game.remove_installation(installation_id):
            return False
        scoped_auto_start = [
            item.settings.get("auto_start")
            for item in game.installations.values()
            if "auto_start" in item.settings
        ]
        if scoped_auto_start:
            game.should_auto_start = any(scoped_auto_start)
        elif not game.installations:
            game.should_auto_start = False
        self._save_games()
        return True

    def set_fever_bridge_forced(
        self, game_id: str, distribution_id: int, forced: bool
    ) -> bool:
        game = self.get_existing_game(game_id)
        if not game or not game.set_fever_bridge_forced(
            distribution_id, forced
        ):
            return False
        self._save_games()
        return True

    def set_auto_close_setting(
        self,
        game_id: str,
        auto_close: bool,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> bool:
        """Set post-login auto-close for one distribution installation."""
        if not game_id:
            return False
            
        game = self.get_game(game_id)
        if game:
            installation = game.set_installation_setting(
                "auto_close_after_login",
                bool(auto_close),
                installation_id,
                distribution_id,
            )
            explicit_scope = bool(installation_id) or (
                Game._coerce_distribution_id(distribution_id) != -1
            )
            if explicit_scope and installation is None:
                return False
            game.auto_close_after_login = auto_close
            game.last_used_time = int(time.time())
            self._save_games()
            return True
        return False
    
    def get_auto_close_setting(
        self,
        game_id: str,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> bool:
        """Get post-login auto-close for one distribution installation."""
        game = self.get_game(game_id)
        if game:
            if (
                (installation_id or Game._coerce_distribution_id(distribution_id) != -1)
                and game.resolve_installation(installation_id, distribution_id) is None
            ):
                return False
            return bool(game.get_installation_setting(
                "auto_close_after_login",
                game.auto_close_after_login,
                installation_id,
                distribution_id,
            ))
        return False

    def list_auto_start_games(self) -> List[Game]:
        """列出所有设置为自动启动的游戏"""
        return [
            game for game in self.games.values()
            if game.get_auto_start_installation() is not None
        ]
    
    def set_login_delay(
        self,
        game_id: str,
        delay: int,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> bool:
        """Set login delay for one distribution installation."""
        if not game_id:
            return False
            
        game = self.get_game(game_id)
        if game:
            installation = game.set_installation_setting(
                "login_delay",
                int(delay),
                installation_id,
                distribution_id,
            )
            explicit_scope = bool(installation_id) or (
                Game._coerce_distribution_id(distribution_id) != -1
            )
            if explicit_scope and installation is None:
                return False
            game.login_delay = delay
            game.last_used_time = int(time.time())
            self._save_games()
            return True
        return False
    
    def get_login_delay(
        self,
        game_id: str,
        installation_id: str = "",
        distribution_id: int = -1,
    ) -> int:
        """Get login delay for one distribution installation."""
        game = self.get_game(game_id)
        if game:
            if (
                (installation_id or Game._coerce_distribution_id(distribution_id) != -1)
                and game.resolve_installation(installation_id, distribution_id) is None
            ):
                return 6
            return int(game.get_installation_setting(
                "login_delay",
                game.login_delay,
                installation_id,
                distribution_id,
            ))
        return 6

    def get_game_default_launcher_data(self, game_id: str) -> int:
        """获取游戏的默认启动器分发ID"""
        game = self.get_game(game_id)
        if game and game.default_distribution != -1:
            return game.get_launcher_data_for_distribution(game.default_distribution)
        return None
    
    def get_game_version(self, game_id: str) -> str:
        """获取游戏版本号"""
        game = self.get_game(game_id)
        if game:
            return game.get_version()
        return ""
    
    def get_game_distribution_options(self, game_id: str) -> List[dict]:
        """获取游戏的分发选项"""
        game = self.get_game(game_id)
        if game:
            return game.get_distribution_options()
        return []
    
    def get_game_launcher_data_for_distribution(self, game_id: str, distribution_id: int) -> Optional[dict]:
        """获取指定分发ID的启动器数据"""
        game = self.get_game(game_id)
        if game:
            return game.get_launcher_data_for_distribution(distribution_id)
        return None
    

    def list_fever_games(self) -> List[dict]:
        if sys.platform != "win32":
            return []
        cached_games = genv.get(_FEVER_GAMES_CACHE_KEY, None)
        if isinstance(cached_games, list):
            return [dict(item) for item in cached_games if isinstance(item, dict)]

        with _FEVER_GAMES_CACHE_LOCK:
            # The startup import worker and the UI may arrive here together.
            # Only the first caller should enumerate the registry; all other
            # callers reuse this process-local genv snapshot.
            cached_games = genv.get(_FEVER_GAMES_CACHE_KEY, None)
            if isinstance(cached_games, list):
                return [
                    dict(item) for item in cached_games if isinstance(item, dict)
                ]

            result = self._read_fever_games_registry()
            genv.set(_FEVER_GAMES_CACHE_KEY, result, cached=False)
            return [dict(item) for item in result]

    def _read_fever_games_registry(self) -> List[dict]:
        """Read a fresh Fever registry snapshot without consulting the cache."""
        import winreg
        result = []
        try:
            base_path = r"Software\FeverGames\FeverGamesInstaller\game"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_path) as key:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        subkey_path = f"{base_path}\\{subkey_name}"
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey_path) as subkey:
                            def read_value(name, default=None):
                                try:
                                    return winreg.QueryValueEx(subkey, name)[0]
                                except FileNotFoundError:
                                    return default

                            game_info_value = read_value("GameInfo")
                            install_path = read_value("LastInstallPath")
                            running_process_name = read_value("RunningProcessName")
                            startup_path = read_value("StartupPath")
                            version_code = read_value("VersionCode")
                            if not all([
                                game_info_value,
                                install_path,
                                running_process_name,
                                startup_path,
                                version_code,
                            ]):
                                index += 1
                                continue
                            game_info_value = str(game_info_value)
                            if game_info_value.startswith("@ByteArray(") and game_info_value.endswith(")"):
                                game_info_value = game_info_value[11:-1]
                            decoded_bytes = base64.b64decode(game_info_value)
                            decoded_str = decoded_bytes.decode('utf-8')
                            game_info_json = json.loads(decoded_str)
                            game_id = game_info_json.get('game_id')
                            display_name = (
                                read_value("DisplayName")
                                or game_info_json.get('display_name')
                            )
                            if not game_id:
                                index += 1
                                continue
                            startup_path = str(startup_path)
                            executable_path = os.path.join(
                                install_path, str(running_process_name)
                            )
                            if not os.path.isfile(executable_path):
                                index += 1
                                continue
                            content_id = read_value("AppContentId")
                            result.append({
                                "fever_id": f"{game_id}:{subkey_name}",
                                "game_id": game_id,
                                "display_name": display_name,
                                "path": executable_path,
                                "distribution_id": int(subkey_name),
                                "version_code": str(version_code),
                                "content_id": content_id,
                                "startup_path": startup_path,
                                "startup_args": str(
                                    read_value("StartupParams") or ""
                                ),
                            })
                            index += 1
                    except OSError:
                        break
        except Exception:
            self.logger.debug("读取Fever游戏列表失败")
        return result

    def initialize_downloadable_installation_versions(self) -> int:
        """Persist a cloud version baseline for remaining local installations.

        Fever records are merged first because their registry version and
        distribution are authoritative for that exact game path.  This pass
        only hashes local installations that still have no version afterwards.
        """
        cloud = CloudRes()
        changed_count = 0
        for game in self.games.values():
            short_game_id = getShortGameId(game.game_id)
            if not cloud.is_downloadable(short_game_id):
                continue
            pending = [
                item for item in game.installations.values()
                if item.path
                and os.path.isfile(item.path)
                and not item.installed_version
            ]
            if not pending:
                continue
            distribution_options = game.get_distribution_options()
            distribution_ids = game._normalize_distribution_ids(
                distribution_options
            )
            if not distribution_ids:
                continue
            file_info_by_distribution = {
                dist_id: game.get_file_distribution_info(dist_id) or {}
                for dist_id in distribution_ids
            }
            launcher_data_by_distribution = {
                dist_id: game.get_launcher_data_for_distribution(dist_id) or {}
                for dist_id in distribution_ids
            }
            for installation in pending:
                before = (
                    installation.distribution_id,
                    installation.installed_version,
                    installation.content_id,
                    installation.startup_path,
                    installation.startup_args,
                )
                game.identify_installation_distribution(
                    installation.installation_id,
                    distribution_options,
                    file_info_by_distribution,
                    launcher_data_by_distribution,
                    force=True,
                )
                after = (
                    installation.distribution_id,
                    installation.installed_version,
                    installation.content_id,
                    installation.startup_path,
                    installation.startup_args,
                )
                if after == before:
                    continue
                changed_count += 1
                installation.write_marker(game.game_id)
                self.logger.info(
                    "已初始化本地游戏版本基线: %s, installation=%s",
                    game.game_id,
                    installation.installation_id,
                )
        if changed_count:
            self._save_games()
        return changed_count

    def start_fever_auto_import(self) -> bool:
        """Merge complete Fever installs, then initialize local baselines.

        Registry parsing and distribution hash matching can take noticeable
        time, so startup only schedules one daemon worker.  An exact game/path
        match is enriched in place; otherwise an existing concrete
        distribution prevents a duplicate automatic import.
        """
        if self._fever_import_thread and self._fever_import_thread.is_alive():
            return False

        def _worker():
            try:
                imported = self.import_missing_fever_games()
                if imported:
                    self.logger.info(
                        "启动时自动导入或补齐Fever游戏完成: %s",
                        ", ".join(imported),
                    )
            except Exception:
                self.logger.exception("启动时自动导入Fever游戏失败")
            try:
                self.initialize_downloadable_installation_versions()
            except Exception:
                self.logger.exception("初始化本地游戏版本基线失败")

        self._fever_import_thread = threading.Thread(
            target=_worker,
            name="fever-auto-import",
            daemon=True,
        )
        self._fever_import_thread.start()
        return True

    def import_missing_fever_games(self) -> List[str]:
        """Synchronously import complete Fever installs without local records."""
        imported = []
        changed = False
        for record in self.list_fever_games():
            target_game_id = str(record.get("game_id") or "")
            target_path = str(record.get("path") or "")
            if not target_game_id or not target_path or not os.path.isfile(target_path):
                continue
            final_game_id = self.find_matching_game_id(target_game_id) or target_game_id
            game = self.get_existing_game(final_game_id)
            if game:
                invalid_ids = [
                    item.installation_id
                    for item in game.installations.values()
                    if not item.path or not os.path.isfile(item.path)
                ]
                for installation_id in invalid_ids:
                    changed = game.remove_installation(installation_id) or changed

                normalized_target = os.path.normcase(os.path.normpath(target_path))
                path_record = next((
                    item for item in game.installations.values()
                    if os.path.normcase(os.path.normpath(item.path or ""))
                    == normalized_target
                ), None)
                record_distribution_id = game._coerce_distribution_id(
                    record.get("distribution_id", -1)
                )
                distribution_record = game.get_installation_for_distribution(
                    record_distribution_id
                )
                if path_record is not None:
                    compatible_distribution = path_record.distribution_id in (
                        -1,
                        record_distribution_id,
                    )
                    record_version = str(record.get("version_code") or "")
                    hydrated = False
                    if compatible_distribution:
                        if (
                            path_record.distribution_id == -1
                            and record_distribution_id != -1
                        ):
                            game._claim_distribution(
                                path_record, record_distribution_id
                            )
                            hydrated = True
                        if not path_record.installed_version and record_version:
                            path_record.installed_version = record_version
                            hydrated = True
                        if (
                            path_record.content_id is None
                            and record.get("content_id") is not None
                        ):
                            path_record.content_id = record.get("content_id")
                            hydrated = True
                        if not path_record.startup_path and record.get("startup_path"):
                            path_record.startup_path = str(record["startup_path"])
                            hydrated = True
                        if not path_record.startup_args and record.get("startup_args"):
                            path_record.startup_args = str(record["startup_args"])
                            hydrated = True
                    if hydrated:
                        path_record.updated_at = int(time.time())
                        path_record.write_marker(game.game_id)
                        changed = True
                        if final_game_id not in imported:
                            imported.append(final_game_id)
                    continue
                if distribution_record is not None:
                    continue
                set_default = not game.installations
            else:
                set_default = True

            imported_game_id = self.import_fever_game(
                final_game_id,
                distribution_id=record.get("distribution_id", -1),
                path=target_path,
                create_shortcut=False,
                notify=False,
                set_default=set_default,
                refresh_registry=False,
            )
            if imported_game_id and imported_game_id not in imported:
                imported.append(imported_game_id)

        if changed:
            self._save_games()
        return imported

    def import_fever_game(
        self,
        game_id: str,
        distribution_id: int = -1,
        path: str = "",
        *,
        create_shortcut: bool = True,
        notify: bool = True,
        set_default: bool = True,
        refresh_registry: bool = True,
    ) -> Optional[str]:
        if not game_id:
            return None
        if refresh_registry:
            # An explicit import is the only in-process operation that must see
            # registry changes made after startup.  Hold the same lock used by
            # readers so concurrent launcher-status calls reuse this refresh.
            with _FEVER_GAMES_CACHE_LOCK:
                genv.set(_FEVER_GAMES_CACHE_KEY, None, cached=False)
                fever_games = self.list_fever_games()
        else:
            fever_games = self.list_fever_games()
        target = None
        for item in fever_games:
            same_game = cmp_game_id(item.get("game_id"), game_id)
            same_distribution = (
                distribution_id == -1
                or item.get("distribution_id") == int(distribution_id)
            )
            same_path = not path or os.path.normcase(os.path.normpath(item.get("path", ""))) == os.path.normcase(os.path.normpath(path))
            if same_game and same_distribution and same_path:
                target = item
                break
        if not target:
            return None
        executable_path = target.get("path", "")
        if not executable_path:
            return None
        target_game_id = target.get("game_id")
        matched_game_id = self.find_matching_game_id(target_game_id)
        final_game_id = matched_game_id or target_game_id
        game = self.games.get(final_game_id)
        display_name = target.get("display_name")
        distribution_id = target.get("distribution_id", -1)
        if game:
            if display_name:
                game.name = display_name
        else:
            game = Game(
                game_id=final_game_id,
                name=display_name if display_name else final_game_id,
            )
            self.games[final_game_id] = game
        installation = game.add_installation(
            path=executable_path,
            distribution_id=distribution_id,
            installed_version=target.get("version_code", ""),
            source="fever",
            content_id=target.get("content_id"),
            startup_path=(
                target.get("startup_path") or os.path.basename(executable_path)
            ),
            startup_args=target.get("startup_args", ""),
            set_default=set_default,
        )
        self.identify_game_installation(
            final_game_id,
            installation.installation_id,
            force=True,
            persist=False,
        )
        # Distribution matching provides cloud launch metadata, while Fever's
        # registry records the version that is actually installed on disk.
        if target.get("version_code"):
            installation.installed_version = str(target["version_code"])
        if target.get("content_id") is not None:
            installation.content_id = target.get("content_id")
        if create_shortcut:
            try:
                game.create_tool_launch_shortcut(
                    installation.path, installation.installation_id
                )
                if notify:
                    app_state.toast(f"成功导入发烧平台游戏，下次启动请使用桌面上的{game.name}(IDV-LOGIN)快捷方式", duration=3000)
            except Exception:
                self.logger.exception("导入Fever游戏后创建快捷方式失败")
        self._save_games()
        return final_game_id


if __name__ == "__main__":
    game_mgr = GameManager()

    game_mgr._save_games()
    g=game_mgr.get_game("h55")
    g.try_update(g.default_distribution,max_concurrent_files=1)
    
