# coding=UTF-8
import json
import logging
import os
import threading


class genv:
    global _list, _cachePath
    _list = {}
    _cachePath = "config.json"
    _cache_lock = threading.Lock()
    _cache_writes_disabled = False

    @staticmethod
    def read_cached():
        """Return settings, or None when the file could not be read."""
        try:
            with open(_cachePath, 'r', encoding='utf-8') as file:
                data = json.load(file)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            return {}
        except OSError as error:
            logging.getLogger(__name__).warning("读取配置失败（%s）", type(error).__name__)
            return None
        except ValueError:
            pass
        logging.getLogger(__name__).warning("config.json 格式错误，按空配置继续")
        return {}

    def set(key, value, cached=False, *, require_saved=False):
        if key == 'GLOB_LOGIN_UUID':
            from prefetch_context import in_prefetch
            if in_prefetch():
                return False
        if not require_saved:
            _list[key] = value
        if cached and isinstance(key, str) and isinstance(value, (str, int, float, bool, list, dict)):
            return genv.update_cached({key: value})
        return not require_saved

    @staticmethod
    def update_cached(values, *, remove=()):
        """Save related preferences together before publishing them in memory."""
        with genv._cache_lock:
            if genv._cache_writes_disabled:
                return False
            data = genv.read_cached()
            if data is None:
                return False
            if values or any(key in data for key in remove):
                data.update(values)
                for key in remove:
                    data.pop(key, None)
                from secure_write import write_json_restricted
                try:
                    write_json_restricted(_cachePath, data)
                except (OSError, ValueError, TypeError) as error:
                    logging.getLogger(__name__).warning("保存配置失败（%s）", type(error).__name__)
                    return False
            _list.update(values)
            for key in remove:
                _list.pop(key, None)
            return True

    @staticmethod
    def discard_cached(*keys):
        for key in keys:
            _list.pop(key, None)
        return genv.update_cached({}, remove=keys)

    @staticmethod
    def reset_cache():
        """Delete persistent tool state and prevent recreating it before restart."""
        with genv._cache_lock:
            cache_path = os.path.abspath(_cachePath)
            genv._cache_writes_disabled = True
            try:
                os.remove(cache_path)
            except FileNotFoundError:
                pass
            except Exception:
                genv._cache_writes_disabled = False
                raise
            return cache_path

    def get(key, default=None):
        if key in _list:
            return _list[key]
        return genv.get_from_file(key, default)

    def get_from_file(key, value):
        return (genv.read_cached() or {}).get(key, value)
