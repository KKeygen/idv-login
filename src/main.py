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
import subprocess
import time
import base64


def parse_command_line_args():
    """解析命令行参数"""
    arg_parser = argparse.ArgumentParser(description="第五人格登陆助手")
    arg_parser.add_argument('--download', type=str, default="", help='下载任务文件绝对路径')
    arg_parser.add_argument('--uri', type=str, default="", help='处理 idvlogin:// URI Scheme 调用')
    arg_parser.add_argument('--open-ui', action='store_true', help='启动后直接打开渠道服管理界面')
    arg_parser.add_argument('--proxy-port', type=int, default=10717, help='mitmproxy 监听端口 (默认 10717)')
    return arg_parser.parse_args()


CLI_ARGS = parse_command_line_args()


import socket
import os
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
import hotfixmgr
import app_state
from proxy_env import set_proxy as _set_proxy, unset_proxy as _unset_proxy


# Global variable declarations
m_certmgr = None
m_proxy = None
m_cloudres=None
logger = None # Will be initialized in __main__
_console_ctrl_handler = None

# Compat mode globals
_dns_policy_mgr = None  # DnsPolicyManager instance for compat mode
_dns_server = None      # LocalDnsServer instance for compat mode


def _configured_target_domains():
    domains = [
        genv.get("DOMAIN_TARGET", "service.mkey.163.com"),
        genv.get("DOMAIN_TARGET_OVERSEA", "sdk-os.mpsdk.easebar.com"),
        genv.get("DOMAIN_TARGET_AUTH_STATUS", ""),
    ]
    return [str(domain).strip() for domain in domains if str(domain or "").strip()]


