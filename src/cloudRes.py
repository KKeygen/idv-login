import os
import json
import re
import requests
from envmgr import genv
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.channelUtils import cmp_game_id

logger = setup_logger()

# fetch_json_from_url 返回状态
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

    def _get_url_meta(self, url):
        """获取指定 URL 的 HTTP 缓存验证器"""
        all_meta = genv.get("_cloud_http_meta", {})
        return all_meta.get(url, {})

    def _set_url_meta(self, url, meta):
        """保存指定 URL 的 HTTP 缓存验证器"""
        all_meta = genv.get("_cloud_http_meta", {})
        all_meta[url] = meta
        genv.set("_cloud_http_meta", all_meta, True)

    def fetch_json_from_url(self):
        """尝试从云端获取配置，支持 HTTP 条件请求 (ETag/If-Modified-Since)。
        
        Returns:
            tuple: (status, data)
                - (_MODIFIED, dict): 服务器返回了新数据
                - (_NOT_MODIFIED, None): 304 未修改
                - (_ERROR, None): 所有 URL 均失败
        """
        for url in self.urls:
            try:
                headers = {}
                url_meta = self._get_url_meta(url)
                if url_meta.get("etag"):
                    headers["If-None-Match"] = url_meta["etag"]
                if url_meta.get("last_modified"):
                    headers["If-Modified-Since"] = url_meta["last_modified"]

                response = self.session.get(
                    url, timeout=10, verify=should_verify_ssl(), headers=headers
                )

                if response.status_code == 304:
                    logger.info(f"云端配置未变化 (304): {url}")
                    return (_NOT_MODIFIED, None)

                response.raise_for_status()
                data = response.json()

                # 更新该 URL 的缓存验证器
                new_meta = {}
                if response.headers.get("ETag"):
                    new_meta["etag"] = response.headers["ETag"]
                if response.headers.get("Last-Modified"):
                    new_meta["last_modified"] = response.headers["Last-Modified"]
                if new_meta:
                    self._set_url_meta(url, new_meta)

                return (_MODIFIED, data)
            except requests.RequestException as e:
                logger.error(f"Failed to fetch JSON from URL {url}: {e}")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON received from URL {url}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error while fetching JSON from URL {url}: {e}")
        logger.error("Failed to fetch JSON from all provided URLs.")
        return (_ERROR, None)

    def load_local_cache(self):
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
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
        status, cloud_data = self.fetch_json_from_url()

        if status == _NOT_MODIFIED:
            logger.info("本地配置已是最新 (HTTP 304)")
            return

        if status == _ERROR or cloud_data is None:
            logger.warning("获取云端配置失败，将继续使用本地配置")
            return

        cloud_last_modified = cloud_data.get('lastModified', 0)
        local_last_modified = self.local_data.get('lastModified', 0)
        if cloud_last_modified > local_last_modified:
            self.local_data = cloud_data
            from secure_write import write_json_restricted
            write_json_restricted(self.cache_file, cloud_data)
            logger.info("云端配置有更新，应用成功")
        else:
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
        包含 app_channel 以及 qrcode_channel_type、dst_jf_game_id、is_remember 等额外参数。"""
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

    def get_version_code_checkpoint(self,shortGameId):
        features = self.get_feature_by_game_id(shortGameId)
        if not features:
            return ""
        return features.get('version_code_checkpoint', "")
    
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

    def get_no_proxy_domains(self):
        """返回云端下发的 NO_PROXY 域名列表。

        每个域名应以 '.' 开头以支持子域名匹配。
        例如：['.gameyw.netease.com', '.ps.netease.com']
        """
        data = self.local_data or {}
        domains = data.get("no_proxy_domains") or []
        if not isinstance(domains, list):
            return []
        return domains
