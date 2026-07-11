import os

from envmgr import genv
from logutil import setup_logger

def _show_msgbox(title: str, text: str, *, is_error: bool = False):
    """弹窗提示（Qt 尚未初始化，用 Win32 MessageBox）"""
    import sys
    if sys.platform == "win32":
        import ctypes
        style = 0x10 if is_error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, text, title, style)
    else:
        print(f"[{title}] {text}")


def _show_yesno(title: str, text: str) -> bool:
    """Yes/No 弹窗，返回用户是否选择 Yes"""
    import sys
    if sys.platform == "win32":
        import ctypes
        # MB_YESNO | MB_ICONQUESTION
        result = ctypes.windll.user32.MessageBoxW(0, text, title, 0x24)
        return result == 6  # IDYES
    else:
        print(f"[{title}] {text}")
        return input("(y/n): ").strip().lower() == 'y'


def _select_exe_file(title: str = "选择游戏可执行文件") -> str:
    """打开文件选择对话框选择 exe 文件"""
    import sys
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        
        OFN_FILEMUSTEXIST = 0x1000
        OFN_PATHMUSTEXIST = 0x800
        MAX_PATH = 260
        
        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD),
                ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD),
                ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD),
                ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD),
                ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR),
                ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD),
                ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR),
                ("lCustData", wintypes.LPARAM),
                ("lpfnHook", wintypes.LPVOID),
                ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", wintypes.LPVOID),
                ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD),
            ]
        
        file_buffer = ctypes.create_unicode_buffer(MAX_PATH)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.lpstrFilter = "可执行文件\0*.exe\0所有文件\0*.*\0\0"
        ofn.lpstrFile = ctypes.cast(file_buffer, wintypes.LPWSTR)
        ofn.nMaxFile = MAX_PATH
        ofn.lpstrTitle = title
        ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
        
        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return file_buffer.value
        return ""
    except Exception:
        return ""


def _select_directory(title: str = "选择安装目录") -> str:
    """打开目录选择对话框。"""
    import sys
    if sys.platform != "win32":
        return ""
    try:
        from PyQt6.QtWidgets import QApplication, QFileDialog
        app = QApplication.instance()
        if app is None:
            return ""
        return QFileDialog.getExistingDirectory(None, title, os.path.expanduser("~"))
    except Exception:
        return ""


def _is_drive_root(path: str) -> bool:
    normalized = os.path.abspath(path)
    drive, tail = os.path.splitdrive(normalized)
    return bool(drive) and os.path.abspath(drive + os.sep) == normalized


