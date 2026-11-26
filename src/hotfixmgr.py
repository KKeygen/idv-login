# coding=UTF-8
"""热更新管理模块。

从 main.py 提取：负责热更新的探测、下载、应用、回滚及用户提示。

TODO: 支持创建新模块文件的热更新
当前实现仅支持替换已存在的模块文件。若需热更新一个原本不存在的新模块，
需要修改 _apply_one() 函数：
1. 当 target_path 不存在时，直接创建新文件（无需备份）
2. 在 _record_update 中标记 "is_new_module": True
3. 在回滚时检查此标记，若为新模块则删除文件而非恢复备份
"""

import importlib.abc
import importlib.util
import hashlib
import hmac
import re
import logging
import os
import py_compile
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

from envmgr import genv

logger = logging.getLogger("hotfixmgr")

# ------------------------------------------------------------------
# 模块级状态：用户退出倒计时期间需要被 handle_exit 读取
# ------------------------------------------------------------------
prompt_active = False
prompt_items: list = []
_import_hook_installed = False


class _HotfixOverlayFinder(importlib.abc.MetaPathFinder):
    def __init__(self, module_paths: Dict[str, str]):
        self.module_paths = dict(module_paths or {})

    def find_spec(self, fullname, path=None, target=None):
        source_path = self.module_paths.get(fullname)
        if not source_path or not os.path.exists(source_path):
            return None
        return importlib.util.spec_from_file_location(fullname, source_path)


# ------------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------------

def probe_cache_write_once() -> bool:
    """探测 genv.set(cached=True) 是否真的写入成功。

    由于 genv.set 在写入失败时只打印错误并吞掉异常，我们用 get_from_file 直接读文件校验。
    若探测失败，则必须跳过所有 hotfix 相关逻辑，以避免"写入失败导致状态无法落盘 -> 无限重启"。

    注意：此探测只应在"决定是否进入 hotfix 逻辑前"执行一次。
    """
    try:
        genv.set("hotfix_probed", True, True, require_saved=True)
        return bool(genv.get_from_file("hotfix_probed", False))
    except Exception:
        return False


def _make_id(item: dict) -> str:
    module_name = (item or {}).get("target_module", "")
    commit = (item or {}).get("target_commit", "")
    version = genv.get("VERSION", "")
    return (f"{version}|{module_name}@{commit}" if version else f"{module_name}@{commit}").strip("@")


def _get_records() -> dict:
    records = genv.get("hotfix_records", {})
    return records if isinstance(records, dict) else {}


def _set_records(records: dict, *, require_saved=False):
    if not isinstance(records, dict):
        records = {}
    genv.set("hotfix_records", records, True, require_saved=require_saved)


def _record_update(hotfix_id: str, patch: dict, *, require_saved=False):
    records = dict(_get_records())
    rec = records.get(hotfix_id, {})
    if not isinstance(rec, dict):
        rec = {}
    rec = dict(rec, **(patch or {}))
    records[hotfix_id] = rec
    _set_records(records, require_saved=require_saved)


def _file_sha256(path):
    with open(path, 'rb') as file:
        return hashlib.sha256(file.read()).hexdigest()


def _installed_file_matches(record):
    expected = record.get('installed_sha256')
    if not expected:
        return False
    if record.get('target_kind') == 'overlay_py':
        path = record.get('target_path')
    else:
        path, _ = _resolve_module_target_path(record.get('target_module', ''))
    try:
        return bool(path) and _file_sha256(path) == expected
    except OSError:
        return False


def _save_installed_record(hotfix_id, record, *, staged=False):
    try:
        record['installed_sha256'] = _file_sha256(record['target_path'])
        if staged:
            # Commit the receipt, crash-validation list, and deliberate skip together.
            records = dict(_get_records())
            records[hotfix_id] = record
            genv.update_cached({
                "hotfix_records": records,
                "hotfix_skipped": [hid for hid in _get_skipped_ids() if hid != hotfix_id],
                "hotfix_pending_validate": list(dict.fromkeys(_get_pending_ids() + [hotfix_id])),
            })
        else:
            _record_update(hotfix_id, record, require_saved=True)
    except Exception as error:
        return False, f"保存热更新状态失败: {error}"
    return True, ''


def _get_pending_ids() -> List[str]:
    pending = genv.get("hotfix_pending_validate", [])
    if isinstance(pending, list):
        return [str(x) for x in pending if str(x)]
    if isinstance(pending, str) and pending:
        return [pending]
    return []


