import hashlib
import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from logutil import setup_logger
from secure_write import write_json_restricted
from ssl_utils import should_verify_ssl


logger = setup_logger()


class DynamicGameCatalog:
    """Load the public Fever catalog and map it to cloud game IDs.

    The generated data is kept separate from ``cache.json``.  Callers can use
    it to fill gaps in the hand-maintained cloud configuration without ever
    replacing an existing entry.
    """

    GAME_CONFIG_URL = (
        "https://gamepay.163.com/common_api/internal/game_config"
        "?appChannel=netease.allysdk3rd"
    )
    HOMEPAGE_URL = (
        "https://loadingbaycn.webapp.163.com/app/v1/game_store/homepage_info"
    )
    APP_DETAIL_URL = (
        "https://loadingbaycn.webapp.163.com/app/v1/game_library/app"
        "?force=1&app_id={}"
    )
    CACHE_SCHEMA_VERSION = 2
    DEFAULT_REFRESH_INTERVAL = 24 * 60 * 60

    _MODIFIED = "modified"
    _UNCHANGED = "unchanged"
    _NOT_MODIFIED = "not_modified"
    _ERROR = "error"

    def __init__(
        self,
        cache_dir: str,
        session: Optional[requests.Session] = None,
        refresh_interval: int = DEFAULT_REFRESH_INTERVAL,
    ):
        self.cache_file = os.path.join(cache_dir, "dynamic_game_catalog.json")
        self.refresh_interval = max(0, int(refresh_interval))
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._lock = threading.RLock()
        self._refreshing = False
        self._cache = self._load_cache()
        self._games: List[dict] = []
        self._games_by_short_id: Dict[str, dict] = {}
        self._cloud_configs: Dict[str, dict] = {}
        self._apply_cache()

    @staticmethod
    def _short_game_id(game_id) -> str:
        return str(game_id or "").strip().split("-")[-1]

    def _load_cache(self) -> dict:
        try:
            with open(self.cache_file, "r", encoding="utf-8") as cache_handle:
                data = json.load(cache_handle)
            if data.get("schema_version") == self.CACHE_SCHEMA_VERSION:
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(f"读取动态游戏目录缓存失败: {exc}")
        return {
            "schema_version": self.CACHE_SCHEMA_VERSION,
            "checked_at": 0,
            "sources": {},
            "game_configs": {},
            "homepage_apps": [],
            "app_details": {},
            "games": [],
        }

    def _apply_cache(self) -> None:
        games = self._cache.get("games", [])
        configs = self._cache.get("game_configs", {})
        if not isinstance(games, list):
            games = []
        if not isinstance(configs, dict):
            configs = {}

        normalized_games = []
        games_by_short_id = {}
        for item in games:
            if not isinstance(item, dict):
                continue
            short_id = self._short_game_id(item.get("short_game_id"))
            distributions = item.get("download_distributions", [])
            platform_type = str(item.get("platform_type") or "fever")
            if not short_id or not isinstance(distributions, list):
                continue
            if platform_type == "fever" and not distributions:
                continue
            normalized_games.append(dict(item))
            games_by_short_id.setdefault(short_id, dict(item))

        with self._lock:
            self._games = normalized_games
            self._games_by_short_id = games_by_short_id
            self._cloud_configs = {
                self._short_game_id(key): dict(value)
                for key, value in configs.items()
                if self._short_game_id(key) and isinstance(value, dict)
            }

    @staticmethod
    def _json_hash(data) -> str:
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _content_for_hash(source_key: str, payload):
        """Exclude request-specific envelope fields from source fingerprints."""
        if not isinstance(payload, dict):
            return payload
        if source_key == "game_config":
            data = payload.get("data", {})
            return data.get("gameConfigList", []) if isinstance(data, dict) else []
        if source_key == "homepage_info":
            return payload.get("data", {})
        return payload

    def _request_json(
        self, source_key: str, url: str, headers: Optional[dict] = None
    ) -> Tuple[str, Optional[dict], dict]:
        previous_meta = self._cache.get("sources", {}).get(source_key, {})
        request_headers = dict(headers or {})
        if previous_meta.get("etag"):
            request_headers["If-None-Match"] = previous_meta["etag"]
        if previous_meta.get("last_modified"):
            request_headers["If-Modified-Since"] = previous_meta["last_modified"]

        try:
            response = self.session.get(
                url,
                timeout=10,
                verify=should_verify_ssl(),
                headers=request_headers,
            )
            if response.status_code == 304:
                return self._NOT_MODIFIED, None, dict(previous_meta)
            response.raise_for_status()
            payload = response.json()
            payload_hash = self._json_hash(
                self._content_for_hash(source_key, payload)
            )
            new_meta = {"sha256": payload_hash}
            if response.headers.get("ETag"):
                new_meta["etag"] = response.headers["ETag"]
            if response.headers.get("Last-Modified"):
                new_meta["last_modified"] = response.headers["Last-Modified"]
            status = (
                self._UNCHANGED
                if payload_hash == previous_meta.get("sha256")
                else self._MODIFIED
            )
            return status, payload, new_meta
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning(f"获取动态游戏目录失败 ({source_key}): {exc}")
            return self._ERROR, None, dict(previous_meta)

    @staticmethod
    def _normalize_game_configs(payload: dict) -> Dict[str, dict]:
        result = {}
        items = payload.get("data", {}).get("gameConfigList", [])
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            short_id = str(item.get("jf_gameid") or item.get("gameid") or "").strip()
            cloud_game_id = str(item.get("mpay_appid") or "").strip()
            if not short_id:
                continue
            result.setdefault(short_id, {
                "game_id": short_id,
                "jf_gameid": short_id,
                "cloud_game_id": cloud_game_id,
                "mpay_appid": cloud_game_id,
                "name": str(item.get("app_name") or "").strip(),
                "icon": str(item.get("iconimg") or "").strip(),
                "log_key": str(item.get("client_log_key") or "").strip(),
            })
        return result

    @staticmethod
    def _normalize_homepage_apps(payload: dict) -> List[dict]:
        ordered_apps = []
        apps_by_id = {}

        def visit(value):
            if isinstance(value, dict):
                app_data = value.get("app_data")
                if isinstance(app_data, dict):
                    try:
                        app_id = int(app_data.get("app_id"))
                    except (TypeError, ValueError):
                        app_id = -1
                    app_type = app_data.get("app_type")
                    if app_id >= 0 and app_type in (1, 3):
                        normalized = {
                            "app_id": app_id,
                            "app_type": app_type,
                            "display_name": str(
                                app_data.get("display_name") or ""
                            ).strip(),
                            "logo": str(app_data.get("logo") or "").strip(),
                            "goods_image": str(
                                app_data.get("goods_image") or ""
                            ).strip(),
                        }
                        if app_id not in apps_by_id:
                            apps_by_id[app_id] = normalized
                            ordered_apps.append(normalized)
                        else:
                            existing = apps_by_id[app_id]
                            for key, item_value in normalized.items():
                                if not existing.get(key) and item_value:
                                    existing[key] = item_value
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload.get("data", {}))
        return ordered_apps

    @staticmethod
    def _normalize_app_detail(payload: dict, expected_app_id: int) -> Optional[dict]:
        if payload.get("code") != 200 or not isinstance(payload.get("data"), dict):
            return None
        data = dict(payload["data"])
        short_id = str(data.get("game_id") or "").strip()
        if not short_id:
            return None
        try:
            app_id = int(data.get("app_id", expected_app_id))
        except (TypeError, ValueError):
            app_id = expected_app_id
        data["app_id"] = app_id
        data["game_id"] = short_id
        return data

    @staticmethod
    def _build_games(
        homepage_apps: List[dict], app_details: Dict[str, dict], configs: Dict[str, dict]
    ) -> List[dict]:
        games = []
        games_by_short_id = {}
        for app in homepage_apps:
            app_id = app.get("app_id")
            app_type = int(app.get("app_type") or 1)
            platform_type = "native_pc" if app_type == 3 else "fever"
            detail = app_details.get(str(app_id), {})
            short_id = str(detail.get("game_id") or "").strip()
            if not short_id:
                continue
            config = configs.get(short_id, {})
            cloud_game_id = str(config.get("cloud_game_id") or "").strip()
            existing = games_by_short_id.get(short_id)
            if existing:
                if platform_type == "fever" and app_id not in existing["download_distributions"]:
                    existing["download_distributions"].append(app_id)
                if app_id not in existing["catalog_app_ids"]:
                    existing["catalog_app_ids"].append(app_id)
                continue
            record = {
                "game_id": cloud_game_id or short_id,
                "short_game_id": short_id,
                "cloud_game_id": cloud_game_id,
                "name": (
                    detail.get("display_name")
                    or app.get("display_name")
                    or config.get("name")
                    or short_id
                ),
                "icon": (
                    detail.get("icon")
                    or detail.get("logo")
                    or app.get("logo")
                    or app.get("goods_image")
                    or config.get("icon")
                    or ""
                ),
                "platform_type": platform_type,
                "catalog_app_id": app_id,
                "catalog_app_ids": [app_id],
                "download_distributions": [app_id] if platform_type == "fever" else [],
                "launcher": dict(detail),
            }
            games.append(record)
            games_by_short_id[short_id] = record
        return games

    def _cache_is_fresh(self) -> bool:
        checked_at = self._cache.get("checked_at", 0)
        try:
            age = time.time() - int(checked_at)
        except (TypeError, ValueError):
            return False
        return bool(self._cache.get("games")) and age < self.refresh_interval

    def _refresh(self, force: bool, already_started: bool = False) -> bool:
        if not already_started:
            with self._lock:
                if self._refreshing:
                    return False
                self._refreshing = True
        try:
            if not force and self._cache_is_fresh():
                return False

            changed = False
            sources = dict(self._cache.get("sources", {}))
            config_status, config_payload, config_meta = self._request_json(
                "game_config", self.GAME_CONFIG_URL, {"X-Game": "base"}
            )
            sources["game_config"] = config_meta
            if config_status == self._MODIFIED or not self._cache.get("game_configs"):
                if config_payload is not None:
                    normalized_configs = self._normalize_game_configs(
                        config_payload
                    )
                    if normalized_configs != self._cache.get("game_configs", {}):
                        self._cache["game_configs"] = normalized_configs
                        changed = True

            homepage_status, homepage_payload, homepage_meta = self._request_json(
                "homepage_info", self.HOMEPAGE_URL
            )
            sources["homepage_info"] = homepage_meta
            if homepage_status == self._MODIFIED or not self._cache.get("homepage_apps"):
                if homepage_payload is not None:
                    normalized_apps = self._normalize_homepage_apps(
                        homepage_payload
                    )
                    if normalized_apps != self._cache.get("homepage_apps", []):
                        self._cache["homepage_apps"] = normalized_apps
                        changed = True

            homepage_apps = self._cache.get("homepage_apps", [])
            app_details = dict(self._cache.get("app_details", {}))
            active_ids = {str(item.get("app_id")) for item in homepage_apps}
            app_details = {
                key: value for key, value in app_details.items() if key in active_ids
            }
            for app in homepage_apps:
                app_id = app.get("app_id")
                detail_key = str(app_id)
                if detail_key in app_details:
                    continue
                detail_status, detail_payload, _ = self._request_json(
                    f"app_detail:{detail_key}",
                    self.APP_DETAIL_URL.format(app_id),
                    {"channel": "mkt-h55"},
                )
                if detail_status == self._ERROR or detail_payload is None:
                    continue
                detail = self._normalize_app_detail(detail_payload, app_id)
                if detail:
                    app_details[detail_key] = detail
                    changed = True

            games = self._build_games(
                homepage_apps,
                app_details,
                self._cache.get("game_configs", {}),
            )
            if games != self._cache.get("games", []):
                self._cache["games"] = games
                changed = True
            self._cache["sources"] = sources
            self._cache["app_details"] = app_details
            if config_status != self._ERROR and homepage_status != self._ERROR:
                self._cache["checked_at"] = int(time.time())
            self._cache["schema_version"] = self.CACHE_SCHEMA_VERSION
            write_json_restricted(self.cache_file, self._cache)
            self._apply_cache()
            if changed:
                downloadable = sum(
                    1 for item in games if item.get("platform_type") == "fever"
                )
                logger.info(
                    f"动态游戏目录已更新，共 {len(games)} 个游戏，"
                    f"其中可下载 {downloadable} 个"
                )
            return changed
        finally:
            with self._lock:
                self._refreshing = False

    def refresh(self, force: bool = False) -> bool:
        """Refresh the catalog; return whether the effective catalog changed."""
        return self._refresh(force)

    def start_background_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            # Mark loading before the thread starts so the first UI request can
            # reliably observe it and poll for the initial catalog.
            self._refreshing = True
        thread = threading.Thread(
            target=self._refresh,
            args=(False, True),
            name="dynamic-game-catalog",
            daemon=True,
        )
        thread.start()
        return True

    def get_games(self) -> List[dict]:
        with self._lock:
            return [dict(item) for item in self._games]

    def get_game(self, game_id: str) -> Optional[dict]:
        short_id = self._short_game_id(game_id)
        with self._lock:
            item = self._games_by_short_id.get(short_id)
            return dict(item) if item else None

    def get_cloud_config(self, game_id: str) -> Optional[dict]:
        short_id = self._short_game_id(game_id)
        with self._lock:
            item = self._cloud_configs.get(short_id)
            return dict(item) if item else None

    def resolve_cloud_game_id(self, game_id: str) -> str:
        original = str(game_id or "").strip()
        if "-" in original:
            return original
        config = self.get_cloud_config(original)
        return str(config.get("cloud_game_id") or original) if config else original

    def get_feature(self, game_id: str) -> Optional[dict]:
        item = self.get_game(game_id)
        if not item:
            return None
        return {
            "game_id": item["short_game_id"],
            "download_distributions": list(item["download_distributions"]),
            "downloadable": item.get("platform_type") == "fever",
            "platform_type": item.get("platform_type", "fever"),
            "catalog_app_id": item.get("catalog_app_id"),
        }

    def get_status(self) -> dict:
        with self._lock:
            return {
                "loading": self._refreshing,
                "ready": bool(self._games),
                "checked_at": self._cache.get("checked_at", 0),
            }