def _probe_game_setup(logger):
    """游戏设置引导：检查路径/自启配置并按数量应用策略。"""
    import sys
    import os
    if sys.platform != "win32":
        return
    
    # 已经引导过了
    if genv.get("game_setup_probe_done_0403", False):
        return
    
    from gamemgr import GameManager
    from channelHandler.channelUtils import getShortGameId
    game_mgr = GameManager()
    
    all_games = game_mgr.list_games()
    auto_start_games = game_mgr.list_auto_start_games()
    fever_games = game_mgr.list_fever_games()
    
    # 建立发烧平台游戏的短id映射
    fever_by_short_id = {}
    for fg in fever_games:
        short_id = getShortGameId(fg.get("game_id", ""))
        if short_id:
            fever_by_short_id[short_id] = fg
    
    configured_game_ids = []
    shortcut_created_ids = set()

    def _record_game(gid, shortcut_created=False):
        if not gid:
            return
        if gid not in configured_game_ids:
            configured_game_ids.append(gid)
        if shortcut_created:
            shortcut_created_ids.add(gid)

    def _apply_startup_strategy():
        """按本轮配置数量应用策略：1个自启；>=2个走快捷方式且不自启。"""
        if not configured_game_ids:
            return

        if len(configured_game_ids) == 1:
            gid = configured_game_ids[0]
            if game_mgr.set_game_auto_start(gid, True):
                logger.info(f"本轮仅配置 1 个游戏，已设为自启: {gid}")
            return

        # >=2：关闭这些游戏的自启，统一推荐快捷方式启动
        for gid in configured_game_ids:
            game_mgr.set_game_auto_start(gid, False)
            game_obj = game_mgr.get_existing_game(gid)
            if not game_obj or not game_obj.path or not os.path.exists(game_obj.path):
                continue
            if gid in shortcut_created_ids:
                continue
            try:
                game_obj.create_tool_launch_shortcut(game_obj.path)
                logger.info(f"已为游戏创建快捷方式: {gid}")
            except Exception as e:
                logger.error(f"创建快捷方式失败 {gid}: {e}")
        logger.info("本轮配置 >=2 个游戏，已关闭自启并切换为快捷方式启动")

    if not all_games:
        # 情况 1：游戏列表为空 → 建议导入发烧平台游戏
        if fever_games:
            for fg in fever_games:
                name = fg.get("display_name") or fg.get("game_id", "未知游戏")
                if _show_yesno(
                    "导入游戏",
                    f"检测到您尚未添加任何游戏。\n\n"
                    f"在发烧平台发现游戏：{name}\n"
                    f"是否导入此游戏？（您只需要导入您打算使用免扫码工具的游戏）"
                ):
                    try:
                        final_gid = game_mgr.import_fever_game(
                            fg["game_id"], fg.get("distribution_id", -1), fg.get("path", "")
                        )
                        logger.info(f"已导入发烧平台游戏: {fg['game_id']}")
                        _record_game(final_gid, shortcut_created=True)
                    except Exception as e:
                        logger.error(f"导入游戏失败 {fg['game_id']}: {e}")

        _apply_startup_strategy()
        genv.set("game_setup_probe_done_0403", True, True)
        return

    if all_games and not auto_start_games:
        # 情况 2：有游戏但没有自启游戏 → 引导配置路径/启动方式
        for g in all_games:
            game_id = g["game_id"]
            game_name = g.get("name") or game_id
            game_obj = game_mgr.get_existing_game(game_id)
            has_path = bool(game_obj and game_obj.path and os.path.exists(game_obj.path))
            short_id = getShortGameId(game_id)

            # 检查发烧平台是否有对应游戏
            fever_match = fever_by_short_id.get(short_id)

            if has_path:
                if _show_yesno(
                    "配置启动方式",
                    f'是否将"{game_name}"纳入启动方案？\n\n'
                    "单个游戏会设为自启；多个游戏会改为快捷方式启动。"
                ):
                    _record_game(game_id, shortcut_created=False)
                continue

            if fever_match:
                # 发烧平台有对应游戏，可自动获取路径
                if _show_yesno(
                    "设置游戏路径",
                    f'游戏"{game_name}"尚未设置路径。\n\n'
                    "已在发烧平台找到对应路径，是否自动导入？"
                ):
                    try:
                        final_gid = game_mgr.import_fever_game(
                            fever_match["game_id"],
                            fever_match.get("distribution_id", -1),
                            fever_match.get("path", ""),
                        )
                        logger.info(f"已从发烧平台导入游戏路径: {game_id}")
                        _record_game(final_gid, shortcut_created=True)
                    except Exception as e:
                        logger.error(f"导入路径失败 {game_id}: {e}")
            else:
                # 需要用户手动选择 exe 路径
                if _show_yesno(
                    "设置游戏路径",
                    f'游戏"{game_name}"尚未设置路径。\n\n'
                    "是否现在选择游戏可执行文件/快捷方式？"
                ):
                    exe_path = _select_exe_file(f"选择 {game_name} 的可执行文件")
                    if exe_path:
                        try:
                            game_mgr.set_game_path(game_id, exe_path)
                            logger.info(f"已设置游戏路径: {game_id}, 路径: {exe_path}")
                            _record_game(game_id, shortcut_created=False)
                        except Exception as e:
                            logger.error(f"设置路径失败 {game_id}: {e}")
                    else:
                        logger.info(f"用户未选择 {game_id} 的可执行文件，跳过")

        _apply_startup_strategy()
    
    # 情况 3：有游戏且有自启 → 什么都不提醒
    genv.set("game_setup_probe_done_0403", True, True)