def _set_pending_ids(pending_ids: List[str]):
    pending_ids = [str(x) for x in (pending_ids or []) if str(x)]
    genv.set("hotfix_pending_validate", pending_ids, True)


def _get_skipped_ids() -> List[str]:
    skipped = genv.get("hotfix_skipped", [])
    if isinstance(skipped, list):
        return [str(x) for x in skipped if str(x)]
    if isinstance(skipped, str) and skipped:
        return [skipped]
    return []


def _add_skipped(hotfix_ids: List[str]):
    existing = set(_get_skipped_ids())
    for hid in hotfix_ids or []:
        if hid:
            existing.add(str(hid))
    genv.set("hotfix_skipped", sorted(existing), True)


def _add_applied(hotfix_ids: List[str]):
    applied = genv.get("hotfix_applied", [])
    if not isinstance(applied, list):
        applied = []
    s = set(str(x) for x in applied if str(x))
    for hid in hotfix_ids or []:
        if hid:
            s.add(str(hid))
    genv.set("hotfix_applied", sorted(s), True)


def _is_macos_pyinstaller() -> bool:
    return sys.platform == "darwin" and bool(getattr(sys, "frozen", False))


def _safe_hotfix_dir_name(hotfix_id: str) -> str:
    safe = []
    for ch in str(hotfix_id):
        safe.append(ch if ch.isalnum() or ch in ("-", "_", ".") else "_")
    return "".join(safe).strip("._") or "hotfix"


def _overlay_root() -> str:
    return os.path.join(genv.get("FP_WORKDIR") or os.getcwd(), "hotfix_overlay")


def _get_active_overlay_paths() -> Dict[str, str]:
    records = _get_records()
    result = {}
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("target_kind") != "overlay_py":
            continue
        if rec.get("status") not in ("pending_validate", "applied"):
            continue
        module_name = str(rec.get("target_module") or "")
        target_path = str(rec.get("target_path") or "")
        if module_name and target_path and os.path.exists(target_path):
            result[module_name] = target_path
    return result


def install_import_hook():
    """启用外置 hotfix overlay。

    macOS PyInstaller 包内模块不可持久替换，因此这类 hotfix 以源码文件形式
    存放在用户工作目录，并通过精确模块名的 import hook 优先加载。
    """
    global _import_hook_installed
    if _import_hook_installed:
        return
    module_paths = _get_active_overlay_paths()
    if not module_paths:
        return
    sys.meta_path.insert(0, _HotfixOverlayFinder(module_paths))
    _import_hook_installed = True
    logger.info(f"【热更新】已启用外置模块覆盖: {', '.join(sorted(module_paths.keys()))}")


def _resolve_module_target_path(module_name: str) -> Tuple[Optional[str], str]:
    """返回 (target_path, kind)。kind ∈ {'py','pyc',''}。

    兼容两种运行环境：
    - 源码运行：src 里存在 .py
    - 嵌入解释器打包运行：pack.yaml 会把 .py 全部编译成同目录 .pyc 并删除 .py
    """
    if not module_name:
        return None, ""

    # 1) best-effort: try import spec origin
    try:
        spec = importlib.util.find_spec(module_name)
        if spec and spec.origin and os.path.exists(spec.origin):
            if spec.origin.endswith(".py"):
                return spec.origin, "py"
            if spec.origin.endswith(".pyc"):
                return spec.origin, "pyc"
    except Exception:
        pass

    # 2) derive from SCRIPT_DIR/module path
    try:
        base_dir = genv.get("SCRIPT_DIR") or os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(base_dir, *module_name.split("."))
        py_path = base + ".py"
        pyc_path = base + ".pyc"
        if os.path.exists(py_path):
            return py_path, "py"
        if os.path.exists(pyc_path):
            return pyc_path, "pyc"
        return py_path, "py"  # keep old error messages (likely "file not exists")
    except Exception:
        return None, ""


def _next_backup_path(target_path: str) -> str:
    base = target_path + ".bak"
    if not os.path.exists(base):
        return base
    idx = 1
    while True:
        cand = f"{base}.{idx}"
        if not os.path.exists(cand):
            return cand
        idx += 1


def _delete_pyc_for_source(source_path: str):
    try:
        pyc_path = importlib.util.cache_from_source(source_path)
        if pyc_path and os.path.exists(pyc_path):
            os.remove(pyc_path)
    except Exception:
        pass


