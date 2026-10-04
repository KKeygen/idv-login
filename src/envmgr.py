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

    def set(key, value, cached=False, *, require_saved=False):
        if key == 'GLOB_LOGIN_UUID':
            from prefetch_context import in_prefetch
            if in_prefetch():
                return
        if not require_saved:
            _list[key] = value
        if isinstance(value, (str, int, float, bool, list, dict)) and isinstance(key, str) and cached:
            with genv._cache_lock:
                if genv._cache_writes_disabled:
                    if require_saved:
                        raise OSError('Persistent configuration writes are disabled')
                    return
                try:
                    if os.path.exists(_cachePath):
                        with open(_cachePath, 'r', encoding='utf-8') as file:
                            data = json.load(file)
                    else:
                        data = {}
                    data[key] = value
                    from secure_write import write_json_restricted
                    write_json_restricted(_cachePath, data, atomic_required=require_saved)
                except Exception as error:
                    if require_saved:
                        raise OSError('Persistent configuration write failed') from error
                    # Legacy best-effort callers keep their contract, without printing values.
                    import traceback
                    print('Failed to cache key', key, type(error).__name__)
                    print(''.join(traceback.format_tb(error.__traceback__)))
                    return
        elif require_saved:
            raise TypeError('Persistent configuration requires a JSON value and cached=True')
        if require_saved:
            _list[key] = value  # Publish the new in-memory value only after persistence succeeds.

    @staticmethod
    def update_cached(values, *, remove=()):
        """Persist one related configuration change before publishing it in memory."""
        with genv._cache_lock:
            if genv._cache_writes_disabled:
                raise OSError('Persistent configuration writes are disabled')
            try:
                if os.path.exists(_cachePath):
                    with open(_cachePath, 'r', encoding='utf-8') as file:
                        data = json.load(file)
                else:
                    data = {}
                data.update(values)
                for key in remove:
                    data.pop(key, None)
                from secure_write import write_json_restricted
                write_json_restricted(_cachePath, data, atomic_required=True)
            except Exception as error:
                raise OSError('Persistent configuration write failed') from error
            _list.update(values)
            for key in remove:
                _list.pop(key, None)

    @staticmethod
    def discard_cached(*keys):
        """Retire obsolete cached fields without retaining their old values."""
        with genv._cache_lock:
            for key in keys:
                _list.pop(key, None)
            if genv._cache_writes_disabled or not os.path.exists(_cachePath):
                return
            with open(_cachePath, 'r', encoding='utf-8') as file:
                data = json.load(file)
            if not any(key in data for key in keys):
                return
            for key in keys:
                data.pop(key, None)
            from secure_write import write_json_restricted
            write_json_restricted(_cachePath, data)

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