def _has_h55_account_record(logger) -> bool:
    try:
        from channelmgr import ChannelManager
        from channelHandler.channelUtils import cmp_game_id, getShortGameId
        channel_mgr = ChannelManager()
        for channel in channel_mgr.channels:
            game_id = getattr(channel, "game_id", "")
            if game_id and (cmp_game_id(game_id, "aecfrt3rmaaaaajl-g-h55") or getShortGameId(game_id) == "h55"):
                return True
    except Exception as e:
        logger.error(f"检查 h55 账号记录失败: {e}")
    return False


def _get_h55_path_installation(game_mgr):
    from channelHandler.channelUtils import getShortGameId
    for game in game_mgr.games.values():
        if getShortGameId(game.game_id) != "h55":
            continue
        candidates = []
        for installation in game.installations.values():
            if not installation.path:
                continue
            if installation.distribution_id not in (-1, 73):
                continue
            candidates.append(installation)
        default = game.get_installation()
        if default in candidates:
            return game, default
        if len(candidates) == 1:
            return game, candidates[0]
        if len(candidates) > 1:
            return game, None
    return None, None


def _prepare_h55_game_path(game_mgr, logger):
    target_dir = _select_directory("选择第五人格新引擎安装目录")
    if not target_dir:
        logger.info("用户未选择第五人格安装目录")
        return None, None
    if _is_drive_root(target_dir):
        _show_msgbox(
            "安装目录无效",
            "不能直接选择磁盘根目录（例如 E:\\）。\n请新建或选择一个专门的游戏安装目录。",
            is_error=True,
        )
        return None, None

    game_id = "aecfrt3rmaaaaajl-g-h55"
    game = game_mgr.get_game(game_id)
    game_path = os.path.join(target_dir, "dwrg.exe")
    game_mgr.rename_game(game_id, "第五人格")
    installation = game_mgr.add_game_installation(
        game_id=game_id,
        path=game_path,
        distribution_id=73,
        source="download",
        startup_path="dwrg.exe",
        set_default=True,
    )
    logger.info(f"已设置第五人格启动路径: {game_path}")
    return game, installation


def _probe_h55_version_update_prompt(logger):
    """当云端 h55 分发版本越过配置基准时，一次性提醒用户去启动器更新。"""
    import sys
    if sys.platform != "win32":
        return

    try:
        from cloudRes import CloudRes
        from gamemgr import Game, GameManager

        file_info = Game("aecfrt3rmaaaaajl-g-h55").get_file_distribution_info(73)
        remote_version = file_info.get("version_code", "") if file_info else ""
        if not remote_version:
            return

        prompt_key = f"h55_version_update_prompted_{remote_version}"
        if genv.get(prompt_key, False):
            return

        game_mgr = GameManager()
        h55_game, h55_installation = _get_h55_path_installation(game_mgr)
        has_h55_account = _has_h55_account_record(logger)
        if not h55_game and not has_h55_account:
            return
        if h55_game and h55_installation is None:
            logger.info("检测到多个第五人格安装且默认安装不明确，跳过自动更新提示")
            return

        if h55_installation and h55_installation.installed_version == remote_version:
            return
        local_version = (
            h55_installation.installed_version
            if h55_installation and h55_installation.installed_version
            else "未知"
        )

        genv.set(prompt_key, True, True)
        if _show_yesno(
            "第五人格6.18新版本提醒",
            "检测到游戏《第五人格》已有新版本。\n\n"
            f"最新版本：{remote_version}\n"
            f"您的版本：{local_version}\n\n"
            "您收到此提示是因为您的工具保存有第五人格账号或第五人格游戏路径，此提示只会出现一次，如果选择以后更新，可以自行前往“渠道服管理界面”更新。"
            "是否现在更新《第五人格》（不需要安装发烧平台）？\n"
        ):
            update_started = False
            if h55_game and h55_installation:
                logger.info(f"用户选择直接更新第五人格")
                logger.warning(f"更新时会出现两个黑色窗口，进度条走到100%后更新即完成，此时再次启动工具或点击桌面上的专用快捷方式即可启动游戏。")
                update_started = h55_game.try_update(
                    73, max_concurrent_files=4,
                    installation_id=h55_installation.installation_id,
                )
            else:
                h55_game, h55_installation = _prepare_h55_game_path(game_mgr, logger)
                if h55_game and h55_installation:
                    logger.info(f"用户选择安装/更新第五人格")
                    logger.warning(f"更新时会出现两个黑色窗口，进度条走到100%后安装/更新即完成，此时再次启动工具或点击桌面上的专用快捷方式即可启动游戏。")
                    update_started = h55_game.try_update(
                        73, max_concurrent_files=4,
                        installation_id=h55_installation.installation_id,
                    )
            if h55_game and h55_installation:
                game_mgr._save_games()
            if update_started:
                sys.exit()
    except Exception as e:
        logger.error(f"第五人格版本更新提醒失败: {e}")