def _compile_source(source_path: str) -> bool:
    try:
        py_compile.compile(source_path, doraise=True)
        return True
    except Exception as e:
        logger.error(f"【热更新】编译失败: {os.path.basename(source_path)}: {type(e).__name__}")
        return False


def _compile_to_pyc(source_path: str, pyc_path: str) -> Tuple[bool, str]:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(pyc_path)), exist_ok=True)
    except Exception:
        pass
    try:
        py_compile.compile(source_path, cfile=pyc_path, doraise=True)
        return True, ""
    except Exception as e:
        logger.error(f"【热更新】编译失败: {os.path.basename(source_path)}: {type(e).__name__}")
        return False, str(e)


def _download_text(url: str, fallbacks: List[str], expected_sha256: str = "") -> Tuple[bool, bytes, str]:
    from ssl_utils import should_verify_ssl
    import queue
    import threading
    results = queue.Queue()
    headers = {"Accept": "text/plain, */*"}

    def download(source_url):
        try:
            sess = requests.Session()
            sess.trust_env = False
            resp = sess.get(source_url, timeout=20, headers=headers, verify=should_verify_ssl())
            if resp.status_code == 200:
                if expected_sha256 and not hmac.compare_digest(hashlib.sha256(resp.content).hexdigest(), expected_sha256):
                    result = (False, b"", "源码 SHA-256 不匹配")
                else:
                    result = (True, resp.content, "")
            else:
                result = (False, b"", f"HTTP {resp.status_code}")
        except Exception as e:
            result = (False, b"", str(e))
        results.put(result)

    sources = [url] + fallbacks
    for source_url in sources:
        threading.Thread(target=download, args=(source_url,), daemon=True).start()
    last_error = ""
    for _ in sources:
        ok, content, last_error = results.get()
        if ok:
            return True, content, ""
    return False, b"", last_error


def _hotfix_source_hash(item, required=False):
    """Read the source hash only when this hotfix is about to be applied."""
    source = item.get('source_sha256', '')
    if not source and not required:
        return ''
    if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{64}', source):
        raise ValueError('热更新缺少有效的源码 SHA-256')
    return source


def _get_verified_hotfix(item, url, fallbacks, required):
    try:
        source_hash = _hotfix_source_hash(item, required)
    except ValueError as error:
        return False, b'', str(error)
    ok, content, error = _download_text(url, fallbacks, source_hash)
    if not ok:
        return False, b'', error
    return True, content, ''


def _build_remote_source_info(module_name: str, commit: str) -> Tuple[str, List[str]]:
    remote_rel = "src/" + "/".join(module_name.split(".")) + ".py"
    url = f"https://git.keygen.eu.org/keygen/idv-login/raw/commit/{commit}/{remote_rel}"
    fallbacks = [
        f"https://gitee.com/opguess/idv-login/raw/{commit}/{remote_rel}",
        f"https://cdn.jsdelivr.net/gh/KKeygen/idv-login@{commit}/{remote_rel}",
        f"https://raw.githubusercontent.com/KKeygen/idv-login/{commit}/{remote_rel}",
        f"https://gh-proxy.org/https://raw.githubusercontent.com/KKeygen/idv-login/{commit}/{remote_rel}",
        f"https://gh.monlor.com/https://raw.githubusercontent.com/KKeygen/idv-login/{commit}/{remote_rel}",
        f"https://hk.gh-proxy.org/https://raw.githubusercontent.com/KKeygen/idv-login/{commit}/{remote_rel}",
    ]
    return url, fallbacks


def _restart_debug_args(args, debug_mode):
    if debug_mode is None:
        return args
    result = [arg for arg in args if arg != '--debug']
    if debug_mode:
        result.append('--debug')
    return result


def restart_self(reason: str = "", *, debug_mode=None):
    try:
        if reason:
            logger.info(f"【热更新】即将重启: {reason}")
        # mark as intentional restart to avoid treating as crash
        genv.set("last_run_state", "restart", True)
        genv.set("last_run_state_ts", int(time.time()), True)
    except Exception:
        pass

    # 重启前执行清理（NRPT 规则、DNS 服务器、代理环境变量等）
    try:
        from main import handle_exit
        handle_exit()
    except Exception:
        pass
    if _is_macos_pyinstaller():
        try:
            genv.set("last_run_state", "restart", True)
            genv.set("last_run_state_ts", int(time.time()), True)
        except Exception:
            pass

    if getattr(sys, 'frozen', False):
        args = [sys.executable] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv

    args = _restart_debug_args(args, debug_mode)

    cwd = genv.get("SCRIPT_DIR") or os.getcwd()
    if _is_macos_pyinstaller():
        cwd = genv.get("FP_WORKDIR") or os.getcwd()
        try:
            os.chdir(cwd)
        except Exception:
            pass
        try:
            os.execv(sys.executable, args)
        except Exception:
            pass

    try:
        subprocess.Popen(args, cwd=cwd)
    except Exception:
        # last resort: try without cwd
        subprocess.Popen(args)
    os._exit(0)


