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

import sys
import argparse
import shutil
import glob
import base64
import subprocess
import time
import importlib.util
import py_compile
from typing import Optional, Tuple, List, Dict, Any


def parse_command_line_args():
    """解析命令行参数"""
    arg_parser = argparse.ArgumentParser(description="第五人格登陆助手")
    arg_parser.add_argument('--mitm', action='store_true', help='直接使用备用模式 (mitmproxy)')
    arg_parser.add_argument('--download', type=str, default="", help='下载任务文件绝对路径')
    return arg_parser.parse_args()


CLI_ARGS = parse_command_line_args()
if not CLI_ARGS.download:
    from gevent import monkey
    monkey.patch_all()


import socket
import os
import sys
import ctypes
import atexit
import requests
import requests.packages
import json
import random
import string

script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(script_dir)
from envmgr import genv


# Global variable declarations
m_certmgr = None
m_hostmgr = None 
m_proxy = None
m_cloudres=None
logger = None # Will be initialized in __main__
_console_ctrl_handler = None

# hotfix prompt state (used by exit hook to persist “skip” when user quits within countdown)
_hotfix_prompt_active = False
_hotfix_prompt_items = []


def _hotfix_probe_cache_write_once() -> bool:
    """探测 genv.set(cached=True) 是否真的写入成功。

    由于 genv.set 在写入失败时只打印错误并吞掉异常，我们用 get_from_file 直接读文件校验。
    若探测失败，则必须跳过所有 hotfix 相关逻辑，以避免“写入失败导致状态无法落盘 -> 无限重启”。

    注意：此探测只应在“决定是否进入 hotfix 逻辑前”执行一次。
    """
    try:
        genv.set("hotfix_probed", True, True)
        return bool(genv.get_from_file("hotfix_probed", False))
    except Exception:
        return False


def _hotfix_make_id(item: dict) -> str:
    module_name = (item or {}).get("target_module", "")
    commit = (item or {}).get("target_commit", "")
    version = genv.get("VERSION", "")
    return (f"{version}|{module_name}@{commit}" if version else f"{module_name}@{commit}").strip("@")


def _hotfix_get_records() -> dict:
    records = genv.get("hotfix_records", {})
    return records if isinstance(records, dict) else {}


def _hotfix_set_records(records: dict):
    if not isinstance(records, dict):
        records = {}
    genv.set("hotfix_records", records, True)


def _hotfix_record_update(hotfix_id: str, patch: dict):
    records = _hotfix_get_records()
    rec = records.get(hotfix_id, {})
    if not isinstance(rec, dict):
        rec = {}
    rec.update(patch or {})
    records[hotfix_id] = rec
    _hotfix_set_records(records)


def _hotfix_get_pending_ids() -> List[str]:
    pending = genv.get("hotfix_pending_validate", [])
    if isinstance(pending, list):
        return [str(x) for x in pending if str(x)]
    if isinstance(pending, str) and pending:
        return [pending]
    return []


def _hotfix_set_pending_ids(pending_ids: List[str]):
    pending_ids = [str(x) for x in (pending_ids or []) if str(x)]
    genv.set("hotfix_pending_validate", pending_ids, True)


def _hotfix_get_skipped_ids() -> List[str]:
    skipped = genv.get("hotfix_skipped", [])
    if isinstance(skipped, list):
        return [str(x) for x in skipped if str(x)]
    if isinstance(skipped, str) and skipped:
        return [skipped]
    return []


def _hotfix_add_skipped(hotfix_ids: List[str]):
    existing = set(_hotfix_get_skipped_ids())
    for hid in hotfix_ids or []:
        if hid:
            existing.add(str(hid))
    genv.set("hotfix_skipped", sorted(existing), True)


def _hotfix_add_applied(hotfix_ids: List[str]):
    applied = genv.get("hotfix_applied", [])
    if not isinstance(applied, list):
        applied = []
    s = set(str(x) for x in applied if str(x))
    for hid in hotfix_ids or []:
        if hid:
            s.add(str(hid))
    genv.set("hotfix_applied", sorted(s), True)


def _hotfix_resolve_module_target_path(module_name: str) -> Tuple[Optional[str], str]:
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


def _hotfix_next_backup_path(target_path: str) -> str:
    base = target_path + ".bak"
    if not os.path.exists(base):
        return base
    idx = 1
    while True:
        cand = f"{base}.{idx}"
        if not os.path.exists(cand):
            return cand
        idx += 1


def _hotfix_delete_pyc_for_source(source_path: str):
    try:
        pyc_path = importlib.util.cache_from_source(source_path)
        if pyc_path and os.path.exists(pyc_path):
            os.remove(pyc_path)
    except Exception:
        pass


def _hotfix_compile_source(source_path: str) -> bool:
    try:
        py_compile.compile(source_path, doraise=True)
        return True
    except Exception as e:
        if logger:
            logger.error(f"【热更新】编译失败: {source_path}: {e}")
        else:
            print(f"【热更新】编译失败: {source_path}: {e}")
        return False


def _hotfix_compile_to_pyc(source_path: str, pyc_path: str) -> Tuple[bool, str]:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(pyc_path)), exist_ok=True)
    except Exception:
        pass
    try:
        py_compile.compile(source_path, cfile=pyc_path, doraise=True)
        return True, ""
    except Exception as e:
        if logger:
            logger.error(f"【热更新】编译失败: {source_path} -> {pyc_path}: {e}")
        else:
            print(f"【热更新】编译失败: {source_path} -> {pyc_path}: {e}")
        return False, str(e)


def _hotfix_download_text(url: str, fallbacks: List[str]) -> Tuple[bool, bytes, str]:
    from ssl_utils import should_verify_ssl
    sess = requests.Session()
    sess.trust_env = False
    headers = {"Accept": "text/plain, */*"}
    for source_url in [url] + fallbacks:
        try:
            resp = sess.get(source_url, timeout=20, headers=headers, verify=should_verify_ssl())
            if resp.status_code == 200:
                return True, resp.content, ""
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_error = str(e)
    return False, b"", last_error


