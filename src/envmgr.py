# coding=UTF-8
import json
import os
import threading


class genv:
    global _list, _cachePath
    _list = {}
    _cachePath = "config.json"
    _cache_lock = threading.Lock()
    _cache_writes_disabled = False

    def set(key, value, cached=False):
        _list[key] = value
        #if this object is json storeable
        if isinstance(value, (str, int, float, bool, list, dict)) and isinstance(key, str):
            if cached:
                with genv._cache_lock:
                    if genv._cache_writes_disabled:
                        return
                    try:
                        if os.path.exists(_cachePath):
                            with open(_cachePath, 'r', encoding='utf-8') as f:
                                data=json.load(f)
                        else:
                            data={}
                        data[key]=value
                        from secure_write import write_json_restricted
                        write_json_restricted(_cachePath, data)
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print("Failed to cache data",key,value)
                        pass

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
        else:
            try:
                with open(_cachePath, 'r', encoding='utf-8') as f:
                    data=json.load(f)
                    if key in data:
                        return data[key]
                    else:
                        return default
            except:
                return default

    def get_from_file(key,value):
        try:
            if os.path.exists(_cachePath):
                with open(_cachePath, 'r', encoding='utf-8') as f:
                    data=json.load(f)
                    if key in data:
                        return data[key]
                    else:
                        return value
            else:
                return value
        except:
            return value