# ------------------------------------------------------------------
# 应用 / 回滚
# ------------------------------------------------------------------

def _apply_one(item: dict, require_hashes: bool = False, *, staged_source=None) -> Tuple[bool, str]:
    """返回 (success, message)。成功时会落盘并编译。"""
    hotfix_id = _make_id(item)
    module_name = (item or {}).get("target_module", "")
    commit = (item or {}).get("target_commit", "")
    if not module_name or not commit:
        return False, "配置缺少 target_module 或 target_commit"

    if _is_macos_pyinstaller():
        return _apply_one_overlay(
            item, hotfix_id, module_name, commit, require_hashes,
            staged_source=staged_source,
        )

    target_path, target_kind = _resolve_module_target_path(module_name)
    if not target_path or not target_kind:
        return False, f"无法定位模块文件: {module_name}"
    if not os.path.exists(target_path):
        return False, f"模块文件不存在: {target_path}"
    if not os.access(target_path, os.W_OK):
        return False, f"模块文件不可写（可能无权限或在只读目录）: {target_path}"

    url, fallbacks = _build_remote_source_info(module_name, commit)
    if staged_source is None:
        ok, content, err = _get_verified_hotfix(item, url, fallbacks, require_hashes)
        if not ok:
            return False, f"下载失败: {url} ({err})"
    else:
        content = staged_source

    backup_path = _next_backup_path(target_path)

    if target_kind == "py":
        # write to temp near target
        tmp_path = target_path + ".hotfix.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入临时文件失败: {e}"

        # compile temp first to validate
        if not _compile_source(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return False, "新文件编译失败，已放弃应用"

        try:
            shutil.copy2(target_path, backup_path)
        except Exception as e:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return False, f"备份失败: {e}"

        try:
            shutil.move(tmp_path, target_path)
            _delete_pyc_for_source(target_path)
            if not _compile_source(target_path):
                raise RuntimeError("新文件落盘后编译失败")
        except Exception as e:
            try:
                if os.path.exists(backup_path):
                    shutil.copy2(backup_path, target_path)
                    _delete_pyc_for_source(target_path)
                    _compile_source(target_path)
            except Exception:
                pass
            return False, f"替换/编译失败，已回滚: {e}"

    elif target_kind == "pyc":
        # Embedded build (pack.yaml): src contains only .pyc
        tmp_source_dir = os.path.join(genv.get("FP_WORKDIR") or os.getcwd(), "hotfix_src")
        try:
            os.makedirs(tmp_source_dir, exist_ok=True)
        except Exception:
            pass
        tmp_source_path = os.path.join(
            tmp_source_dir,
            f"{module_name.replace('.', '_')}_{commit[:8]}.py",
        )
        tmp_pyc_path = target_path + ".hotfix.tmp"
        try:
            with open(tmp_source_path, "wb") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入临时源码失败: {e}"

        okc, errc = _compile_to_pyc(tmp_source_path, tmp_pyc_path)
        if not okc:
            try:
                os.remove(tmp_source_path)
            except Exception:
                pass
            try:
                if os.path.exists(tmp_pyc_path):
                    os.remove(tmp_pyc_path)
            except Exception:
                pass
            return False, f"新文件编译失败，已放弃应用: {errc}"

        try:
            shutil.copy2(target_path, backup_path)
        except Exception as e:
            try:
                os.remove(tmp_source_path)
            except Exception:
                pass
            try:
                os.remove(tmp_pyc_path)
            except Exception:
                pass
            return False, f"备份失败: {e}"

        try:
            os.replace(tmp_pyc_path, target_path)
        except Exception as e:
            try:
                if os.path.exists(backup_path):
                    shutil.copy2(backup_path, target_path)
            except Exception:
                pass
            return False, f"替换失败，已回滚: {e}"
        finally:
            try:
                os.remove(tmp_source_path)
            except Exception:
                pass

    else:
        return False, f"不支持的目标类型: {target_kind}"

    saved, error = _save_installed_record(
        hotfix_id,
        {
            "status": "pending_validate",
            "target_module": module_name,
            "target_commit": commit,
            "note": (item or {}).get("note", ""),
            "target_kind": target_kind,
            "target_path": os.path.abspath(target_path),
            "backup_path": os.path.abspath(backup_path),
            "applied_at": int(time.time()),
            "url": url,
        },
        staged=staged_source is not None,
    )
    if not saved:
        if staged_source is not None:
            try:
                shutil.copy2(backup_path, target_path)
                if target_kind == "py":
                    _delete_pyc_for_source(target_path)
                os.remove(backup_path)
            except OSError:
                logger.warning("【热更新】保存失败后无法还原模块: %s", module_name)
        return False, error
    return True, f"应用成功: {module_name} ({commit})"


def _apply_one_overlay(
    item: dict, hotfix_id: str, module_name: str, commit: str,
    require_hashes: bool = False, *, staged_source=None,
) -> Tuple[bool, str]:
    """为 macOS PyInstaller 应用外置源码覆盖，不修改包内文件。"""
    url, fallbacks = _build_remote_source_info(module_name, commit)
    if staged_source is None:
        ok, content, err = _get_verified_hotfix(item, url, fallbacks, require_hashes)
        if not ok:
            return False, f"下载失败: {url} ({err})"
    else:
        content = staged_source

    patch_root = os.path.join(_overlay_root(), _safe_hotfix_dir_name(hotfix_id))
    target_path = os.path.join(patch_root, *module_name.split(".")) + ".py"
    tmp_path = target_path + ".hotfix.tmp"
    previous_source = None
    try:
        if staged_source is not None and os.path.isfile(target_path):
            with open(target_path, "rb") as installed_source:
                previous_source = installed_source.read()
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(tmp_path, "wb") as f:
            f.write(content)
    except Exception as e:
        return False, f"写入外置热更新文件失败: {e}"

    if not _compile_source(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False, "新文件编译失败，已放弃应用"

    try:
        os.replace(tmp_path, target_path)
        _delete_pyc_for_source(target_path)
        if not _compile_source(target_path):
            raise RuntimeError("外置热更新文件落盘后编译失败")
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False, f"写入外置热更新文件失败: {e}"

    saved, error = _save_installed_record(
        hotfix_id,
        {
            "status": "pending_validate",
            "target_module": module_name,
            "target_commit": commit,
            "note": (item or {}).get("note", ""),
            "target_kind": "overlay_py",
            "target_path": os.path.abspath(target_path),
            "backup_path": "",
            "applied_at": int(time.time()),
            "url": url,
        },
        staged=staged_source is not None,
    )
    if not saved:
        if staged_source is not None:
            try:
                if previous_source is None:
                    os.remove(target_path)
                else:
                    with open(tmp_path, "wb") as old_source:
                        old_source.write(previous_source)
                    os.replace(tmp_path, target_path)
                _delete_pyc_for_source(target_path)
            except OSError:
                logger.warning("【热更新】保存失败后无法还原外置模块: %s", module_name)
        return False, error
    return True, f"应用成功: {module_name} ({commit}, macOS 外置覆盖)"


def _rollback_one(hotfix_id: str) -> Tuple[bool, str]:
    records = _get_records()
    rec = records.get(hotfix_id, {})
    if not isinstance(rec, dict):
        return False, "记录损坏"
    target_path = rec.get("target_path")
    if rec.get("target_kind") == "overlay_py":
        try:
            if target_path and os.path.exists(target_path):
                os.remove(target_path)
            _record_update(hotfix_id, {"status": "rolled_back", "rolled_back_at": int(time.time())})
            return True, "已禁用外置热更新"
        except Exception as e:
            return False, f"禁用外置热更新失败: {e}"

    backup_path = rec.get("backup_path")
    if not target_path or not backup_path:
        return False, "缺少 target_path/backup_path"
    if not os.path.exists(backup_path):
        return False, f"备份文件不存在: {backup_path}"
    try:
        shutil.copy2(backup_path, target_path)
        if str(target_path).endswith(".py"):
            _delete_pyc_for_source(target_path)
            _compile_source(target_path)
        _record_update(hotfix_id, {"status": "rolled_back", "rolled_back_at": int(time.time())})
        return True, "已回滚"
    except Exception as e:
        return False, f"回滚失败: {e}"


# ------------------------------------------------------------------
# 公共 API —— 由 main.py 调用
# ------------------------------------------------------------------

def pre_start_check_and_rollback_if_needed():
    """在本次运行开始前：根据上次运行状态，对待验证的 hotfix 做确认/回滚。"""
    prev_state = genv.get("last_run_state", "")
    pending_ids = _get_pending_ids()
    if not pending_ids:
        return

    # A reinstall may have replaced a pending module and its backup. Do not
    # restore the previous installation; the current cloud list decides later.
    records = _get_records()
    changed = [hid for hid in pending_ids
               if records.get(hid, {}).get('installed_sha256')
               and not _installed_file_matches(records[hid])]
    if changed:
        for hid in changed:
            _record_update(hid, {'status': 'needs_reapply'}, require_saved=True)
        pending_ids = [hid for hid in pending_ids if hid not in changed]
        _set_pending_ids(pending_ids)
    if not pending_ids:
        return

    # 若上次运行崩溃，则回滚所有待验证 hotfix，并标记为永久跳过
    if prev_state == "crash":
        for hid in list(pending_ids):
            ok, msg = _rollback_one(hid)
            print(f"【热更新】检测到上次热更新后崩溃，回滚 {hid}: {msg}")
            _record_update(hid, {"status": "skipped", "skip_reason": "rollback_after_crash"})
        _add_skipped(pending_ids)
        _set_pending_ids([])
        # 回滚完毕后，重启。
        restart_self("已回滚所有待验证热更新")
        return

    # 若上次正常退出，则确认 hotfix 生效
    if prev_state == "ok":
        for hid in list(pending_ids):
            _record_update(hid, {"status": "applied", "validated_at": int(time.time())})
        _add_applied(pending_ids)
        _set_pending_ids([])


def _hotfixes_for_version(cloudres, version):
    if not cloudres:
        return []
    try:
        hotfixes = cloudres.get_hotfixes()
    except Exception:
        return []
    applicable = []
    for item in hotfixes or []:
        if not isinstance(item, dict):
            continue
        need_versions = item.get("need_hotfix_version", [])
        if isinstance(need_versions, str):
            need_versions = [need_versions]
        if need_versions and not isinstance(need_versions, (list, tuple)):
            continue
        if version and need_versions and version not in [str(v) for v in need_versions]:
            continue
        applicable.append(item)
    return applicable


def manual_hotfix_unavailable(cloudres):
    if not genv.get("VERSION"):
        return "开发环境没有安装版本，无法应用热更新"
    if not getattr(cloudres, "signatures_required", False):
        return "当前没有经过签名验证的热更新目录"
    if not getattr(cloudres, "local_data", None):
        return "官方热更新目录尚不可用"
    return ""


def _staged_hotfix_path(hid):
    name = hashlib.sha256(hid.encode("utf-8")).hexdigest() + ".py"
    return os.path.join(genv.get("FP_WORKDIR") or os.getcwd(), "hotfix_staged", name)


def _read_verified_staged_source(item):
    hid = _make_id(item)
    try:
        source_hash = _hotfix_source_hash(item, required=True)
        with open(_staged_hotfix_path(hid), "rb") as source_file:
            source = source_file.read()
        if hmac.compare_digest(hashlib.sha256(source).hexdigest(), source_hash):
            return source
    except (OSError, ValueError):
        pass
    return None


def _stage_hotfix(item):
    from secure_write import write_file_restricted

    hid = _make_id(item)
    module = item["target_module"]
    commit = item["target_commit"]
    url, fallbacks = _build_remote_source_info(module, commit)
    ok, source, error = _get_verified_hotfix(item, url, fallbacks, True)
    if not ok:
        return False, error
    path = _staged_hotfix_path(hid)
    try:
        compile(source, f"{module}.py", "exec")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_file_restricted(path, source)
        _record_update(hid, {
            "status": "pending_restart", "target_module": module, "target_commit": commit,
        }, require_saved=True)
    except Exception as error:
        try:
            os.remove(path)
        except OSError:
            pass
        return False, f"保存待应用热更新失败: {error}"
    return True, ""


def _public_hotfix(item, status, record):
    module = item.get("target_module")
    commit = item.get("target_commit")
    if not (isinstance(module, str)
            and len(module) <= 160 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module)
            and isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)):
        return None
    note = item.get("note")
    entry = {
        "id": _make_id(item), "module": module, "commit": commit,
        "note": note[:1000] if isinstance(note, str) else "", "status": status,
        "applyable": status in ("pending", "skipped"),
    }
    if status in ("applied", "pending_validation"):
        applied_at = record.get("applied_at")
        if type(applied_at) is int:
            entry["applied_at"] = applied_at
    return entry