def _hotfix_restart_self(reason: str = ""):
    try:
        if reason:
            if logger:
                logger.info(f"【热更新】即将重启: {reason}")
            else:
                print(f"【热更新】即将重启: {reason}")
        # mark as intentional restart to avoid treating as crash
        genv.set("last_run_state", "restart", True)
        genv.set("last_run_state_ts", int(time.time()), True)
    except Exception:
        pass

    if getattr(sys, 'frozen', False):
        args = [sys.executable] + sys.argv[1:]
    else:
        args = [sys.executable] + sys.argv

    try:
        subprocess.Popen(args, cwd=genv.get("SCRIPT_DIR") or os.getcwd())
    except Exception:
        # last resort: try without cwd
        subprocess.Popen(args)
    os._exit(0)


def _hotfix_apply_one(item: dict) -> Tuple[bool, str]:
    """返回 (success, message)。成功时会落盘并编译。"""
    hotfix_id = _hotfix_make_id(item)
    module_name = (item or {}).get("target_module", "")
    commit = (item or {}).get("target_commit", "")
    if not module_name or not commit:
        return False, "配置缺少 target_module 或 target_commit"

    target_path, target_kind = _hotfix_resolve_module_target_path(module_name)
    if not target_path or not target_kind:
        return False, f"无法定位模块文件: {module_name}"
    if not os.path.exists(target_path):
        return False, f"模块文件不存在: {target_path}"
    if not os.access(target_path, os.W_OK):
        return False, f"模块文件不可写（可能无权限或在只读目录）: {target_path}"

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
    ok, content, err = _hotfix_download_text(url,fallbacks)
    if not ok:
        return False, f"下载失败: {url} ({err})"

    backup_path = _hotfix_next_backup_path(target_path)

    if target_kind == "py":
        # write to temp near target
        tmp_path = target_path + ".hotfix.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入临时文件失败: {e}"

        # compile temp first to validate
        if not _hotfix_compile_source(tmp_path):
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
            _hotfix_delete_pyc_for_source(target_path)
            if not _hotfix_compile_source(target_path):
                raise RuntimeError("新文件落盘后编译失败")
        except Exception as e:
            try:
                if os.path.exists(backup_path):
                    shutil.copy2(backup_path, target_path)
                    _hotfix_delete_pyc_for_source(target_path)
                    _hotfix_compile_source(target_path)
            except Exception:
                pass
            return False, f"替换/编译失败，已回滚: {e}"

    elif target_kind == "pyc":
        # Embedded build (pack.yaml): src contains only .pyc; compile downloaded source into a .pyc then replace
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

        okc, errc = _hotfix_compile_to_pyc(tmp_source_path, tmp_pyc_path)
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

    _hotfix_record_update(
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
    )
    return True, f"应用成功: {module_name} ({commit})"


def _hotfix_rollback_one(hotfix_id: str) -> Tuple[bool, str]:
    records = _hotfix_get_records()
    rec = records.get(hotfix_id, {})
    if not isinstance(rec, dict):
        return False, "记录损坏"
    target_path = rec.get("target_path")
    backup_path = rec.get("backup_path")
    if not target_path or not backup_path:
        return False, "缺少 target_path/backup_path"
    if not os.path.exists(backup_path):
        return False, f"备份文件不存在: {backup_path}"
    try:
        shutil.copy2(backup_path, target_path)
        if str(target_path).endswith(".py"):
            _hotfix_delete_pyc_for_source(target_path)
            _hotfix_compile_source(target_path)
        _hotfix_record_update(hotfix_id, {"status": "rolled_back", "rolled_back_at": int(time.time())})
        return True, "已回滚"
    except Exception as e:
        return False, f"回滚失败: {e}"


def hotfix_pre_start_check_and_rollback_if_needed():
    """在本次运行开始前：根据上次运行状态，对待验证的 hotfix 做确认/回滚。"""
    prev_state = genv.get("last_run_state", "")
    pending_ids = _hotfix_get_pending_ids()
    if not pending_ids:
        return

    # 若上次运行崩溃，则回滚所有待验证 hotfix，并标记为永久跳过
    if prev_state == "crash":
        for hid in list(pending_ids):
            ok, msg = _hotfix_rollback_one(hid)
            print(f"【热更新】检测到上次热更新后崩溃，回滚 {hid}: {msg}")
            _hotfix_record_update(hid, {"status": "skipped", "skip_reason": "rollback_after_crash"})
        _hotfix_add_skipped(pending_ids)
        _hotfix_set_pending_ids([])
        #回滚完毕后，重启。
        _hotfix_restart_self("已回滚所有待验证热更新")
        return

    # 若上次正常退出，则确认 hotfix 生效
    if prev_state == "ok":
        for hid in list(pending_ids):
            _hotfix_record_update(hid, {"status": "applied", "validated_at": int(time.time())})
        _hotfix_add_applied(pending_ids)
        _hotfix_set_pending_ids([])


def handle_hotfix_if_needed():
    """根据 CloudRes 下发配置，提示并应用热更新（必要时重启）。"""
    if not m_cloudres:
        return
    try:
        hotfixes = m_cloudres.get_hotfixes()
    except Exception:
        hotfixes = []
    if not hotfixes:
        return

    current_version = str(genv.get("VERSION", ""))
    skipped = set(_hotfix_get_skipped_ids())
    records = _hotfix_get_records()

    candidates = []
    for item in hotfixes:
        if not isinstance(item, dict):
            continue
        need_versions = item.get("need_hotfix_version", [])
        if isinstance(need_versions, str):
            need_versions = [need_versions]
        if current_version and need_versions and current_version not in [str(v) for v in need_versions]:
            continue
        hid = _hotfix_make_id(item)
        if not hid:
            continue
        if hid in skipped:
            continue
        status = (records.get(hid, {}) or {}).get("status")
        if status in ("applied", "pending_validate"):
            continue
        candidates.append(item)

    if not candidates:
        return

    # prompt user, allow quitting within 5 seconds to skip permanently
    global _hotfix_prompt_active, _hotfix_prompt_items
    _hotfix_prompt_active = True
    _hotfix_prompt_items = candidates

    print("\n================ 热更新提示 ================")
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
    _hotfix_prompt_active = False
    _hotfix_prompt_items = []

    applied_ids = []
    success_any = False
    for item in candidates:
        hid = _hotfix_make_id(item)
        _hotfix_record_update(hid, {"status": "applying", "ts": int(time.time())})
        ok, msg = _hotfix_apply_one(item)
        print(f"【热更新】{hid}: {msg}")
        if ok:
            success_any = True
            applied_ids.append(hid)
        else:
            _hotfix_record_update(hid, {"status": "skipped", "skip_reason": "apply_failed", "error": msg})
            _hotfix_add_skipped([hid])

    if success_any:
        _hotfix_set_pending_ids(applied_ids)
        _hotfix_restart_self("已应用热更新")