def _is_compat_port_available() -> bool:
    """检查兼容模式所需的 443 端口是否可用。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 443))
        return True
    except socket.error:
        return False


def _cleanup_residual_proxy_registry(logger):
    """清理注册表中残留的代理环境变量（旧版本遗留 / 极端情况）。

    新版 proxy_env.py 已将 _SAVED_PROXY_ENV 持久化，正常崩溃恢复由其处理。
    此函数用于旧版本升级时的兼容清理：旧版未持久化 saved state，
    需要直接按端口范围匹配注册表值并删除。
    """
    try:
        import winreg
        from mitm_proxy import DEFAULT_PROXY_PORT
        ports_to_check = set(range(DEFAULT_PROXY_PORT, DEFAULT_PROXY_PORT + 11))
        cleaned = False
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
        ) as key:
            # HTTP_PROXY / HTTPS_PROXY 格式为 http://127.0.0.1:{port}
            for var in ("HTTP_PROXY", "HTTPS_PROXY"):
                try:
                    val, _ = winreg.QueryValueEx(key, var)
                    if any(f"127.0.0.1:{p}" in val for p in ports_to_check):
                        winreg.DeleteValue(key, var)
                        logger.info(f"已清理残留代理环境变量: {var}={val}")
                        cleaned = True
                except FileNotFoundError:
                    pass
            # NO_PROXY 是域名列表，只要检测到 HTTP_PROXY 残留就一并清除
            if cleaned:
                try:
                    winreg.DeleteValue(key, "NO_PROXY")
                    logger.info("已清理残留 NO_PROXY")
                except FileNotFoundError:
                    pass
        if cleaned:
            from proxy_env import _broadcast_env_change
            _broadcast_env_change()
    except Exception as e:
        logger.error(f"清理残留代理环境变量失败: {e}")


def _probe_proxy_mode(logger):
    """代理模式引导：统一使用兼容模式。"""
    import sys
    import subprocess
    if sys.platform != "win32":
        return
    
    # 已经询问过了
    if genv.get("proxy_mode_asked_0403", False):
        if genv.get("proxy_mode", "") in ("global", "process") and not genv.get("proxy_mode_compat_prompted_0420", False):
            import hotfixmgr
            if not hotfixmgr.probe_cache_write_once():
                return

            # 检查 443 端口是否可用，不可用则跳过迁移（下次启动会重试）
            if not _is_compat_port_available():
                logger.warning("443 端口被占用，暂不迁移到兼容模式")
                return

            # 清理上次崩溃可能残留的代理注册表项
            _cleanup_residual_proxy_registry(logger)

            genv.set("proxy_mode", "compat", True)
            genv.set("proxy_mode_compat_prompted_0420", True, True)
            logger.info("将已有的代理模式自动变更为兼容模式，即将重启...")


            logger.warning("代理模式已变更为兼容模式，应用即将重启。如果重启后遇到网络连接问题，请重启计算机一次即可解决。")

            try:
                from main import handle_exit
                handle_exit()
            except Exception:
                pass

            if getattr(sys, 'frozen', False):
                args = [sys.executable] + sys.argv[1:]
            else:
                args = [sys.executable] + sys.argv

            # 清理当前进程的代理环境变量，防止子进程继承
            _proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                           "NO_PROXY", "no_proxy")
            clean_env = {k: v for k, v in os.environ.items() if k not in _proxy_vars}

            try:
                subprocess.Popen(args, cwd=genv.get("SCRIPT_DIR") or os.getcwd(), env=clean_env)
            except Exception:
                subprocess.Popen(args, env=clean_env)
            os._exit(0)
        return

    # 首次探测：直接设置为兼容模式，不再承担快捷方式/路径引导。
    genv.set("proxy_mode", "compat", True)
    logger.info("首次探测，自动应用兼容模式")
    genv.set("proxy_mode_asked_0403", True, True)


def run_once():
    """一次性任务，通过 genv 键控制只执行一次"""
    logger = setup_logger()

    # config.json 写入健康检查
    if not genv.get("config_fixed_0403", False):
        import hotfixmgr, os
        if not hotfixmgr.probe_cache_write_once():
            logger.warning("config.json 写入探测失败，尝试修复...")
            try:
                cache_path = "config.json"
                if os.path.exists(cache_path):
                    os.remove(cache_path)
                    logger.info("已删除损坏的 config.json")
            except Exception as e:
                logger.error(f"删除 config.json 失败: {e}")

            # 尝试重新写入
            if hotfixmgr.probe_cache_write_once():
                genv.set("config_fixed_0403", True, True)
                _show_msgbox(
                    "配置文件已重置",
                    "检测到配置文件损坏，已自动重置。\n"
                    "您的账号记录不受影响，但部分设置（如自动登录延迟）可能需要重新配置。",
                )
                logger.info("config.json 修复成功")
            else:
                _show_msgbox(
                    "配置文件修复失败",
                    "无法写入配置文件，这通常是权限问题导致的。\n"
                    "请尝试以管理员身份运行，或联系开发者获取支持。",
                    is_error=True,
                )
                logger.error("config.json 修复失败，写入仍然不可用")
    
    # 游戏设置引导
    try:
        _probe_game_setup(logger)
    except Exception as e:
        logger.error(f"游戏设置引导失败: {e}")

    # 第五人格新版本引导
    try:
        _probe_h55_version_update_prompt(logger)
    except Exception as e:
        logger.error(f"第五人格新版本引导失败: {e}")
    
    # 代理模式引导（多游戏有账号时）
    try:
        _probe_proxy_mode(logger)
    except Exception as e:
        logger.error(f"代理模式引导失败: {e}")
    
    # 清理旧版本遗留的 hosts 记录（修复 isExist bug 后需要重新执行）
    if not genv.get("hosts_cleanup_v600_done", False):
        try:
            from hostmgr import hostmgr
            h_mgr = hostmgr()
            domain_target = genv.get("DOMAIN_TARGET", "service.mkey.163.com")
            domain_oversea = genv.get("DOMAIN_TARGET_OVERSEA", "sdk-os.mpsdk.easebar.com")
            
            if h_mgr.isExist(domain_target):
                logger.warning(f"Hosts文件中已存在{domain_target}的记录，正在尝试删除旧记录...")
                h_mgr.remove(domain_target)
            if h_mgr.isExist(domain_oversea):
                logger.warning(f"Hosts文件中已存在{domain_oversea}的记录，正在尝试删除旧记录...")
                h_mgr.remove(domain_oversea)
            
            genv.set("hosts_cleanup_v600_done", True, True)
            logger.info("hosts 清理完成")
        except Exception as e:
            logger.error(f"删除可能存在的旧Hosts记录失败: {e}")

    # 一次性清理残留的 NRPT 规则和代理环境变量（工具崩溃/异常退出可能遗留）
    if not genv.get("nrpt_env_cleanup_v603_done", False):
        import sys
        if sys.platform == "win32":
            try:
                from mitm_proxy import remove_all_nrpt_rules
                remove_all_nrpt_rules()
                logger.info("已清理残留 NRPT 规则")
            except Exception as e:
                logger.error(f"清理残留 NRPT 规则失败: {e}")
            _cleanup_residual_proxy_registry(logger)
            genv.set("nrpt_env_cleanup_v603_done", True, True)