def _manual_hotfix_entries(cloudres, version):
    catalog = [item for item in _hotfixes_for_version(cloudres, version)
               if isinstance(item.get("target_module"), str)]
    latest = {item["target_module"]: _make_id(item) for item in catalog}
    items = {_make_id(item): item for item in catalog}
    records = _get_records()
    skipped = set(_get_skipped_ids())
    for hid, record in records.items():
        if (hid.startswith(f"{version}|") and isinstance(record, dict)
                and hid == _make_id(record) and hid not in items
                and record.get("status") in ("applied", "pending_validate")
                and _installed_file_matches(record)):
            items[hid] = record

    entries = []
    for hid, item in items.items():
        record = records.get(hid)
        if not isinstance(record, dict):
            record = {}
        stored = record.get("status")
        message = ""
        if (stored in ("applied", "pending_validate")
                and _make_id(record) == hid and _installed_file_matches(record)):
            state = "pending_validation" if stored == "pending_validate" else "applied"
        elif hid != latest.get(item.get("target_module")):
            state = "superseded"
        elif (stored == "pending_restart" and _make_id(record) == hid
              and _read_verified_staged_source(item) is not None):
            state = "restart_required"
            message = "已校验并准备，重启后应用"
        elif hid in skipped or stored in ("skipped", "rolled_back"):
            if stored != "rolled_back" and record.get("skip_reason") == "user_exit_during_countdown":
                state = "skipped"
                message = "上次已跳过，可手动应用"
            else:
                state = "blocked"
                message = "此热更新已回滚或永久停用"
        else:
            state = "pending"
            if stored == "apply_failed":
                message = "上次应用失败，可重试"
        if state in ("pending", "skipped"):
            try:
                _hotfix_source_hash(item, required=True)
            except ValueError:
                state, message = "blocked", "官方条目缺少有效的源码校验值"
        public = _public_hotfix(item, state, record)
        if public:
            if message:
                public["message"] = message
            entries.append((item, public))
    return entries


