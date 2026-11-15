import os
import queue
import threading
import time
import sys
import json
import re
import requests
from datetime import datetime
from envmgr import genv
from const import manual_login_channels
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.channelUtils import cmp_game_id

logger = setup_logger()

_MODIFIED = "modified"
_NOT_MODIFIED = "not_modified"
_ERROR = "error"

class CloudRes:
    # 单例实例
    _instance = None
    # 初始化标记
    _initialized = False
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, urls=[], cache_dir='./'):
        # 防止重复初始化
        if not self._initialized:
            self.urls = urls
            self.cache_dir = cache_dir
            self.cache_file = os.path.join(cache_dir, 'cache.json')
            self.local_data = self.load_local_cache()
            self.session = requests.Session()
            self.session.trust_env = False
            self._initialized = True

    def fetch_json_from_url(self, urls=None, snapshot=None):
        local_data = snapshot[1] if snapshot is not None else self.local_data
        for url in self.urls if urls is None else urls:
            try:
                response = self.session.get(url, timeout=60, verify=should_verify_ssl())
                response.raise_for_status()
                data = response.json()
                if (isinstance(data, dict) and type(data.get('lastModified')) is int
                        and data['lastModified'] >= local_data.get('lastModified', 0)):
                    return data
            except (requests.RequestException, ValueError):
                pass
        return None

    def load_local_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) and type(data.get('lastModified')) is int else {}
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse cache file: {e}")
            return {}
        except IOError as e:
            logger.error(f"Failed to read cache file: {e}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error while loading cache file: {e}")
            return {}

    def update_cache_if_needed(self):
        results = queue.Queue()
        ready = threading.Event()
        deadline = time.monotonic() + 60
        snapshot = (None, self.local_data)
        urls = tuple(self.urls)

        def fetch(url):
            try:
                data = self.fetch_json_from_url([url], snapshot)
                result = (_MODIFIED, data) if data else (_ERROR, None)
            except Exception:
                result = (_ERROR, None)
            results.put(result)

        def collect():
            applied = False
            try:
                for _ in urls:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        status, data = results.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if status == _ERROR:
                        continue
                    try:
                        self._apply_fetch_result(status, data, background=applied)
                    except OSError:
                        continue
                    applied = True
                    ready.set()
                if not applied:
                    self._apply_fetch_result(_ERROR, None)
            finally:
                ready.set()

        for url in urls:
            threading.Thread(target=fetch, args=(url,), daemon=True).start()
        threading.Thread(target=collect, daemon=True).start()
        ready.wait(max(0, deadline - time.monotonic()))

    def _apply_fetch_result(self, status, cloud_data, background=False):
        if status == _NOT_MODIFIED:
            if not background:
                logger.info("本地配置已是最新")
            return
        if status == _ERROR or cloud_data is None:
            if not background:
                logger.warning("获取云端配置失败，将继续使用本地配置")
            return
        if cloud_data.get('lastModified', 0) > self.local_data.get('lastModified', 0):
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(cloud_data, f, ensure_ascii=False, indent=4)
            self.local_data = cloud_data
            if not background:
                logger.info("云端配置有更新，应用成功")
        elif not background:
            logger.info("本地配置已是最新")

    def get_channelData(self, channelName,shortGameId):
        data=self.local_data.get('data', [])
        for item in data:
            if item.get('app_channel') == channelName and item.get('game_id') == shortGameId:
                return item
        return None

    def get_by_game_id(self,shortGameId):
        data=self.local_data.get('data', [])
        for item in data:
            if item.get('game_id') == shortGameId:
                return item
        return None
    
    def get_netease_style_pkgname_by_game_id(self,shortGameId):
        data=self.local_data.get('data', [])
        #正则匹配，netease style指的是package_name字段包含com.netease.A或者com.netease.A.B中的com.netease.A部分，且game_id匹配
        pattern = f"com\\.netease\\.[^.]+"
        for item in data:
            if item.get('game_id') == shortGameId and item.get('package_name') and re.match(pattern, item.get('package_name')):
                return item.get('package_name')
        return None
    
    #same with data, but key is feature_game_short_ids
    def get_feature_by_game_id(self,shortGameId):
        data=self.local_data.get('feature_game_short_ids', [])
        for item in data:
            if item.get('game_id') == shortGameId:
                return item
        return None
    
    def get_all_by_game_id(self,shortGameId):
        data=self.local_data.get('data', [])
        result = []
        for item in data:
            if item.get('game_id') == shortGameId:
                result.append(item)
        return result

    def get_by_game_id_and_key(self,shortGameId,key):
        data=self.local_data.get('data', [])
        for item in data:
            if item.get('game_id') == shortGameId and item.get(key) != "" and item.get(key) != None:
                return item.get(key)
        return None

    def get_version(self):
        return self.local_data.get('version', genv.get('VERSION'))

    def get_netease_qrcode_login_game_list(self):
        return self.local_data.get('netease_qrcode_login_game_list', [])

    def is_game_in_qrcode_login_list(self,game_id):
        game_list = self.get_netease_qrcode_login_game_list()
        for item in game_list:
            if cmp_game_id(item.get('game_id'), game_id):
                return True
        return False

    def get_qrcode_app_channel(self,game_id):
        config = self.get_qrcode_login_config(game_id)
        return config.get('app_channel') if config else None

    def get_qrcode_login_config(self, game_id):
        """返回 netease_qrcode_login_game_list 中指定 game_id 的完整配置字典，
        包含 app_channel 以及 cv、qrcode_channel_type 等额外参数。"""
        game_list = self.get_netease_qrcode_login_game_list()
        for item in game_list:
            if cmp_game_id(item.get('game_id'), game_id):
                return item
        return None

    def get_announcement(self):
        return self.local_data.get('announcement', '')

    def get_downloadUrl(self):
        return self.local_data.get('downloadUrl', '')
    
    def get_guideUrl(self):
        return self.local_data.get('guideUrl', '')
    
    def get_detail(self):
        return self.local_data.get('detail', '')
    
    def get_detail_html(self):
        return self.local_data.get('detail_html', self.get_detail())
    
    def get_risk_wm(self):
        return self.local_data.get('risk_wm', '')
    
    def get_login_page(self):
        import base64
        return base64.b64decode(self.local_data.get('login_base64_page', '')).decode()
    
    def get_shortcuts(self):
        return self.local_data.get('shortcuts', [])
    
    def is_update_critical(self):
        return self.local_data.get('critical_update', False)
    
    def get_start_argument(self,shortGameId):
        features = self.get_feature_by_game_id(shortGameId)
        if not features:
            return ""
        return features.get('start_argument', "")
    
    def get_download_distributions(self,shortGameId):
        features = self.get_feature_by_game_id(shortGameId)
        if not features:
            return []
        return features.get('download_distributions', [])
    
    def is_convert_to_normal(self,shortGameId):
        features = self.get_feature_by_game_id(shortGameId)
        if not features:
            return False
        return features.get('convert_to_normal', False)
    
    def is_downloadable(self,shortGameId):
        features = self.get_feature_by_game_id(shortGameId)
        if not features:
            return True
        return features.get('downloadable', True)

    def get_hotfixes(self):
        """返回云端下发的热更新配置列表。

        兼容字段名：hotfix。
        期望每个元素包含：need_hotfix_version, target_module, target_commit, note。
        """
        data = self.local_data or {}
        hotfixes = data.get("hotfix") or []
        if not isinstance(hotfixes, list):
            return []
        return hotfixes