def get_computer_name():
    try:
        # 获取计算机名
        computer_name = socket.gethostname()
        # 确保计算机名编码为 UTF-8
        computer_name_utf8 = computer_name.encode('utf-8').decode('utf-8')
        return computer_name_utf8
    except Exception as e:
        logger.exception(f"获取计算机名时发生异常: {e}")
        return None

def handle_exit():
    # Assuming logger is initialized by the time this is called via atexit or signal
    # hotfix: if user quits during countdown, persist skip permanently
    global _hotfix_prompt_active, _hotfix_prompt_items
    try:
        if _hotfix_prompt_active and _hotfix_prompt_items:
            ids = [_hotfix_make_id(i) for i in _hotfix_prompt_items if isinstance(i, dict)]
            ids = [x for x in ids if x]
            if ids:
                _hotfix_add_skipped(ids)
                for it in _hotfix_prompt_items:
                    if isinstance(it, dict):
                        _hotfix_record_update(
                            _hotfix_make_id(it),
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

    # mark clean exit unless already marked as crash
    try:
        if genv.get("last_run_state", "") != "crash":
            genv.set("last_run_state", "ok", True)
            genv.set("last_run_state_ts", int(time.time()), True)
    except Exception:
        pass

    if logger:
        logger.info("程序关闭，正在清理！")
    else:
        print("程序关闭，正在清理！ (logger 未初始化)")

    if not genv.get("USING_BACKUP_VER", False) and m_hostmgr: # m_hostmgr is global
        if logger: logger.info("正在清理 hosts...")
        else: print("正在清理 hosts...")
        m_hostmgr.remove(genv.get("DOMAIN_TARGET"))
        m_hostmgr.remove(genv.get("DOMAIN_TARGET_OVERSEA"))
    
    if genv.get("USING_BACKUP_VER", False):
        backup_mgr = genv.get("backupVerMgr")
        if backup_mgr:
            if logger: logger.info("正在停止 mitmproxy...")
            else: print("正在停止 mitmproxy...")
            backup_mgr.stop_mitmproxy()
    from httpdnsblocker import HttpDNSBlocker
    HttpDNSBlocker().unblock_all()
    print("再见!")

def handle_update():

    # 延后导入：避免在工作目录切换前导入 cloudRes/logutil 导致 log.txt 写入启动目录（如 bat 文件夹）
    from cloudRes import CloudRes

    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import QMessageBox, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextBrowser, QPushButton, QToolButton, QMenu, QSizePolicy, QApplication
    from PyQt6.QtCore import Qt
    
    ignoredVersions=genv.get("ignoredVersions",[])
    #ignoredVersions=[]
    if "dev" in genv.get("VERSION","v5.4.0").lower() or "main" in genv.get("VERSION","v5.4.0").lower():
        print("【在线更新】当前版本为开发版本，更新功能已关闭。")
        return
    if genv.get("CLOUD_VERSION")==genv.get("VERSION"):
        print("【在线更新】当前版本已是最新版本。")
        return
    elif not genv.get("CLOUD_VERSION") in ignoredVersions:
        print(f"【在线更新】工具有新版本：{genv.get('CLOUD_VERSION')}。")
        details=CloudRes().get_detail_html()
        
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
        dialog = QDialog()
        dialog.setWindowTitle(f"新版本！")
        dialog.setSizeGripEnabled(True)
        dialog_layout = QVBoxLayout(dialog)
        title_label = QLabel(f"{genv.get('VERSION')} -> {genv.get('CLOUD_VERSION')}")
        dialog_layout.addWidget(title_label)
        formatted_details = details.replace('\n', '<br>')
        details_view = QTextBrowser()
        details_view.setHtml(formatted_details)
        details_view.setOpenExternalLinks(True)
        details_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        dialog_layout.addWidget(details_view, 1)
        screen = QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            dialog.resize(int(available.width() * 0.4), int(available.height() * 0.6))
            dialog.setMaximumSize(int(available.width() * 0.8), int(available.height() * 0.8))
        dialog.setMinimumSize(520, 420)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        yes_btn = QPushButton("现在更新")
        
        no_btn = QToolButton()
        no_btn.setText("下次提醒我")
        no_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        no_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        no_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        no_btn.setMinimumHeight(23)
        if CloudRes().is_update_critical():
            pass
        else:
            ignore_action = QAction("忽略此版本", dialog)
            def on_ignore(checked=False):
                print(f"【在线更新】用户选择忽略版本{genv.get('CLOUD_VERSION')}。")
                ignoredVersions.append(genv.get("CLOUD_VERSION"))
                genv.set("ignoredVersions",ignoredVersions,True)
                dialog.reject()

            ignore_action.triggered.connect(on_ignore)
        
            menu = QMenu()
            menu.addAction(ignore_action)
            no_btn.setMenu(menu)
        
        no_btn.clicked.connect(dialog.reject)
        yes_btn.clicked.connect(dialog.accept)
        button_layout.addWidget(no_btn)
        button_layout.addWidget(yes_btn)
        dialog_layout.addLayout(button_layout)
        
        result = dialog.exec()
       
        def go_to_update():
            url=CloudRes().get_downloadUrl()
            import webbrowser
            webbrowser.open(url)
            QMessageBox.information(None, "提示", "请按照打开的网页指引下载更新。\n程序将自动退出。")
            sys.exit(0)
        if result == QDialog.DialogCode.Accepted:
            go_to_update()
        else:
            if CloudRes().is_update_critical():
                info_box = QMessageBox()
                info_box.setWindowTitle("提示")
                info_box.setText("本次更新为安全相关更新，为了保护你的账号安全，请及时更新。")
                update_btn = info_box.addButton("现在更新", QMessageBox.ButtonRole.AcceptRole)
                remind_btn = info_box.addButton("我知道了，下次提醒我", QMessageBox.ButtonRole.RejectRole)
                info_box.setDefaultButton(update_btn)
                info_box.exec()
                if info_box.clickedButton() == update_btn:
                    go_to_update()
                else:
                    # 用户选择下次提醒，什么都不做
                    pass
            return
    else:
        print(f"【在线更新】检测到新版本{genv.get('CLOUD_VERSION')}，但已被用户永久跳过。")
        return

def ctrl_handler(ctrl_type):
    if ctrl_type in (0, 1, 2, 5, 6):
        handle_exit()
        return False
    return True


def initialize():
    # if we don't have enough privileges, relaunch as administrator
    if sys.platform=='win32':
        if ctypes.windll.shell32.IsUserAnAdmin() == 0:
            # 获取正确的可执行文件路径
            if getattr(sys, 'frozen', False):
                # 如果是PyInstaller打包的exe文件，使用sys.argv[0]
                executable = sys.argv[0]
            else:
                # 如果是Python脚本，使用sys.executable
                executable = sys.executable
            
            # 解决含空格的目录，准备命令行参数
            # 对于exe文件，不需要包含sys.argv[0]在参数中
            if getattr(sys, 'frozen', False):
                # exe文件：只传递从argv[1]开始的参数
                args = sys.argv[1:] if len(sys.argv) > 1 else []
                argvs = [f'"{arg}"' for arg in args]
            else:
                # Python脚本：需要传递完整的argv
                argvs = [f'"{i}"' for i in sys.argv]
            
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", executable, " ".join(argvs), script_dir, 1
            )
            sys.exit()
    else:
        #check if we have root privileges
        if os.geteuid() != 0:
            print("sudo required.")
            sys.exit(1)
    #全局变量声明
    global m_certmgr, m_hostmgr, m_proxy, m_cloudres

        # initialize workpath
    if not os.path.exists(genv.get("FP_WORKDIR")):
        os.mkdir(genv.get("FP_WORKDIR"))
    os.chdir(os.path.join(genv.get("FP_WORKDIR")))


    # initialize the global vars at first
    genv.set("DOMAIN_TARGET", "service.mkey.163.com")
    genv.set("DOMAIN_TARGET_OVERSEA","sdk-os.mpsdk.easebar.com")
    genv.set("FP_FAKE_DEVICE", os.path.join(genv.get("FP_WORKDIR"), "fakeDevice.json"))
    genv.set("FP_WEBCERT", os.path.join(genv.get("FP_WORKDIR"), "domain_cert_4.pem"))
    genv.set("FP_WEBKEY", os.path.join(genv.get("FP_WORKDIR"), "domain_key_4.pem"))
    genv.set("FP_CACERT", os.path.join(genv.get("FP_WORKDIR"), "root_ca_oversea_0213.pem"))
    genv.set("FP_CHANNEL_RECORD", os.path.join(genv.get("FP_WORKDIR"), "channels.json"))
    genv.set("CHANNEL_ACCOUNT_SELECTED", "")
    genv.set("GLOB_LOGIN_PROFILE_PATH", os.path.join(genv.get("FP_WORKDIR"), "profile"))
    genv.set("GLOB_LOGIN_CACHE_PATH", os.path.join(genv.get("FP_WORKDIR"), "cache"))
    genv.set("SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))
    CloudPaths = [
        "https://git.keygen.eu.org/keygen/idv-login/raw/branch/main/assets/cloudRes.json",
        "https://gitee.com/opguess/idv-login/raw/main/assets/cloudRes.json",
        "https://cdn.jsdelivr.net/gh/KKeygen/idv-login@main/assets/cloudRes.json",
        "https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/assets/cloudRes.json",
        "https://gh-proxy.org/https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/assets/cloudRes.json",
        "https://gh.monlor.com/https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/assets/cloudRes.json",
        "https://hk.gh-proxy.org/https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/assets/cloudRes.json",
    ]

    # 无版本信息时：优先使用本地 assets\cloudRes.json；仅当本地不存在时才使用云端
    try:
        version = genv.get("VERSION", "")
        version_missing = (not isinstance(version, str)) or (not version.strip())
        if version_missing:
            local_cloudres_path = os.path.normpath(
                os.path.join(genv.get("SCRIPT_DIR") or script_dir, "..", "assets", "cloudRes.json")
            )
            if os.path.exists(local_cloudres_path):
                try:
                    with open(local_cloudres_path, "r", encoding="utf-8") as f:
                        local_cloudres = json.load(f)
                    cache_path = os.path.join(genv.get("FP_WORKDIR"), "cache.json")
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(local_cloudres, f, ensure_ascii=False, indent=4)
                    CloudPaths = []
                    print("【云端配置】未检测到版本信息，已使用本地 assets\\cloudRes.json。")
                except Exception as e:
                    print(f"【云端配置】读取本地 assets\\cloudRes.json 失败，将继续使用云端配置: {e}")
            else:
                print("【云端配置】未检测到版本信息，且本地 assets\\cloudRes.json 不存在，将使用云端配置。")
    except Exception:
        pass

    # handle exit
    atexit.register(handle_exit)

    from cloudRes import CloudRes
    m_cloudres=CloudRes(CloudPaths,genv.get('FP_WORKDIR'))
    m_cloudres.update_cache_if_needed()
    genv.set("CLOUD_RES",m_cloudres)
    genv.set("CLOUD_VERSION",m_cloudres.get_version())
    genv.set("CLOUD_ANNO",m_cloudres.get_announcement())

    # disable warnings for requests
    requests.packages.urllib3.disable_warnings()



    if not os.path.exists(genv.get("FP_FAKE_DEVICE")):
        udid = "".join(random.choices(string.hexdigits, k=16))
        sdkDevice = {
            "device_model": "M2102K1AC",
            "os_name": "android",
            "os_ver": "12",
            "udid": udid,
            "app_ver": "157",
            "imei": "".join(random.choices(string.digits, k=15)),
            "country_code": "CN",
            "is_emulator": 0,
            "is_root": 0,
            "oaid": "",
        }
        with open(genv.get("FP_FAKE_DEVICE"), "w") as f:
            json.dump(sdkDevice, f)
    else:
        with open(genv.get("FP_FAKE_DEVICE"), "r") as f:
            sdkDevice = json.load(f)
    genv.set("FAKE_DEVICE", sdkDevice)
    
    if not os.path.exists(genv.get("GLOB_LOGIN_PROFILE_PATH")):
        os.makedirs(genv.get("GLOB_LOGIN_PROFILE_PATH"))
    
    from certmgr import certmgr
    
    
    from channelmgr import ChannelManager
    m_certmgr = certmgr()
    if sys.platform=='darwin':
        from macProxyMgr import macProxyMgr
        m_proxy = macProxyMgr()
    else:
        from proxymgr import proxymgr
        m_proxy = proxymgr()
    genv.set("CHANNELS_HELPER", ChannelManager())
    #blocks httpdns ips
    from httpdnsblocker import HttpDNSBlocker
    
    # 检查全局HTTPDNS屏蔽设置，默认启用
    httpdns_enabled = genv.get("httpdns_blocking_enabled", False)
    
    if httpdns_enabled:
        HttpDNSBlocker().apply_blocking()
        logger.info(f"HTTPDNS屏蔽已启用，封锁了{len(HttpDNSBlocker().blocked)}个HTTPDNS IP")
    else:
        # 确保之前的屏蔽规则被清除
        HttpDNSBlocker().unblock_all()

    logger.info("初始化内置浏览器")
    os.environ.pop('QT_QPA_PLATFORM_PLUGIN_PATH', None)
    os.environ.pop('QT_PLUGIN_PATH', None)
    os.environ.pop('LD_LIBRARY_PATH', None)   # Linux/macOS 下动态库搜索路径
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineCore import QWebEngineUrlScheme
    from PyQt6.QtNetwork import QNetworkProxyFactory
    argv = sys.argv if sys.argv else ["idv-login"]
    genv.set("APP",QApplication(argv))
    QWebEngineUrlScheme.registerScheme(QWebEngineUrlScheme("hms".encode()))
    QNetworkProxyFactory.setUseSystemConfiguration(False)

    if genv.get(f"{genv.get('VERSION')}_first_use",True):
        # 记录安装根目录
        record_install_root()
        #该版本首次使用会弹出教程
        #import webbrowser
        #url=CloudRes().get_guideUrl()
        #genv.set("httpdns_blocking_enabled",False,True)
        #webbrowser.open(url)
        genv.set(f"{genv.get('VERSION')}_first_use",False,True)
        #from gamemgr import GameManager
        #try:
        #    game_mgr = GameManager()
        #    for game in game_mgr.games.values():
        #        start_args=CloudRes().get_start_argument(getShortGameId(game.game_id)) or ""
        #        logger.info(f"新建快捷方式: {game.path}，启动参数: {start_args}")
        #        game.create_launch_shortcut(start_args=start_args,bypass_path_check=False)
                
        #except Exception as e:
        #    logger.error(f"首次使用创建快捷方式失败: {e}")
            
    try:
        setup_shortcuts()
    except:
        logger.error("创建快捷方式失败")
    computer_name = get_computer_name()
    from run_once import run_once
    try:
        run_once()
    except Exception as e:
        logger.error(f"运行一次性任务失败: {e}")
    #如果是windows，清空DNS缓存
    if sys.platform=='win32':
        os.system("ipconfig /flushdns")

def welcome():
    print(f"[+] 欢迎使用第五人格登陆助手 {genv.get('VERSION')}!")
    print(" - 官方项目地址 : https://github.com/KKeygen/idv-login/")
    print(" - 如果你的这个工具不能用了，请前往仓库检查是否有新版本发布或加群询问！")
    print(" - 本程序使用GNU GPLv3协议开源，完全免费，严禁倒卖！")
    print(" - This program is free software: you can redistribute it and/or modify")
    print(" - it under the terms of the GNU General Public License as published by")
    print(" - the Free Software Foundation, either version 3 of the License, or")
    print(" - (at your option) any later version.")
def handle_announcement():
    if genv.get("CLOUD_ANNO")!="":
        print(f"【公告】{genv.get('CLOUD_ANNO')}")
def cloudBuildInfo():
    try:
        from buildinfo import BUILD_INFO,VERSION
        message=BUILD_INFO
        genv.set("VERSION",VERSION)
        print(f"构建信息：{message}。如需校验此版本是否被篡改，请前往官方项目地址。")
    except:
        print("警告：没有找到校验信息，请不要使用本工具，以免被盗号。")

def prepare_platform_workdir():
    if sys.platform=='win32':
        kernel32 = ctypes.WinDLL("kernel32")
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        global _console_ctrl_handler
        _console_ctrl_handler = HandlerRoutine(ctrl_handler)
        kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), (0x4|0x80|0x20|0x2|0x10|0x1|0x00|0x100))
        genv.set("FP_WORKDIR", os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
    elif sys.platform=='darwin':
        setup_signal_handlers()
        mac_app_support = os.path.expanduser("~/Library/Application Support")
        genv.set("FP_WORKDIR", os.path.join(mac_app_support, "idv-login"))
        os.environ["PROGRAMDATA"] = mac_app_support
    elif sys.platform=='linux':
        setup_signal_handlers()
        home = os.path.expanduser("~")
        genv.set("FP_WORKDIR", os.path.join(home, ".idv-login"))
        os.environ["PROGRAMDATA"] = genv.get("FP_WORKDIR")





def setup_work_directory():
    """设置和创建工作目录"""
    # 确保工作目录存在，使用makedirs可以创建多级目录
    if not os.path.exists(genv.get("FP_WORKDIR")):
        try:
            os.makedirs(genv.get("FP_WORKDIR"), exist_ok=True)
        except Exception as e:
            print(f"创建工作目录失败: {e}")
            # 如果无法创建标准目录，则尝试使用当前目录
            genv.set("FP_WORKDIR", os.path.join(os.getcwd(), "idv-login"))
            os.makedirs(genv.get("FP_WORKDIR"), exist_ok=True)
    
    # 切换到工作目录
    try:
        os.chdir(genv.get("FP_WORKDIR"))
        print(f"已将工作目录设置为 -> {genv.get('FP_WORKDIR')}")
    except Exception as e:
        print(f"切换到工作目录失败: {e}")

def record_install_root():
    try:
        install_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
        program_data = os.environ.get("PROGRAMDATA", "")
        if not program_data:
            return
        flag_dir = os.path.join(program_data, "idv-login")
        os.makedirs(flag_dir, exist_ok=True)
        flag_path = os.path.join(flag_dir, "install_root.flag")
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write(install_root)
    except Exception:
        pass

def _encode_download_path(path):
    if not path:
        return ""
    if sys.platform == "win32":
        path = path.replace("/", "\\")
    return base64.b64encode(path.encode("utf-8")).decode("utf-8")

def _encode_repair_list_path(path):
    if not path:
        return ""
    path = os.path.abspath(path).replace("\\", "/")
    return base64.b64encode(path.encode("utf-8")).decode("utf-8")

def handle_download_task(task_file_path):
    if not task_file_path:
        print("缺少下载任务文件路径")
        return False
    task_file_path = os.path.abspath(task_file_path)
    if not os.path.exists(task_file_path):
        print(f"下载任务文件不存在: {task_file_path}")
        return False
    prepare_platform_workdir()
    setup_work_directory()
    from logutil import setup_logger
    logger_local = setup_logger()
    task_data = None
    try:
        with open(task_file_path, "r", encoding="utf-8") as f:
            task_data = json.load(f)
    except Exception as e:
        logger_local.exception(f"读取下载任务文件失败: {e}")
        return False
    download_root = task_data.get("download_root", "")
    directories = task_data.get("directories", [])
    files = task_data.get("files", [])
    concurrent_files = int(task_data.get("concurrent_files", 2))
    game_id = task_data.get("game_id", "")
    version_code = task_data.get("version_code", "")
    distribution_id = int(task_data.get("distribution_id", -1))
    content_id = task_data.get("content_id")
    original_version = task_data.get("original_version", "")
    repair_list_path = task_data.get("repair_list_path", "")
    start_args = task_data.get("start_args", "")
    result = True
    ui_server_process = None
    ui_server_thread = None
    stop_event = None
    download_process = None
    use_download_ipc = bool(content_id) and distribution_id != -1 and download_root
    try:
        if use_download_ipc:
            from download_binary import ensure_binary, PORT_SEND_HEARTBEAT, PORT_RECEIVE_PROGRESS
            import download_binary
            import threading
            if not ensure_binary():
                result = False
            else:
                stop_event = threading.Event()
                topic_bytes = str(content_id).encode("utf-8") if content_id else b""
                ui_server_thread = threading.Thread(
                    target=download_binary.main_ui_server,
                    kwargs={
                        "topic": topic_bytes,
                        "sub_port": PORT_SEND_HEARTBEAT,
                        "pub_port": PORT_RECEIVE_PROGRESS,
                        "stop_event": stop_event
                    }
                )
                ui_server_thread.daemon = True
                ui_server_thread.start()
                #等待几秒钟，确保UI服务器启动完成
                import time
                time.sleep(5)
                creationflags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
                encoded_path = _encode_download_path(download_root)
                encoded_repair_list_path = _encode_repair_list_path(repair_list_path)
                #./downloadIPC  --gameid:73 --contentid:434 --subport:1737 --pubport:1740 --path:RTpcRmV2ZXJBcHBzXGR3cmcy --env:live --oversea:0 --targetVersion:v3_3028_7e8d8ea06733136dd915a6e865440158 --originVersion:v3_2547 --scene:2 --rateLimit:0  --channel:platform --locale:zh_Hans  --isSSD:1 --isRepairMode:0
                download_cmd = [
                    os.path.join(os.getcwd(), "downloadIPC.exe"),
                    f"--gameid:{distribution_id}",
                    f"--env:live",
                    f"--oversea:0",
                    f"--scene:3",
                    f"--rateLimit:0",
                    f"--channel:platform",
                    f"--locale:zh_Hans",
                    f"--isSSD:1",
                    f"--isRepairMode:1",
                    f"--contentid:{content_id}",
                    f"--subport:{PORT_SEND_HEARTBEAT}",
                    f"--pubport:{PORT_RECEIVE_PROGRESS}",
                    f"--path:{encoded_path}",
                    f"--repairListPath:{encoded_repair_list_path}",
                ]
                if version_code:
                    download_cmd.append(f"--targetVersion:{version_code}")
                if original_version:
                    download_cmd.append(f"--originVersion:{original_version}")
                else:
                    download_cmd.append(f"--originVersion:")
                download_process = subprocess.Popen(download_cmd, creationflags=creationflags)
                exit_code = download_process.wait()
                result = exit_code == 0
        if result and game_id and version_code:
            from gamemgr import GameManager
            game_mgr = GameManager()
            game = game_mgr.get_game(game_id)
            if game:
                game.version = version_code
                if distribution_id != -1:
                    game.default_distribution = distribution_id
                    game.should_auto_start=True
                game_mgr._save_games()
                print(f"下载任务完成，准备创建游戏启动快捷方式，启动参数: {start_args}")
                game.create_launch_shortcut(start_args=start_args,bypass_path_check=True)

                
        if ui_server_process:
            try:
                ui_server_process.terminate()
                ui_server_process.wait(timeout=5)
            except Exception:
                pass
        if stop_event:
            stop_event.set()
        if ui_server_thread:
            ui_server_thread.join(timeout=2)
        try:
            os.remove(task_file_path)
        except Exception as e:
            logger_local.exception(f"删除下载任务文件失败: {e}")
        try:#尝试用explorer打开下载完成的目录
            if download_root and os.path.exists(download_root):
                #使用正斜杠避免路径问题
                download_root = os.path.abspath(download_root)
                
                if sys.platform == "win32" and not result:
                    logger_local.info(f"下载任务失败，请检查日志。如需重新下载，请重新发起下载任务。下载目录: {download_root}")
                    subprocess.Popen(["explorer", download_root])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", download_root])
                elif sys.platform == "linux":
                    subprocess.Popen(["xdg-open", download_root])
        except Exception as e:
            logger_local.exception(f"打开下载目录失败: {e}")
        return result
    except Exception as e:
        logger_local.exception(f"下载任务执行失败: {e}")
    finally:
        from gamemgr import Game
        game=Game(game_id=game_id,path=os.path.join(download_root,"dummy.exe"))

def cleanup_expired_certificates():
    """清理过期的证书文件"""
    # 检查证书是否过期
    web_cert_expired = m_certmgr.is_certificate_expired(genv.get("FP_WEBCERT"))
    ca_cert_expired = m_certmgr.is_certificate_expired(genv.get("FP_CACERT"))

    if web_cert_expired or ca_cert_expired:
        logger.info("一个或多个证书已过期或不存在，正在重新生成...")
        # 删除旧证书文件（如果存在）
        cert_files = [
            (genv.get("FP_WEBCERT"), "网站证书"),
            (genv.get("FP_WEBKEY"), "网站密钥"),
            (genv.get("FP_CACERT"), "CA证书")
        ]
        
        for cert_path, cert_name in cert_files:
            if os.path.exists(cert_path):
                os.remove(cert_path)
                logger.info(f"已删除旧的{cert_name}: {cert_path}")
    
    return web_cert_expired, ca_cert_expired


def generate_certificates_if_needed():
    """检查并生成必要的证书文件"""
    web_cert_expired, ca_cert_expired = cleanup_expired_certificates()
    
    if (os.path.exists(genv.get("FP_WEBCERT")) == False) or \
       (os.path.exists(genv.get("FP_WEBKEY")) == False) or \
       (os.path.exists(genv.get("FP_CACERT")) == False) or \
       web_cert_expired or ca_cert_expired: # 添加过期检查条件
        logger.info("正在生成必要的证书文件...")

        ca_key = m_certmgr.generate_private_key(bits=2048)
        ca_cert = m_certmgr.generate_ca(ca_key)
        m_certmgr.export_cert(genv.get("FP_CACERT"), ca_cert)

        srv_key = m_certmgr.generate_private_key(bits=2048)
        srv_cert = m_certmgr.generate_cert(
            [genv.get("DOMAIN_TARGET"),genv.get("DOMAIN_TARGET_OVERSEA"),"localhost"], srv_key, ca_cert, ca_key
        )

        if m_certmgr.import_to_root(genv.get("FP_CACERT")) == False:
            logger.error("导入CA证书失败!")
            if sys.platform == 'win32': # Keep platform-specific behavior
                os.system("pause")
            else:
                input("导入CA证书失败，请按回车键退出。")
            sys.exit(-1)

        m_certmgr.export_cert(genv.get("FP_WEBCERT"), srv_cert)
        m_certmgr.export_key(genv.get("FP_WEBKEY"), srv_key)
        logger.info("证书初始化成功!")


def setup_backup_version_manager():
    """初始化备用版本管理器"""
    from backupvermgr import BackupVersionMgr
    backupVerMgr_instance = BackupVersionMgr(work_dir=genv.get("FP_WORKDIR"))
    return backupVerMgr_instance

def setup_shortcuts():
    """设置快捷方式"""
    from shortcutmgr import ShortcutMgr
    shortcutMgr_instance = ShortcutMgr()
    shortcutMgr_instance.handle_shortcuts()

def start_mitm_mode(backupVerMgr_instance):
    """启动MITM代理模式"""
    logger.warning("正在启动备用方案 (mitmproxy)...")
    pid = os.getpid()
    try:
        genv.set("backupVerMgr", backupVerMgr_instance)
        if backupVerMgr_instance.setup_environment(): 
            if backupVerMgr_instance.start_mitmproxy_redirect(pid):
                genv.set("USING_BACKUP_VER", True, False) # Mark as actively using MITM
                logger.info("备用方案 (mitmproxy) 启动成功!")
                return True
            else:
                logger.error("备用方案 (mitmproxy) 启动失败。程序将继续，但可能无法正常工作。")
        else:
            logger.error("备用方案 (mitmproxy) 环境设置失败。程序将继续，但可能无法正常工作。")
    except Exception as e_mitm:
        logger.exception(f"启动备用方案 (mitmproxy) 时发生错误: {e_mitm}")
        logger.error("备用方案 (mitmproxy) 启动时发生异常。程序将继续，但可能无法正常工作。")
    return False


def setup_host_manager():
    """设置Host管理器进行域名重定向"""
    global m_hostmgr
    logger.info("正在重定向目标地址到本机 (hosts 文件修改)...")
    try:
        from hostmgr import hostmgr 
        # m_hostmgr is a global variable, assign the instance to it.
        m_hostmgr = hostmgr() 

        if m_hostmgr.isExist(genv.get("DOMAIN_TARGET")) == True:
            logger.info("识别到手动定向!")
            logger.info(
                f"请确保已经将 {genv.get('DOMAIN_TARGET')}、{genv.get('DOMAIN_TARGET_OVERSEA')} 和 localhost 指向 127.0.0.1"
            )
        else:
            m_hostmgr.add(genv.get("DOMAIN_TARGET"), "127.0.0.1")
            m_hostmgr.add(genv.get("DOMAIN_TARGET_OVERSEA"), "127.0.0.1")
            m_hostmgr.add("localhost", "127.0.0.1")
        return True
    except Exception as e_hostmgr:
        logger.warning(f"Host管理器初始化失败 ({e_hostmgr})，正在尝试备用方案 (mitmproxy)...")
        return False


def fallback_to_mitm():
    """当Host管理器失败时，回退到MITM模式"""
    from backupvermgr import BackupVersionMgr
    pid = os.getpid()
    try:
        backupVerMgr_instance = BackupVersionMgr(work_dir=genv.get("FP_WORKDIR"))
        genv.set("backupVerMgr", backupVerMgr_instance) # Store for cleanup
        # Check if backupVer was already true (e.g. from config) or if env setup is ok
        if genv.get("backupVer", False) and backupVerMgr_instance.setup_environment():
            genv.set("backupVer", True, True) # Mark intention/attempt
            if backupVerMgr_instance.start_mitmproxy_redirect(pid):
                genv.set("USING_BACKUP_VER", True, False) # Mark as actively using MITM
                logger.info("备用方案 (mitmproxy) 启动成功!")
            else:
                logger.error("备用方案 (mitmproxy) 启动失败。")
        else:
            logger.error("备用方案 (mitmproxy) 环境设置失败。")
    except Exception as e_mitm_fallback:
        logger.exception(f"尝试备用方案 (mitmproxy) 时发生错误: {e_mitm_fallback}")
        logger.error("备用方案 (mitmproxy) 尝试时发生异常。")


def setup_network_proxy(force_mitm_mode):
    """设置网络代理（Host管理器或MITM模式）"""
    backupVerMgr_instance = setup_backup_version_manager()
    
    if force_mitm_mode:
        logger.info("命令行参数指定使用备用模式。")
        genv.set("backupVer", True, True)
        start_mitm_mode(backupVerMgr_instance)
    else:
        # Standard host modification logic
        if not setup_host_manager():
            # Fallback to MITM (original logic)
            fallback_to_mitm()


def handle_error_and_exit(e):
    """处理异常并退出程序"""
    # hotfix: mark crash so next run can rollback pending hotfix
    try:
        genv.set("last_run_state", "crash", True)
        genv.set("last_run_state_ts", int(time.time()), True)
        genv.set("last_run_error", str(e), True)
    except Exception:
        pass
    try:
        import debugmgr
        debugmgr.DebugMgr().export_debug_info()
    except:
        pass
    if logger: # Check if logger was initialized
        logger.exception(
            f"发生未处理的异常:{e}.反馈时请发送日志！\n日志路径:{genv.get('FP_WORKDIR')}下的log.txt"
        )
    else:
        print(f"发生未处理的异常:{e}. 日志记录器未初始化。")
        # Try to provide workdir info if genv is available
        workdir_path = genv.get('FP_WORKDIR', '未知') if 'genv' in locals() else '未知'
        print(f"工作目录: {workdir_path}")

    # Original logic to open explorer, with safeguards
    try:
        log_file_path = os.path.join(genv.get("FP_WORKDIR"), "log.txt")
        if sys.platform == 'win32' and os.path.exists(log_file_path):
            # Ensure the path is quoted for explorer if it contains spaces
            os.system(f'explorer /select,"{log_file_path}"')
        input_message = "发生错误。如果可能，已尝试打开日志文件所在目录。按回车键退出。"
    except Exception: # Fallback if genv or FP_WORKDIR is not set
        input_message = "发生严重错误，无法获取日志路径。按回车键退出。"

    input(input_message)

def setup_signal_handlers():
    import signal
    
    def signal_handler(sig, frame):
        print(f"捕获到信号 {sig}，正在执行清理...")
        handle_exit()
        sys.exit(0)
    # 捕获常见的终止信号
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # kill 命令
    if sys.platform!='win32':
        signal.signal(signal.SIGHUP, signal_handler)   # 终端关闭
def main(cli_args=None):


    """主函数入口"""
    global logger
    if cli_args is None:
        cli_args = CLI_ARGS
    prepare_platform_workdir()
    
    # 设置工作目录
    setup_work_directory()
    

    # Initialize logger (assign to global logger variable)
    from logutil import setup_logger
    logger = setup_logger() 

    force_mitm_mode = cli_args.mitm

    try:
        cloudBuildInfo()
        initialize() # This sets up atexit(handle_exit) among other things

        # hotfix gate: verify config cache can be written; if not, skip all hotfix logic to avoid infinite restarts.
        can_run_hotfix = _hotfix_probe_cache_write_once()
        if not can_run_hotfix:
            logger.warning("【热更新】探测到配置缓存写入失败：已跳过本次所有热更新逻辑（避免无限重启）。")
        else:
            # hotfix: rollback/confirm pending hotfix based on last run state (genv 环境在 initialize 后更完整)
            try:
                hotfix_pre_start_check_and_rollback_if_needed()
            except Exception:
                pass


        genv.set("last_run_state", "running", True)
        genv.set("last_run_state_ts", int(time.time()), True)


        # hotfix: apply if needed (may restart process)
        if can_run_hotfix:
            handle_hotfix_if_needed()

        welcome()
        handle_update()
        handle_announcement()
        
        # 证书管理
        generate_certificates_if_needed()

        # 网络代理设置
        setup_network_proxy(force_mitm_mode)
        
        # Start proxy server (m_proxy is global, initialized in initialize())
        logger.info("正在启动代理服务器...")
        m_proxy.run()
    except Exception as e:
        handle_error_and_exit(e)


if __name__ == "__main__":
    try:
        if CLI_ARGS.download:
            success = handle_download_task(CLI_ARGS.download)
            sys.exit(0 if success else 1)
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
    main(CLI_ARGS)