def manual_hotfix_status(cloudres):
    version = str(genv.get("VERSION") or "")
    reason = manual_hotfix_unavailable(cloudres)
    entries = _manual_hotfix_entries(None if reason else cloudres, version) if version else []
    items = [public for _, public in entries]
    return {
        "success": True, "version": version, "available": not reason,
        "available_reason": reason,
        "restart_required": any(item["status"] == "restart_required" for item in items),
        "applyable_count": sum(item["applyable"] for item in items),
        "items": items,
    }


def apply_pending_hotfixes(cloudres):
    reason = manual_hotfix_unavailable(cloudres)
    if reason:
        return {"success": False, "results": [], "restart_required": False, "error": reason}
    entries = _manual_hotfix_entries(cloudres, str(genv.get("VERSION") or ""))
    restart_required = any(item["status"] == "restart_required" for _, item in entries)
    if any(item["applyable"] for _, item in entries) and not probe_cache_write_once():
        return {"success": False, "results": [], "restart_required": restart_required,
                "error": "无法保存热更新状态，请检查配置目录写入权限"}

    failed = False
    results = []
    for item, public in entries:
        result = {key: public[key] for key in ("id", "module", "commit")}
        if public["status"] in ("blocked", "superseded"):
            result.update(status="skipped", message=public.get("message") or "已有较新的热更新")
        elif public["applyable"]:
            try:
                ok, detail = _stage_hotfix(item)
            except Exception:
                from logutil import safe_exception
                safe_exception(logger, "手动应用热更新出错")
                ok, detail = False, "应用过程异常"
            if ok:
                restart_required = True
                result.update(status="staged", message="已校验并准备，重启后应用")
            else:
                failed = True
                logger.warning("【热更新】%s: %s", public["id"], detail)
                if public["status"] != "skipped":
                    _record_update(public["id"], {"status": "apply_failed"})
                result.update(status="failed", message="热更新准备失败，请查看本地日志")
        else:
            continue
        results.append(result)
    return {"success": not failed, "results": results, "restart_required": restart_required}


