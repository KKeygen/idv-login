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