# -- 全局异常钩子 --
def _global_excepthook(exc_type, exc_value, exc_tb):
    """处理主线程中未捕获的异常"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    import traceback
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    if logger:
        logger.critical(f"未捕获的异常:\n{tb_str}")
    else:
        print(f"未捕获的异常:\n{tb_str}", file=sys.stderr)

sys.excepthook = _global_excepthook

def _threading_excepthook(args):
    """处理子线程中未捕获的异常 (Python 3.8+)"""
    if args.exc_type is SystemExit:
        return
    import traceback
    tb_str = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    thread_name = args.thread.name if args.thread else "Unknown"
    if logger:
        logger.critical(f"线程 '{thread_name}' 中未捕获的异常:\n{tb_str}")
    else:
        print(f"线程 '{thread_name}' 中未捕获的异常:\n{tb_str}", file=sys.stderr)

import threading
threading.excepthook = _threading_excepthook


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


# ------------------------------------------------------------------
# 用户级代理环境变量管理 (Windows)
# 全局标志，防止handle_exit被多次调用
_exit_handled = False
_exit_lock = threading.Lock()

def handle_exit():
    global _exit_handled, _dns_policy_mgr, _dns_server
    with _exit_lock:
        if _exit_handled:
            return
        _exit_handled = True
    
    # Assuming logger is initialized by the time this is called via atexit or signal
    # hotfix: if user quits during countdown, persist skip permanently
    try:
        hotfixmgr.handle_exit_skip_if_active()
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

    if app_state.fever_bridge is not None:
        try:
            app_state.fever_bridge.stop()
        except Exception:
            if logger:
                logger.exception("停止平台托管登录失败")
        app_state.fever_bridge = None

    # 停止 mitmproxy 代理
    proxy_mgr = app_state.proxy_mgr
    if proxy_mgr:
        if logger: 
            logger.info("正在停止 mitmproxy 代理...")
        else: 
            print("正在停止 mitmproxy 代理...")
            sys.stdout.flush()
        proxy_mgr.stop()
        if logger:
            logger.info("mitmproxy 代理已停止")
        else:
            print("mitmproxy 代理已停止")
            sys.stdout.flush()

    # 兼容模式清理：停止 DNS 服务器和清理 DNS 策略
    if _dns_server:
        try:
            _dns_server.stop()
            _dns_server = None
        except Exception as e:
            if logger:
                logger.warning(f"停止 DNS 服务器失败: {e}")

    if _dns_policy_mgr:
        try:
            _dns_policy_mgr.cleanup()
            _dns_policy_mgr = None
        except Exception as e:
            if logger:
                logger.warning(f"清理 DNS 策略失败: {e}")

    # 清理自定义 DNS 缓存
    try:
        from mitm_proxy import clear_custom_dns
        clear_custom_dns()
    except Exception:
        pass

    # 恢复代理设置（Windows 环境变量 / macOS networksetup / Linux gsettings）
    try:
        if logger:
            logger.info("正在恢复代理设置...")
        _unset_proxy()
        if logger:
            logger.info("代理设置已恢复")
    except Exception as e:
        if logger:
            logger.warning(f"恢复代理设置失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())

    # 注销 URI Scheme（减少痕迹）
    try:
        if logger:
            logger.info("正在注销 URI Scheme...")
        from uri_scheme import unregister_uri_scheme
        unregister_uri_scheme()
        if logger:
            logger.info("URI Scheme 已注销")
    except Exception as e:
        if logger:
            logger.warning(f"注销 URI Scheme 失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
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
        # 强制结束进程，防止主线程阻塞在 Qt 事件循环 (app.exec()) 中导致程序假死
        # 等待足够时间以确保清理操作（NRPT 规则删除、WM_SETTINGCHANGE 广播等）完成
        import time, os
        time.sleep(3.0)
        os._exit(0)
        return True
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
                # Python脚本：需要传递完整的argv，argv[0] 转为绝对路径
                abs_argv0 = os.path.abspath(sys.argv[0])
                argvs = [f'"{abs_argv0}"'] + [f'"{a}"' for a in sys.argv[1:]]
            
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
    global m_certmgr, m_proxy, m_cloudres

        # initialize workpath
    if not os.path.exists(genv.get("FP_WORKDIR")):
        os.mkdir(genv.get("FP_WORKDIR"))
    os.chdir(os.path.join(genv.get("FP_WORKDIR")))


    # initialize the global vars at first
    # 全局禁用 requests 库的环境变量代理 (HTTP_PROXY 等),
    # 防止本工具设置的系统代理影响 Python 侧的 HTTP 请求
    import requests as _requests_mod
    _orig_session_init = _requests_mod.Session.__init__

    def _no_trust_env_init(self, *a, **kw):
        _orig_session_init(self, *a, **kw)
        self.trust_env = False

    _requests_mod.Session.__init__ = _no_trust_env_init
    #Refer to https://forum.qt.io/post/833203. LGTM, yet potentially risky.
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    genv.set("DOMAIN_TARGET", "service.mkey.163.com")
    genv.set("DOMAIN_TARGET_OVERSEA","sdk-os.mpsdk.easebar.com")
    genv.set("DOMAIN_TARGET_AUTH_STATUS", "mgbsdk.matrix.netease.com")
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
        "https://gitee.com/opguess/idv-login/raw/main/assets/cloudRes.json",
        "https://hk.gh-proxy.org/https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/assets/cloudRes.json",
        "https://cdn.jsdelivr.net/gh/KKeygen/idv-login@main/assets/cloudRes.json",
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
                    from secure_write import write_json_restricted
                    write_json_restricted(cache_path, local_cloudres)
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
    app_state.cloud_res = m_cloudres
    genv.set("CLOUD_VERSION",m_cloudres.get_version())
    genv.set("CLOUD_ANNO",m_cloudres.get_announcement())
    m_cloudres.start_dynamic_game_catalog_update()

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
        from secure_write import write_json_restricted
        write_json_restricted(genv.get("FP_FAKE_DEVICE"), sdkDevice)
    else:
        with open(genv.get("FP_FAKE_DEVICE"), "r", encoding="utf-8") as f:
            sdkDevice = json.load(f)
    app_state.fake_device = sdkDevice
    
    if not os.path.exists(genv.get("GLOB_LOGIN_PROFILE_PATH")):
        os.makedirs(genv.get("GLOB_LOGIN_PROFILE_PATH"))
    
    from certmgr import certmgr
    
    
    from channelmgr import ChannelManager
    m_certmgr = certmgr()
    # Proxy manager is created later during setup_network_proxy()
    m_proxy = None
    app_state.channels_helper = ChannelManager()

    logger.info("初始化内置浏览器")
    os.environ.pop('QT_QPA_PLATFORM_PLUGIN_PATH', None)
    os.environ.pop('QT_PLUGIN_PATH', None)
    os.environ.pop('LD_LIBRARY_PATH', None)   # Linux/macOS 下动态库搜索路径
    # 移除进程环境中的代理变量，防止 QtWebEngine (Chromium 子进程) 继承后
    # 将 OAuth 登录页面等 HTTPS 流量路由到 mitmproxy 导致加载失败。
    # 游戏子进程通过 mitm_proxy.get_proxy_env() 获取独立的代理配置。
    for _pvar in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "NO_PROXY", "no_proxy"):
        os.environ.pop(_pvar, None)
    # 双保险: 通过 Chromium 命令行参数显式禁用代理
    _chromium_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--no-proxy-server" not in _chromium_flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            (_chromium_flags + " " if _chromium_flags else "") + "--no-proxy-server"
        )
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtNetwork import QNetworkProxyFactory, QNetworkProxy
    from uimgr import (
        configure_application_icon,
        configure_windows_app_identity,
        register_url_scheme,
    )
    # Register custom URL schemes before creating QApplication
    register_url_scheme()
    configure_windows_app_identity()

    argv = sys.argv if sys.argv else ["idv-login"]
    app = QApplication(argv)
    configure_application_icon(app)
    app_state.app = app
    # 初始化主线程调度器，必须在 app 设置后、其他线程启动前调用
    app_state._ensure_invoker()

    # 关闭所有窗口后不退出应用 —— 本工具是后台代理服务，应持续运行。
    app.setQuitOnLastWindowClosed(False)
    # 标记主事件循环即将运行，让 WebBrowser.run() 使用局部 QEventLoop
    # 而非再次调用 app.exec()。否则登录窗口关闭时 cleanup() 会调用
    # app.quit() 导致整个应用退出，管理页面无法收到登录结果。
    app.setProperty("_main_loop_running", True)

    # 显式告知 Qt (及其内嵌 Chromium) 不使用任何代理。
    # Qt 文档: "If QNetworkProxy::applicationProxy is set, it will also be
    # used for Qt WebEngine." 这是控制 Chromium 代理行为的官方 API。
    QNetworkProxyFactory.setUseSystemConfiguration(False)
    QNetworkProxy.setApplicationProxy(
        QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
    )

    if genv.get(f"{genv.get('VERSION')}_first_use",True):
        # 记录安装根目录
        record_install_root()
        #该版本首次使用会弹出教程
        #import webbrowser
        #url=CloudRes().get_guideUrl()
        #genv.set("httpdns_blocking_enabled",False,True)
        #webbrowser.open(url)
        genv.set(f"{genv.get('VERSION')}_first_use",False,True)
        from hostmgr import hostmgr
        try:
            h_mgr=hostmgr()
            if h_mgr.isExist(genv.get("DOMAIN_TARGET")):
                logger.warning(f"Hosts文件中已存在{genv.get('DOMAIN_TARGET')}的记录，正在尝试删除旧记录...")
                h_mgr.remove(genv.get("DOMAIN_TARGET"))
            if h_mgr.isExist(genv.get("DOMAIN_TARGET_OVERSEA")):
                logger.warning(f"Hosts文件中已存在{genv.get('DOMAIN_TARGET_OVERSEA')}的记录，正在尝试删除旧记录...")
                h_mgr.remove(genv.get("DOMAIN_TARGET_OVERSEA"))
        except Exception as e:
            logger.error(f"删除可能存在的旧Hosts记录失败: {e}")
        
        # v6.0.0 新增：首次启动时清理孤立的 weblogin profile/cache 文件夹
        try:
            from channelmgr import ChannelManager
            channel_mgr = ChannelManager()
            channel_mgr.cleanup_orphaned_weblogin_profiles()
        except Exception as e:
            logger.error(f"清理孤立 weblogin profile/cache 失败: {e}")
        
        #from gamemgr import GameManager
        #try:
        #    game_mgr = GameManager()
        #    for game in game_mgr.games.values():
        #        logger.info(f"新建快捷方式: {game.path}")
        #        game.create_tool_launch_shortcut(game.path or "")
                
        #except Exception as e:
        #    logger.error(f"首次使用创建快捷方式失败: {e}")
            
    try:
        setup_shortcuts()
    except:
        logger.error("创建快捷方式失败")
    from run_once import run_once
    try:
        run_once()
    except Exception as e:
        logger.error(f"运行一次性任务失败: {e}")
    #如果是windows，清空DNS缓存
    #if sys.platform=='win32':
    #    subprocess.call(
    #        "ipconfig /flushdns",
    #        shell=True,
    #        stdout=subprocess.DEVNULL,
    #        stderr=subprocess.DEVNULL,
    #    )

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
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        global _console_ctrl_handler
        _console_ctrl_handler = HandlerRoutine(ctrl_handler)
        kernel32.SetConsoleCtrlHandler(_console_ctrl_handler, True)
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), (0x4|0x80|0x20|0x2|0x10|0x1|0x00|0x100))
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING belongs to the output handles.
        # Preserve their existing modes and only add ANSI support.
        for std_handle_id in (-11, -12):
            output_handle = kernel32.GetStdHandle(std_handle_id)
            output_mode = ctypes.c_uint()
            if output_handle and kernel32.GetConsoleMode(
                output_handle, ctypes.byref(output_mode)
            ):
                kernel32.SetConsoleMode(output_handle, output_mode.value | 0x0004)
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


def _download_core_log_path(game_id="", installation_id=""):
    log_dir = os.path.join(
        genv.get("FP_WORKDIR", os.getcwd()),
        "download-core-logs",
    )
    os.makedirs(log_dir, exist_ok=True)
    identity = f"{game_id}_{installation_id}" or "unknown"
    safe_identity = "".join(
        char if char.isalnum() or char in ("-", "_") else "_"
        for char in identity
    ).strip("_") or "unknown"
    stamp = int(time.time() * 1000)
    return os.path.join(
        log_dir,
        f"download_core_{safe_identity}_{stamp}_{os.getpid()}.log",
    )


_download_status_lock = threading.Lock()


def _write_download_status(status_file, values):
    if not status_file:
        return
    try:
        with _download_status_lock:
            current = {}
            try:
                with open(status_file, "r", encoding="utf-8") as status_handle:
                    current = json.load(status_handle)
            except (FileNotFoundError, OSError, ValueError, TypeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            current.update(values)
            current["updated_at"] = int(time.time())
            temp_file = status_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as status_handle:
                json.dump(current, status_handle, ensure_ascii=False)
            os.replace(temp_file, status_file)
    except Exception:
        # Progress reporting must never terminate the download itself.
        pass

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
    game_id = task_data.get("game_id", "")
    installation_id = task_data.get("installation_id", "")
    version_code = task_data.get("version_code", "")
    distribution_id = int(task_data.get("distribution_id", -1))
    content_id = task_data.get("content_id")
    original_version = task_data.get("original_version", "")
    repair_list_path = task_data.get("repair_list_path", "")
    progress_file = task_data.get("progress_file", "")
    control_file = task_data.get("control_file", "")
    start_args = task_data.get("start_args", "")
    result = True
    ui_server_process = None
    ui_server_thread = None
    stop_event = None
    download_process = None
    use_download_ipc = bool(content_id) and distribution_id != -1 and download_root
    core_error = {"value": ""}
    _write_download_status(progress_file, {
        "status": "pending",
        "phase": "preparing",
        "progress_percent": 0,
    })
    try:
        if use_download_ipc:
            from download_binary import ensure_binary, allocate_ipc_ports
            import download_binary
            import threading
            if not ensure_binary():
                result = False
            else:
                heartbeat_port, progress_port = allocate_ipc_ports()
                stop_event = threading.Event()
                topic_bytes = str(content_id).encode("utf-8") if content_id else b""

                def publish_progress(event):
                    if event.get("success") is False:
                        core_error["value"] = str(event.get("error") or "下载失败")
                    _write_download_status(progress_file, event)

                ui_server_thread = threading.Thread(
                    target=download_binary.main_ui_server,
                    kwargs={
                        "topic": topic_bytes,
                        "sub_port": heartbeat_port,
                        "pub_port": progress_port,
                        "stop_event": stop_event,
                        "on_event": publish_progress,
                        "control_file": control_file,
                    }
                )
                ui_server_thread.daemon = True
                ui_server_thread.start()
                #等待几秒钟，确保UI服务器启动完成
                import time
                time.sleep(5)
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
                    f"--subport:{heartbeat_port}",
                    f"--pubport:{progress_port}",
                    f"--path:{encoded_path}",
                    f"--repairListPath:{encoded_repair_list_path}",
                ]
                if version_code:
                    download_cmd.append(f"--targetVersion:{version_code}")
                if original_version:
                    download_cmd.append(f"--originVersion:{original_version}")
                else:
                    download_cmd.append(f"--originVersion:")
                core_log_path = _download_core_log_path(game_id, installation_id)
                _write_download_status(progress_file, {
                    "core_log_path": core_log_path,
                })
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if sys.platform == "win32"
                    else 0
                )
                logger_local.info(f"下载核心输出日志: {core_log_path}")
                with open(core_log_path, "ab", buffering=0) as core_log:
                    download_process = subprocess.Popen(
                        download_cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=core_log,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                    exit_code = download_process.wait()
                result = exit_code == 0 and not core_error["value"]
        if result and game_id and version_code:
            from gamemgr import GameManager
            game_mgr = GameManager()
            game = game_mgr.get_game(game_id)
            installation = game.get_installation(installation_id) if game else None
            if game and installation:
                if installation.distribution_id not in (-1, distribution_id):
                    logger_local.error(
                        "下载结果与安装记录分发不匹配，拒绝回写: "
                        f"installation={installation.distribution_id}, task={distribution_id}"
                    )
                    result = False
                else:
                    installation.installed_version = version_code
                    installation.distribution_id = distribution_id
                    installation.content_id = content_id
                    installation.source = "download"
                    installation.startup_args = start_args or installation.startup_args
                    installation.updated_at = int(time.time())
                    if not installation.write_marker(game_id):
                        logger_local.warning("下载完成，但写入安装标记文件失败")
                    game.default_installation_id = installation.installation_id
                    game.should_auto_start = True
                    game_mgr._save_games()
                    print(f"下载任务完成，准备创建游戏启动快捷方式，启动参数: {start_args}")
                    game.create_tool_launch_shortcut(
                        installation.path, installation.installation_id
                    )
            elif game:
                logger_local.error(
                    f"下载完成但安装记录不存在，拒绝覆盖其他安装: {installation_id}"
                )
                result = False

                
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
        if control_file:
            try:
                os.remove(control_file)
            except FileNotFoundError:
                pass
            except Exception as e:
                logger_local.exception(f"删除下载控制文件失败: {e}")
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
        _write_download_status(progress_file, {
            "status": "done",
            "success": bool(result),
            "phase": "finished" if result else "failed",
            "progress_percent": 100 if result else 0,
            "error": "" if result else (core_error["value"] or "下载核心异常退出"),
            "game_id": game_id,
            "installation_id": installation_id,
            "distribution_id": distribution_id,
            "target_version": version_code,
            "content_id": content_id,
        })
        return result
    except Exception as e:
        logger_local.exception(f"下载任务执行失败: {e}")
        _write_download_status(progress_file, {
            "status": "done",
            "success": False,
            "phase": "failed",
            "error": str(e),
        })
    finally:
        from gamemgr import Game
        game=Game(game_id=game_id,path=os.path.join(download_root,"dummy.exe"))

def cleanup_expired_certificates():
    """清理过期的证书文件"""
    from mitm_proxy import MitmProxyManager

    # 检查证书是否过期
    web_cert_expired = m_certmgr.is_certificate_expired(genv.get("FP_WEBCERT"))
    ca_cert_expired = m_certmgr.is_certificate_expired(genv.get("FP_CACERT"))

    if web_cert_expired or ca_cert_expired:
        logger.info("一个或多个证书已过期或不存在，正在重新生成...")
        confdir = MitmProxyManager.get_confdir()
        # 删除旧证书文件（如果存在）
        cert_files = [
            (genv.get("FP_WEBCERT"), "网站证书"),
            (genv.get("FP_WEBKEY"), "网站密钥"),
            (genv.get("FP_CACERT"), "CA证书"),
            (os.path.join(confdir, "mitmproxy-ca.pem"), "mitmproxy CA密钥+证书"),
            (os.path.join(confdir, "mitmproxy-ca-cert.pem"), "mitmproxy CA证书"),
        ]
        
        for cert_path, cert_name in cert_files:
            if cert_path and os.path.exists(cert_path):
                os.remove(cert_path)
                logger.info(f"已删除旧的{cert_name}: {cert_path}")
    
    return web_cert_expired, ca_cert_expired


def generate_certificates_if_needed():
    """检查并生成必要的证书文件, 同时为 mitmproxy 准备 CA 密钥+证书。

    CA 私钥保存在 mitmproxy confdir 内部的 ``mitmproxy-ca.pem`` 中，
    并通过文件系统权限限制只允许当前用户读取。公钥证书导出到
    ``FP_CACERT`` 并导入系统根证书存储。
    """
    from mitm_proxy import MitmProxyManager
    from cryptography.hazmat.primitives import serialization

    web_cert_expired, ca_cert_expired = cleanup_expired_certificates()

    confdir = MitmProxyManager.get_confdir()
    os.makedirs(confdir, exist_ok=True)
    mitm_ca_pem = os.path.join(confdir, "mitmproxy-ca.pem")
    mitm_ca_cert_pem = os.path.join(confdir, "mitmproxy-ca-cert.pem")

    need_regen = (
        not os.path.exists(genv.get("FP_CACERT"))
        or not os.path.exists(mitm_ca_pem)
        or not os.path.exists(mitm_ca_cert_pem)
        or web_cert_expired
        or ca_cert_expired
    )

    if need_regen:
        logger.info("正在生成必要的证书文件...")

        ca_key = m_certmgr.generate_private_key(bits=2048)
        ca_cert = m_certmgr.generate_ca(ca_key)
        m_certmgr.export_cert(genv.get("FP_CACERT"), ca_cert)

        # ── 保存 CA 私钥+证书供 mitmproxy 使用 ──
        key_pem = ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)

        # mitmproxy-ca.pem = key + cert (mitmproxy 需要)
        # 使用 os.open 以限制权限创建文件 (避免短暂的权限窗口)
        from secure_write import write_file_restricted
        write_file_restricted(mitm_ca_pem, key_pem + cert_pem)
        # mitmproxy-ca-cert.pem = cert only (公钥，无需限制权限)
        with open(mitm_ca_cert_pem, "wb") as f:
            f.write(cert_pem)

        # ── 导入 CA 证书到系统根证书存储 ──
        if m_certmgr.import_to_root(genv.get("FP_CACERT")) == False:
            logger.error("导入CA证书失败!")
            if sys.platform == 'win32':
                os.system("pause")
            else:
                input("导入CA证书失败，请按回车键退出。")
            sys.exit(-1)

        # ── 生成服务器证书 (仍用于某些内部流程) ──
        srv_key = m_certmgr.generate_private_key(bits=2048)
        srv_cert = m_certmgr.generate_cert(
            [*_configured_target_domains(), "localhost"], srv_key, ca_cert, ca_key,
        )
        m_certmgr.export_cert(genv.get("FP_WEBCERT"), srv_cert)
        m_certmgr.export_key(genv.get("FP_WEBKEY"), srv_key)
        logger.info("证书初始化成功!")
    else:
        logger.info("证书已存在且有效，跳过生成。")


def setup_shortcuts():
    """设置快捷方式"""
    from shortcutmgr import ShortcutMgr
    shortcutMgr_instance = ShortcutMgr()
    shortcutMgr_instance.handle_shortcuts()

def setup_network_proxy(proxy_port):
    """Set up the mitmproxy-based network proxy.

    Supports three modes:
    - "global": Sets system-level HTTP_PROXY/HTTPS_PROXY environment variables
    - "process": Sets proxy variables only for game subprocess
    - "compat": Uses DNS hijacking (NRPT/Hosts) + local DNS server + mitmproxy reverse proxy

    In compat mode, the workflow is:
    1. Set up DNS policy (NRPT rules or Hosts file) to redirect target domains to 127.0.0.1
    2. Start local DNS server on 127.0.0.1:53 to respond with 127.0.0.1 for target domains
    3. Start mitmproxy in reverse proxy mode on port 443 to handle incoming HTTPS requests
    """
    global m_proxy, _dns_policy_mgr, _dns_server

    from gamemgr import GameManager
    from channelHandler.channelUtils import getShortGameId
    from cloudRes import CloudRes

    game_helper = GameManager()
    ui_logger = logger

    # Platform-specific defaults
    if sys.platform == "darwin":
        cv = "a5.10.0"
        login_style = 2
        app_channel_default = "netease.wyzymnqsd_cps_dev"
        use_login_mapping_always = False

        qrcode_app_channel_provider = CloudRes().get_qrcode_app_channel

        _CREATE_LOGIN_WHITELIST = frozenset({
            "app_channel", "qrcode_channel_type", "gv", "gvn", "cv", "sv",
            "app_type", "app_mode", "_cloud_extra_base64", "sc",
        })

        def _create_login_query_hook(query, game_id):
            config = CloudRes().get_qrcode_login_config(game_id)
            if config:
                for k, v in config.items():
                    if k in _CREATE_LOGIN_WHITELIST:
                        query[k] = str(v)
    else:
        cv = "a5.10.0"
        login_style = 1
        app_channel_default = "netease.wyzymnqsd_cps_dev"
        use_login_mapping_always = False

        qrcode_app_channel_provider = CloudRes().get_qrcode_app_channel

        _CREATE_LOGIN_WHITELIST = frozenset({
            "app_channel", "qrcode_channel_type", "gv", "gvn", "cv", "sv",
            "app_type", "app_mode", "_cloud_extra_base64", "sc",
        })

        def _create_login_query_hook(query, game_id):
            config = CloudRes().get_qrcode_login_config(game_id)
            if config:
                for k, v in config.items():
                    if k in _CREATE_LOGIN_WHITELIST:
                        query[k] = str(v)

    # Create the UI manager for the Qt window
    from uimgr import UIManager
    ui_mgr = UIManager(game_helper=game_helper, ui_logger=ui_logger)
    app_state.ui_mgr = ui_mgr
    game_helper.start_fever_auto_import()

    # Create the mitmproxy addon
    from mitm_addon import IDVLoginAddon
    addon = IDVLoginAddon(
        cv=cv,
        login_style=login_style,
        game_helper=game_helper,
        logger=logger,
        app_channel_default=app_channel_default,
        qrcode_app_channel_provider=qrcode_app_channel_provider,
        create_login_query_hook=_create_login_query_hook,
        use_login_mapping_always=use_login_mapping_always,
        ui_manager=ui_mgr,
    )

    # 获取代理模式
    uri_action = genv.get("URI_STARTUP_ACTION", "")
    uri_game_id = genv.get("URI_STARTUP_GAME_ID", "")
    uri_installation_id = genv.get("URI_STARTUP_INSTALLATION_ID", "")
    uri_initial_view = genv.get("URI_STARTUP_VIEW", "")
    auto_games = game_helper.list_auto_start_games()
    proxy_mode = genv.get("proxy_mode", "")
    if not proxy_mode:
        proxy_mode = "compat"
        genv.set("proxy_mode", "compat", True)

    # 兼容模式特殊处理
    if proxy_mode == "compat":
        # 兼容模式不使用代理环境变量，清理上次可能残留的设置
        try:
            _unset_proxy()
        except Exception:
            pass
        logger.info("正在启动兼容模式...")
        try:
            _setup_compat_mode(addon)
        except Exception as e:
            logger.error(f"兼容模式启动失败: {e}，回退到常规代理模式")
            # 清理可能已部分设置的资源
            if _dns_policy_mgr:
                try:
                    _dns_policy_mgr.cleanup()
                except Exception:
                    pass
                _dns_policy_mgr = None
            if _dns_server:
                try:
                    _dns_server.stop()
                except Exception:
                    pass
                _dns_server = None
            from mitm_proxy import clear_custom_dns
            clear_custom_dns()
            # 回退到常规模式（不持久化，下次启动仍尝试兼容模式）
            proxy_mode = "process" if auto_games else "global"
            from mitm_proxy import MitmProxyManager
            proxy_mgr = MitmProxyManager(addon=addon, port=proxy_port, mode="regular")
            proxy_mgr.start()
            m_proxy = proxy_mgr
            app_state.proxy_mgr = proxy_mgr
    else:
        # 常规代理模式（global 或 process）
        from mitm_proxy import MitmProxyManager
        proxy_mgr = MitmProxyManager(addon=addon, port=proxy_port, mode="regular")
        proxy_mgr.start()
        m_proxy = proxy_mgr
        app_state.proxy_mgr = proxy_mgr

    # Register the URI scheme so QR code redirects open our Qt window
    from uri_scheme import register_uri_scheme, start_uri_listener
    register_uri_scheme()

    # Start the URI listener so that new --uri invocations signal this instance
    def _on_uri_signal(
        action: str,
        game_id: str,
        installation_id: str = "",
        initial_view: str = "",
    ):
        logger.info(
            f"收到 URI 信号: action={action}, game_id={game_id}, "
            f"installation_id={installation_id}, view={initial_view}"
        )
        if action == "fever-channel-accounts":
            bridge = app_state.fever_bridge
            target_game_id = game_id or getattr(
                bridge, "active_target_game_id", ""
            )
            ui_mgr.open_for_game(target_game_id, "accounts")
        elif action == "start" and game_id:
            game = game_helper.get_game(game_id)
            if game:
                logger.info(f"通过信号启动游戏: {game.name or game_id}")
                game.start(installation_id)
            else:
                logger.warning(f"信号指定的游戏未找到: {game_id}")
        else:
            ui_mgr.open_for_game(game_id, initial_view)

    start_uri_listener(_on_uri_signal)

    # If we were launched via --uri / --open-ui, open the UI
    if genv.get("URI_STARTUP_OPEN_UI"):
        startup_game_id = genv.get("URI_STARTUP_GAME_ID", "")
        ui_mgr.open_for_game(startup_game_id, uri_initial_view)
        genv.set("URI_STARTUP_OPEN_UI", "")
        genv.set("URI_STARTUP_VIEW", "")

    # 根据模式执行不同的启动逻辑
    if proxy_mode == "compat":
        # 兼容模式：无需设置代理环境变量，DNS 劫持会自动生效
        logger.info("提示：当前使用兼容模式，通过 DNS 劫持拦截游戏流量。")
        if uri_action == "start" and uri_game_id:
            game = game_helper.get_game(uri_game_id)
            if game:
                logger.info(f"通过快捷方式启动游戏: {game.name or uri_game_id}")
                game.start(uri_installation_id)
            else:
                logger.warning(f"未找到游戏: {uri_game_id}")
            genv.set("URI_STARTUP_ACTION", "")
            genv.set("URI_STARTUP_GAME_ID", "")
            genv.set("URI_STARTUP_INSTALLATION_ID", "")
            genv.set("URI_STARTUP_VIEW", "")
        elif auto_games:
            names = ", ".join(g.name for g in auto_games)
            logger.info(f"同时启动自启游戏: {names}")
            for g in auto_games:
                installation = g.get_auto_start_installation()
                g.start(installation.installation_id if installation else "")
    elif uri_action == "start" and uri_game_id:
        # 最高优先级：快捷方式启动（进程级代理），仅启动指定游戏
        game = game_helper.get_game(uri_game_id)
        if game:
            logger.info(f"通过快捷方式启动游戏: {game.name or uri_game_id}")
            game.start(uri_installation_id)
        else:
            logger.warning(f"未找到游戏: {uri_game_id}")
        # 清理启动参数
        genv.set("URI_STARTUP_ACTION", "")
        genv.set("URI_STARTUP_GAME_ID", "")
        genv.set("URI_STARTUP_INSTALLATION_ID", "")
        genv.set("URI_STARTUP_VIEW", "")
    elif proxy_mode == "global":
        # 次优先级：全局模式 → 设置系统/用户级代理
        _set_proxy(proxy_port)
        logger.info("提示：当前使用全局代理模式，会处理本机所有网络连接。")
        if auto_games:
            # 全局模式下也启动自启游戏（它们会继承代理环境变量）
            names = ", ".join(g.name for g in auto_games)
            logger.info(f"同时启动自启游戏: {names}")
            for g in auto_games:
                installation = g.get_auto_start_installation()
                g.start(installation.installation_id if installation else "")
        else:
            logger.info("如果出现其他程序无法联网问题，可在管理页面切换为进程代理模式。")
    elif auto_games:
        # 第三优先级：进程模式但有自启游戏 → 启动自启游戏（进程级代理）
        names = ", ".join(g.name for g in auto_games)
        logger.info(f"进程代理模式，启动自启游戏: {names}")
        for g in auto_games:
            installation = g.get_auto_start_installation()
            g.start(installation.installation_id if installation else "")
    else:
        # 进程代理模式且没有自启游戏
        logger.info("当前使用进程代理模式，请通过桌面快捷方式启动游戏。")
        logger.info("如需设置全局代理，可在管理页面切换为全局模式。")

    if proxy_mode == "compat":
        logger.info("兼容模式已就绪！DNS 劫持和反向代理已启动。")
    else:
        logger.info(f"mitmproxy 代理模式已就绪！监听端口: {proxy_port}")
    logger.info("拦截成功，您现在可以打开游戏了。游戏将通过代理自动路由。")
    logger.warning("如果您在之前已经打开了游戏，请关闭游戏后重新打开，否则工具不会生效！")
    logger.info("登入账号且已经··进入游戏··后，您可以关闭本工具。")


def _setup_compat_mode(addon):
    """设置兼容模式：DNS 劫持 + 本地 DNS 服务器 + 反向代理。"""
    global m_proxy, _dns_policy_mgr, _dns_server

    # 目标域名
    target_domains = _configured_target_domains()

    # 0. 检测并处理 443 端口占用
    _check_and_handle_port_443()

    # 1. 并行预解析目标域名的真实 IP（防止 DNS 回环）
    from mitm_proxy import resolve_domain_ip, add_custom_dns
    from concurrent.futures import ThreadPoolExecutor, as_completed

    resolved_ips = {}
    with ThreadPoolExecutor(max_workers=len(target_domains)) as executor:
        futures = {
            executor.submit(resolve_domain_ip, domain, True): domain
            for domain in target_domains
        }
        for future in as_completed(futures, timeout=15):
            domain = futures[future]
            try:
                ip = future.result()
                if ip:
                    resolved_ips[domain] = ip
                    add_custom_dns(domain, 443, ip)
                    logger.debug(f"目标服务器: {domain} -> {ip}")
                else:
                    logger.warning(f"无法解析 {domain}，兼容模式可能无法正常工作")
            except Exception as e:
                logger.warning(f"解析 {domain} 失败: {e}")

    if not resolved_ips:
        logger.error("所有目标域名解析失败，无法启动兼容模式")
        raise RuntimeError("Failed to resolve any target domain for compat mode")

    # 保存解析结果供其他模块使用
    genv.set("COMPAT_RESOLVED_IPS", resolved_ips)

    # 2. 设置 DNS 策略（NRPT 或 Hosts）
    from mitm_proxy import DnsPolicyManager
    _dns_policy_mgr = DnsPolicyManager(domains=target_domains, target_ip="127.0.0.1")
    if not _dns_policy_mgr.setup():
        logger.error("DNS 策略设置失败，无法启动兼容模式")
        raise RuntimeError("Failed to setup DNS policy for compat mode")

    method = "NRPT" if _dns_policy_mgr.is_using_nrpt else "Hosts 文件"
    logger.debug(f"DNS 策略已设置 (方式: {method})")

    # 3. 启动本地 DNS 服务器。Hosts 文件方式已经直接写入解析结果，
    # 无需再占用 53 端口。
    if _dns_policy_mgr.is_using_nrpt:
        from mitm_proxy import LocalDnsServer
        _dns_server = LocalDnsServer(
            intercept_domains=set(target_domains),
            target_ip="127.0.0.1",
            listen_host="127.0.0.1",
            listen_port=53,
        )
        if not _dns_server.start():
            logger.error("本地 DNS 服务器启动失败")
            _dns_policy_mgr.cleanup()
            _dns_policy_mgr = None
            raise RuntimeError("Failed to start local DNS server for compat mode")

        logger.debug("本地 DNS 服务器已启动 (127.0.0.1:53)")
    else:
        _dns_server = None
        logger.debug("Hosts 文件方式无需启动本地 DNS 服务器")

    # 4. 启动 mitmproxy 反向代理
    from mitm_proxy import MitmProxyManager
    proxy_mgr = MitmProxyManager(addon=addon, mode="compat")
    proxy_mgr.start()
    m_proxy = proxy_mgr
    app_state.proxy_mgr = proxy_mgr


def _ask_user_confirmation(title: str, message: str) -> bool:
    """向用户询问确认，优先使用 Qt 对话框，回退到 Windows API，最后使用控制台。
    
    Args:
        title: 对话框标题
        message: 对话框消息
        
    Returns:
        用户是否确认（是/否）
    """
    # 1. 尝试使用 Qt 对话框（如果已初始化）
    try:
        if app_state.app is not None:
            from PyQt6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                None, title, message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            return reply == QMessageBox.StandardButton.Yes
    except Exception:
        pass
    
    # 2. 尝试使用 Windows API（仅 Windows）
    if sys.platform == "win32":
        try:
            import ctypes
            MB_YESNO = 0x04
            MB_ICONQUESTION = 0x20
            IDYES = 6
            result = ctypes.windll.user32.MessageBoxW(
                0, message, title, MB_YESNO | MB_ICONQUESTION
            )
            return result == IDYES
        except Exception:
            pass
    
    # 3. 回退到控制台输入（回车为肯定）
    print(f"\n{title}")
    print(message)
    user_input = input("按回车键确认，输入其他内容取消: ")
    return user_input == ""


def _check_and_handle_port_443():
    """检测并处理 443 端口占用。"""
    import psutil

    def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return False
            except socket.error:
                return True

    if not is_port_in_use(443):
        return

    logger.warning("检测到 443 端口被占用，正在查找占用进程...")

    # 查找占用 443 端口的进程
    occupying_pids = []
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in result.stdout.splitlines():
                if ":443 " in line and "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pid = parts[-1]
                        if pid.isdigit():
                            occupying_pids.append(int(pid))
        except Exception as e:
            logger.warning(f"查找占用进程失败: {e}")
    else:
        try:
            result = subprocess.run(
                ["lsof", "-i", ":443", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=10
            )
            for line in result.stdout.splitlines()[1:]:  # Skip header
                parts = line.split()
                if len(parts) >= 2:
                    pid = parts[1]
                    if pid.isdigit():
                        occupying_pids.append(int(pid))
        except Exception as e:
            logger.warning(f"查找占用进程失败: {e}")

    if not occupying_pids:
        logger.warning("无法确定占用 443 端口的进程，请手动关闭后重试")
        _ask_user_confirmation("端口占用", "无法确定占用 443 端口的进程。\n请手动关闭相关进程后按确认继续。")
        return

    # 获取进程信息并提示用户
    for pid in set(occupying_pids):
        try:
            proc = psutil.Process(pid)
            exe_name = proc.exe()
            logger.warning(f"进程 {exe_name} (PID={pid}) 正在占用 443 端口")

            # Windows PID 4 是 System 进程（HTTP.sys 服务）
            if sys.platform == "win32" and pid == 4:
                logger.warning("这是 Windows HTTP 服务 (PID=4)，将尝试停止 http 服务")
                if _ask_user_confirmation(
                    "停止 Windows HTTP 服务",
                    "Windows HTTP 服务 (PID=4) 正在占用 443 端口。\n"
                    "这通常由 IIS、SQL Server Reporting Services 等引起。\n\n"
                    "是否停止 Windows HTTP 服务？"
                ):
                    try:
                        subprocess.run(
                            ["net", "stop", "http", "/y"],
                            check=True, timeout=30,
                            capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        logger.info("HTTP 服务已停止")
                        time.sleep(2)
                    except Exception as e:
                        logger.error(f"停止 HTTP 服务失败: {e}")
            else:
                if _ask_user_confirmation(
                    "终止占用端口的进程",
                    f"进程 {exe_name}\n(PID={pid}) 正在占用 443 端口。\n\n"
                    f"是否终止该进程？"
                ):
                    try:
                        if sys.platform == "win32":
                            subprocess.run(
                                ["taskkill", "/f", "/pid", str(pid)],
                                check=True, timeout=10,
                                capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                            )
                        else:
                            subprocess.run(["kill", "-9", str(pid)], check=True, timeout=10)
                        logger.info(f"进程 {pid} 已终止")
                        time.sleep(2)
                    except Exception as e:
                        logger.error(f"终止进程失败: {e}")
        except psutil.AccessDenied:
            logger.warning(f"无权限访问进程 {pid}，请以管理员身份运行")
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logger.warning(f"获取进程 {pid} 信息失败: {e}")


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
        sys.stdout.flush()
        handle_exit()
        # 等待足够时间以确保清理操作（NRPT 规则删除、WM_SETTINGCHANGE 广播等）完成
        time.sleep(3.0)
        import os
        os._exit(0)
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
    from console_capture import install_console_capture
    install_console_capture()
    prepare_platform_workdir()
    
    # 设置工作目录
    setup_work_directory()
    

    # Initialize logger (assign to global logger variable)
    from logutil import setup_logger
    logger = setup_logger() 

    try:
        cloudBuildInfo()
        # hotfix gate: verify config cache can be written before importing most app modules.
        # macOS PyInstaller hotfixes are loaded via an import hook, so it must be installed
        # before initialize() pulls in channel/proxy/UI modules.
        can_run_hotfix = hotfixmgr.probe_cache_write_once()
        if not can_run_hotfix:
            print("【热更新】探测到配置缓存写入失败：已跳过本次所有热更新逻辑（避免无限重启）。")
        else:
            try:
                hotfixmgr.pre_start_check_and_rollback_if_needed()
            except Exception:
                pass
            try:
                hotfixmgr.install_import_hook()
            except Exception:
                pass

        initialize() # This sets up atexit(handle_exit) among other things


        genv.set("last_run_state", "running", True)
        genv.set("last_run_state_ts", int(time.time()), True)


        # hotfix: apply if needed (may restart process)
        if can_run_hotfix:
            hotfixmgr.handle_if_needed(m_cloudres)

        welcome()
        handle_update()
        handle_announcement()
        
        # 证书管理: 生成 CA + 服务器证书并导入系统信任存储
        generate_certificates_if_needed()

        # 网络代理设置 (mitmproxy normal mode)
        proxy_port = cli_args.proxy_port
        setup_network_proxy(proxy_port)

        # setup_network_proxy → _set_proxy → _broadcast_env_change 可能导致
        # Qt 重新读取系统代理。必须在此之后再次显式设置 NoProxy。
        from PyQt6.QtNetwork import QNetworkProxy
        QNetworkProxy.setApplicationProxy(
            QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
        )
        
        # The proxy runs in a background thread.
        # Keep the main thread alive (for Qt event loop or simple wait).
        app = app_state.app
        if app is not None:
            # If a Qt application exists, run its event loop.
            # 在Qt退出前执行清理
            app.aboutToQuit.connect(handle_exit)
            app.setProperty("_main_loop_running", True)
            app.exec()
            # Qt已退出，但handle_exit可能还没完成，稍等一下
            time.sleep(0.2)
        else:
            # No Qt app – just block the main thread.
            import threading
            stop_event = threading.Event()
            try:
                stop_event.wait()
            except KeyboardInterrupt:
                pass
    except Exception as e:
        handle_error_and_exit(e)


if __name__ == "__main__":
    try:
        if CLI_ARGS.download:
            success = handle_download_task(CLI_ARGS.download)
            sys.exit(0 if success else 1)
        # --open-ui 是 --uri "idvlogin://open" 的简写，用于快捷方式
        if CLI_ARGS.open_ui and not CLI_ARGS.uri:
            CLI_ARGS.uri = "idvlogin://open"
        if CLI_ARGS.uri:
            # URI scheme invocation (e.g. idvlogin://open?game_id=xxx or idvlogin://start?game_id=xxx)
            # 如果当前进程没有管理员权限，先提权再处理 URI
            if sys.platform == "win32" and ctypes.windll.shell32.IsUserAnAdmin() == 0:
                if getattr(sys, 'frozen', False):
                    exe = sys.argv[0]
                    args = sys.argv[1:] if len(sys.argv) > 1 else []
                    argvs = [f'"{a}"' for a in args]
                else:
                    exe = sys.executable
                    # argv[0] 转为绝对路径，避免提权后工作目录变化导致找不到脚本
                    abs_argv0 = os.path.abspath(sys.argv[0])
                    argvs = [f'"{abs_argv0}"'] + [f'"{a}"' for a in sys.argv[1:]]
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", exe, " ".join(argvs), script_dir, 1
                )
                sys.exit(0)
            # Try to signal the already-running instance via named pipe.
            from uri_scheme import parse_uri, signal_running_instance
            params = parse_uri(CLI_ARGS.uri)
            action = params.get("action", "open")
            game_id = params.get("game_id", "")
            installation_id = params.get("installation_id", "")
            initial_view = params.get("view", "")
            
            # idvlogin://start?game_id=xxx - 启动工具并仅启动该游戏（进程代理模式）
            # idvlogin://open?game_id=xxx - 打开 UI
            # idvlogin://fever-channel-accounts - 打开当前游戏的渠道账号管理
            # 两种 action 都先尝试信号已运行实例
            if signal_running_instance(
                game_id, action, installation_id, initial_view
            ):
                # 已运行实例收到信号，退出本进程
                sys.exit(0)
            # 无已运行实例，作为新实例启动
            if action == "start" and game_id:
                genv.set("URI_STARTUP_ACTION", "start")
                genv.set("URI_STARTUP_GAME_ID", game_id)
                genv.set("URI_STARTUP_INSTALLATION_ID", installation_id)
            else:
                # No running instance — fall through and start normally.
                # Store the game_id so the UI opens for it after startup.
                genv.set("URI_STARTUP_GAME_ID", game_id)
                genv.set("URI_STARTUP_VIEW", initial_view)
                genv.set("URI_STARTUP_OPEN_UI", "1")
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
    main(CLI_ARGS)