def handle_if_needed(cloudres):
    """根据 CloudRes 下发配置，提示并应用热更新（必要时重启）。

    Parameters
    ----------
    cloudres : CloudRes
        云端资源管理实例（原 main.py 中的 m_cloudres）。
    """
    current_version = str(genv.get("VERSION", ""))
    skipped = set(_get_skipped_ids())
    records = _get_records()

    # A module replacement supersedes all earlier applicable entries for it.
    current_modules = {
        item.get("target_module"): item
        for item in _hotfixes_for_version(cloudres, current_version)
    }

    required = bool(getattr(cloudres, "signatures_required", False))
    candidates = []
    staged = []
    for item in current_modules.values():
        hid = _make_id(item)
        if not hid:
            continue
        record = records.get(hid, {}) or {}
        if (required and current_version and record.get("status") == "pending_restart"
                and _make_id(record) == hid
                and (hid not in skipped or record.get("skip_reason") == "user_exit_during_countdown")):
            source = _read_verified_staged_source(item)
            if source is not None:
                staged.append((item, source))
                continue
        if hid in skipped:
            continue
        if record.get("status") in ("applied", "pending_validate") and _installed_file_matches(record):
            continue
        candidates.append(item)

    if staged and _apply_startup_hotfixes(staged, required):
        return
    if not candidates:
        return

    # prompt user, allow quitting within 5 seconds to skip permanently
    global prompt_active, prompt_items
    prompt_active = True
    prompt_items = candidates

    print("\n================ 热更新提示 ================")
    if _is_macos_pyinstaller():
        print("检测到需要对当前版本进行热更新，将下载并启用外置模块覆盖。")
    else:
        print("检测到需要对当前版本进行热更新，将下载并替换本地模块文件。")
    print("如果你不想热更新，请在 5 秒内直接退出程序（关闭窗口/Ctrl+C）。")
    for idx, item in enumerate(candidates, 1):
        module_name = item.get("target_module", "")
        commit = item.get("target_commit", "")
        note = item.get("note", "")
        print(f"- [{idx}] 模块: {module_name}")
        print(f"      云端版本: {commit}")
        if note:
            print(f"      更新原因: {note}")
    print("===========================================\n")

    for i in range(5, 0, -1):
        print(f"【热更新】{i}s 后自动热更新... (现在退出即永久跳过)")
        time.sleep(1)

    # accepted
    prompt_active = False
    prompt_items = []

    _apply_startup_hotfixes(((item, None) for item in candidates), required)


def _apply_startup_hotfixes(items, require_hashes):
    applied_ids = []
    for item, staged_source in items:
        hid = _make_id(item)
        record = _get_records().get(hid, {})
        if (require_hashes and staged_source is None
                and isinstance(record, dict) and record.get("status") == "apply_failed"):
            # A failed restart may have left the verified download intact.
            staged_source = _read_verified_staged_source(item)
        _record_update(hid, {"status": "applying", "ts": int(time.time())})
        if staged_source is None:
            ok, message = _apply_one(item, require_hashes=require_hashes)
        else:
            ok, message = _apply_one(item, require_hashes=True, staged_source=staged_source)
        print(f"【热更新】{hid}: {message}")
        if ok:
            applied_ids.append(hid)
            if staged_source is not None:
                try:
                    os.remove(_staged_hotfix_path(hid))
                except OSError:
                    pass
        elif require_hashes:
            # A missing or invalid mirror can be transient, so allow a later retry.
            _record_update(hid, {"status": "apply_failed", "error": message})
        else:
            _record_update(hid, {"status": "skipped", "skip_reason": "apply_failed", "error": message})
            _add_skipped([hid])

    if applied_ids:
        pending = list(dict.fromkeys(_get_pending_ids() + applied_ids))
        if pending != _get_pending_ids():
            _set_pending_ids(pending)
        restart_self("已应用热更新")
    return bool(applied_ids)


def handle_exit_skip_if_active():
    """在 handle_exit 中调用：若用户在倒计时期间退出，永久跳过当前 hotfix。"""
    if not prompt_active or not prompt_items:
        return
    try:
        ids = [_make_id(i) for i in prompt_items if isinstance(i, dict)]
        ids = [x for x in ids if x]
        if ids:
            _add_skipped(ids)
            for it in prompt_items:
                if isinstance(it, dict):
                    _record_update(
                        _make_id(it),
                        {
                            "status": "skipped",
                            "skip_reason": "user_exit_during_countdown",
                            "target_module": it.get("target_module", ""),
                            "target_commit": it.get("target_commit", ""),
                            "note": it.get("note", ""),
                            "skipped_at": int(time.time()),
                        },
                    )
            print("【热更新】已记录：本次热更新被用户跳过（永久）。")
    except Exception:
        pass
