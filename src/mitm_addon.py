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

import json
import os
import re
import sys
import threading
import time

import app_state
from mitmproxy import http


LOGIN_METHODS = [
    {
        "name": "手机账号",
        "icon_url": "",
        "text_color": "",
        "hot": True,
        "type": 7,
        "icon_url_large": "",
    },
    {
        "name": "快速游戏",
        "icon_url": "",
        "text_color": "",
        "hot": True,
        "type": 2,
        "icon_url_large": "",
    },
    {
        "login_url": "",
        "name": "网易邮箱",
        "icon_url": "",
        "text_color": "",
        "hot": True,
        "type": 1,
        "icon_url_large": "",
    },
    {
        "login_url": "",
        "name": "扫码登录",
        "icon_url": "",
        "text_color": "",
        "hot": True,
        "type": 17,
        "icon_url_large": "",
    },
]

PC_INFO = {
    "extra_unisdk_data": "",
    "from_game_id": "h55",
    "src_app_channel": "netease",
    "src_client_ip": "",
    "src_client_type": 1,
    "src_jf_game_id": "h55",
    "src_pay_channel": "netease",
    "src_sdk_version": "3.15.0",
    "src_udid": "",
}


class IDVLoginAddon:
    """mitmproxy addon that intercepts and modifies game API traffic.

    Replaces the Flask-based proxy handlers (proxymgr.py / macProxyMgr.py)
    and the route registrations in common_mpay_routes.py.

    Also handles /_idv-login/* routes inline so that the game's built-in
    WebView can display the account management UI without a separate
    HTTPS server on port 443.
    """

    def __init__(
        self,
        *,
        cv,
        login_style,
        game_helper,
        logger,
        app_channel_default="netease.wyzymnqsd_cps_dev",
        qrcode_app_channel_provider=None,
        create_login_query_hook=None,
        use_login_mapping_always=False,
        ui_manager=None,
    ):
        from envmgr import genv
        from login_stack_mgr import LoginStackManager
        from cloudRes import CloudRes

        self.cv = cv
        self.login_style = login_style
        self.game_helper = game_helper
        self.logger = logger
        self.app_channel_default = app_channel_default
        self.qrcode_app_channel_provider = qrcode_app_channel_provider
        self.create_login_query_hook = create_login_query_hook
        self.use_login_mapping_always = use_login_mapping_always
        self.ui_manager = ui_manager

        self.genv = genv
        self.stack_mgr = LoginStackManager.get_instance()
        self.cloud_res = CloudRes

        self.oversea_domain = str(
            genv.get("DOMAIN_TARGET_OVERSEA", "sdk-os.mpsdk.easebar.com")
        ).strip().lower()
        self.target_domains = {
            str(genv.get("DOMAIN_TARGET", "service.mkey.163.com")).lower(),
            self.oversea_domain,
        }

        # Regex patterns for route matching
        self._re_login_methods = re.compile(r"^/mpay/games/([^/]+)/login_methods$")
        self._re_first_login = re.compile(
            r"^/mpay/api/users/login/mobile/(finish|get_sms|verify_sms)$"
        )
        self._re_device_users_post = re.compile(
            r"^/mpay/games/([^/]+)/devices/([^/]+)/users$"
        )
        self._re_handle_login = re.compile(
            r"^/mpay/games/([^/]+)/devices/([^/]+)/users/([^/]+)$"
        )

    # ------------------------------------------------------------------
    # mitmproxy hooks
    # ------------------------------------------------------------------

    def request(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if host not in self.target_domains:
            return

        path = flow.request.path.split("?")[0]

        # ── _idv-login routes: handle locally, do NOT forward upstream ──
        if path.startswith("/_idv-login/"):
            self._handle_idv_login_request(flow, path)
            return

        # Overseas MPay uses a different API namespace.  Keep its
        # requests untouched; only pc/config is intentionally patched
        # on the response side below.
        if host == self.oversea_domain:
            return

        # ── Game API routes: may modify query before forwarding ──
        if path == "/mpay/api/qrcode/create_login":
            self._modify_create_login_request(flow)
        elif path in (
            "/mpay/api/users/login/mobile/finish",
            "/mpay/api/users/login/mobile/get_sms",
            "/mpay/api/users/login/mobile/verify_sms",
        ):
            flow.request.query["cv"] = self.cv
        elif self._re_device_users_post.match(path) and flow.request.method == "POST":
            flow.request.query["cv"] = self.cv
        elif self._re_handle_login.match(path) and flow.request.method == "GET":
            self._modify_handle_login_request(flow)
        elif path in ("/mpay/api/qrcode/image",):
            pass  # no request modification needed
        elif path == "/mpay/games/pc_config":
            if flow.request.query.get("game_id", "") != "aecglf6ee4aaaarz-g-a50":
                flow.request.query["cv"] = self.cv
        elif path == "/mpay/api/users/login/qrcode/exchange_token":
            pass  # handled in response
        elif path == "/mpay/api/qrcode/query":
            pass  # handled in response
        elif path == "/mpay/api/data/upload":
            pass  # handled in response
        elif not path.startswith("/mpay/api/qrcode/") and not path.startswith("/mpay/api/reverify/"):
            # Global catch-all: add CV
            flow.request.query["cv"] = self.cv

    def response(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if host not in self.target_domains:
            return

        path = flow.request.path.split("?")[0]

        # ── _idv-login routes are fully handled in request() ──
        if path.startswith("/_idv-login/"):
            return

        try:
            if host == self.oversea_domain:
                if path == "/api/games/pc/config":
                    self._modify_oversea_config_response(flow)
                return

            if self._re_login_methods.match(path):
                self._modify_login_methods_response(flow)
            elif self._re_handle_login.match(path) and flow.request.method == "GET":
                self._modify_handle_login_response(flow)
            elif path == "/mpay/api/qrcode/image":
                self._modify_qrcode_image_response(flow)
            elif path == "/mpay/games/pc_config":
                if flow.request.query.get("game_id", "") != "aecglf6ee4aaaarz-g-a50":
                    self._modify_pc_config_response(flow)
            elif path == "/mpay/api/qrcode/create_login":
                self._modify_create_login_response(flow)
            elif path == "/mpay/api/qrcode/query":
                self._handle_qrcode_query_response(flow)
            elif path == "/mpay/api/users/login/qrcode/exchange_token":
                self._handle_exchange_token_response(flow)
            elif path == "/mpay/api/data/upload":
                self._handle_data_upload_response(flow)
        except Exception:
            self.logger.exception(f"处理响应时出错: {path}")

    # ------------------------------------------------------------------
    # Request modification helpers
    # ------------------------------------------------------------------

    def _modify_create_login_request(self, flow: http.HTTPFlow):
        query = dict(flow.request.query)
        game_id = query.get("game_id", "")
        if self.create_login_query_hook:
            self.create_login_query_hook(query, game_id)
            flow.request.query.update(query)

    def _modify_handle_login_request(self, flow: http.HTTPFlow):
        mapping = {
            "opt_fields": "nickname,avatar,realname_status,mobile_bind_status,exit_popup_info,mask_related_mobile,related_login_status,detect_is_new_user",
            "verify_status": "1",
            "login_for": "1",
            "gv": "251881013",
            "gvn": "2025.0707.1013",
            "sv": "35",
            "app_type": "games",
            "app_mode": "2",
            "app_channel": self.app_channel_default,
            "_cloud_extra_base64": "e30=",
            "sc": "1",
        }

        use_mapping = self.use_login_mapping_always

        m = self._re_handle_login.match(flow.request.path.split("?")[0])
        game_id = m.group(1) if m else ""

        if self.qrcode_app_channel_provider:
            qrcode_channel = self.qrcode_app_channel_provider(game_id)
            if qrcode_channel:
                mapping["app_channel"] = qrcode_channel
                use_mapping = True

        if use_mapping:
            flow.request.query["cv"] = self.cv
            for k, v in mapping.items():
                flow.request.query[k] = v
        else:
            flow.request.query["cv"] = self.cv

    # ------------------------------------------------------------------
    # Response modification helpers
    # ------------------------------------------------------------------

    def _modify_login_methods_response(self, flow: http.HTTPFlow):
        try:
            data = json.loads(flow.response.content)
            data["entrance"] = [LOGIN_METHODS]
            data["select_platform"] = True
            data["qrcode_select_platform"] = True
            for i in data.get("config", {}):
                data["config"][i]["select_platforms"] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
            flow.response.content = json.dumps(data).encode()
        except Exception:
            pass

    def _modify_handle_login_response(self, flow: http.HTTPFlow):
        try:
            data = json.loads(flow.response.content)
            data["user"]["pc_ext_info"] = PC_INFO
            flow.response.content = json.dumps(data).encode()
        except Exception:
            pass

    def _modify_qrcode_image_response(self, flow: http.HTTPFlow):
        try:
            wm_text = self.cloud_res().get_risk_wm()
            if wm_text:
                from riskWmUtils import wm
                flow.response.content = wm(flow.response.content, wm_text)
        except Exception:
            pass

    def _modify_pc_config_response(self, flow: http.HTTPFlow):
        try:
            data = json.loads(flow.response.content)
            data["game"]["config"]["cv_review_status"] = 1
            data["game"]["config"]["web_token_persist"] = True
            data["game"]["config"]["mobile_related_login"]["guide_related_mobile"] = True
            data["game"]["config"]["mobile_related_login"]["force_related_login"] = True
            data["game"]["config"]["login"]["login_style"] = self.login_style
            flow.response.content = json.dumps(data).encode()
        except Exception:
            pass

    def _modify_create_login_response(self, flow: http.HTTPFlow):
        try:
            data = json.loads(flow.response.content)
            query = dict(flow.request.query)
            game_id = query.get("game_id", "")
            process_id = query.get("process_id", "")

            self.genv.set("CHANNEL_ACCOUNT_SELECTED", "")

            qr_data = {
                "uuid": data["uuid"],
                "game_id": game_id,
            }

            # 发烧平台
            dst_jf_game_id = query.get("dst_jf_game_id", "")
            if dst_jf_game_id:
                qr_data["dst_jf_game_id"] = dst_jf_game_id
                if (not self.genv.get("has_opened_admin", False)) and (not query.get("_cloud_extra_base64", "")):
                    self.genv.set("has_opened_admin", True)
                    if self.ui_manager:
                        self.ui_manager.open_for_game(dst_jf_game_id)
                self.stack_mgr.push_cached_qrcode_data(dst_jf_game_id, process_id, qr_data)
                self.stack_mgr.ensure_pending_stack(dst_jf_game_id)
            else:
                self.stack_mgr.push_cached_qrcode_data(game_id, process_id, qr_data)
                self.stack_mgr.ensure_pending_stack(game_id)

            # Auto-login
            auto_uuid = self.genv.get(f"auto-{game_id}", "")
            if auto_uuid:
                delay = self.game_helper.get_login_delay(game_id)
                self.logger.info(f"即将自动登录，{delay}秒后开始扫码")
                self.genv.set("CHANNEL_ACCOUNT_SELECTED", auto_uuid)

                def _delayed_scan():
                    time.sleep(delay)
                    # simulate_scan 可能触发 webLogin 创建 Qt 对象，
                    # 必须在 Qt 主线程中执行
                    def _do_scan():
                        def _on_scan_complete(result):
                            # result 可能是空字典 {} (返回200但内容为空)，这也算成功
                            if result is not None and result is not False:
                                self.logger.info("自动登录成功")
                            else:
                                self.logger.warning("自动登录失败，可能需要重新授权")
                        app_state.channels_helper.simulate_scan(
                            auto_uuid, qr_data["uuid"], qr_data["game_id"],
                            on_complete=_on_scan_complete
                        )
                    app_state.run_on_main_thread(_do_scan)

                t = threading.Thread(target=_delayed_scan, daemon=True)
                t.start()

            # Change the QR code redirect URL
            # Use the idvlogin:// URI scheme so the system opens our Qt window
            uri_scheme_url = f"idvlogin://open?game_id={game_id}"
            data["scanner_guide_text"] = "已开启记住账号，可长期保存账号记录"
            data["scanner_download_guide_text"]="如果您正在为代肝/共号扫码，请注意保护账号安全，谨防诈骗"
            data["qrcode_scanners"][0]["url"] = uri_scheme_url

            flow.response.content = json.dumps(data).encode()
        except Exception:
            self.logger.exception("处理 create_login 响应失败")

    def _handle_qrcode_query_response(self, flow: http.HTTPFlow):
        if self.genv.get("CHANNEL_ACCOUNT_SELECTED"):
            return
        try:
            data = json.loads(flow.response.content)
            game_id = flow.request.query.get("game_id", "")
            process_id = flow.request.query.get("process_id", "")

            if data.get("code", -1) != -1:
                self.stack_mgr.pop_cached_qrcode_data(game_id, process_id)
                self.logger.error(
                    f"扫码登录失败，错误码：{data.get('code', -1)}，信息：{data.get('reason', '')}"
                )

            qr_status = data.get("qrcode", {}).get("status", 0)
            if qr_status == 2 and not self.genv.get("CHANNEL_ACCOUNT_SELECTED"):
                self.stack_mgr.push_pending_login_info(
                    game_id, process_id, data["login_info"]
                )
        except Exception:
            self.logger.exception("处理 qrcode/query 响应失败")

    def _handle_exchange_token_response(self, flow: http.HTTPFlow):
        is_selected = bool(self.genv.get("CHANNEL_ACCOUNT_SELECTED"))
        try:
            form_data = {}
            content_type = flow.request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                from urllib.parse import parse_qs
                raw = flow.request.content.decode("utf-8", errors="replace")
                parsed = parse_qs(raw, keep_blank_values=True)
                form_data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            elif "application/json" in content_type:
                form_data = json.loads(flow.request.content)

            game_id = flow.request.query.get("game_id", "") or form_data.get("game_id", "")
            process_id = flow.request.query.get("process_id", "")

            if flow.response.status_code == 200:
                resp_data = json.loads(flow.response.content)
                modified = False

                # 强制记住账号（仅限非 netease 渠道；官服通过 create_login 参数实现）
                login_channel = resp_data.get("user", {}).get("login_channel", "")
                if not login_channel.startswith("netease"):
                    ext_info = resp_data.get("ext_info", {})
                    if not ext_info.get("is_remember"):
                        ext_info["is_remember"] = True
                        resp_data["ext_info"] = ext_info
                        modified = True

                    user = resp_data.get("user", {})

                    # pc_ext_info.is_remember 强制设为 true
                    pc_ext = user.get("pc_ext_info", {})
                    if isinstance(pc_ext, dict) and not pc_ext.get("is_remember"):
                        pc_ext["is_remember"] = True
                        user["pc_ext_info"] = pc_ext
                        modified = True
                else:
                    ext_info = resp_data.get("ext_info", {})
                    user = resp_data.get("user", {})


                resp_data["user"] = user
                if not user.get("client_username"):
                    import base64
                    from datetime import datetime, timezone, timedelta
                    from urllib.parse import unquote

                    channel = user.get("login_channel", "")
                    uid = user.get("id", "")
                    short_channel = channel.replace("nearme_", "") if channel.startswith("nearme_") else channel
                    display_name = f"{short_channel}_{uid[-3:]}" if uid else short_channel

                    # 从 extra_unisdk_data 中提取 AT 过期时间
                    expiry_str = ""
                    try:
                        eud_raw = ext_info.get("extra_unisdk_data", "")
                        if eud_raw:
                            eud = json.loads(eud_raw)
                            sauth_b64 = eud.get("SAUTH_JSON", "")
                            if sauth_b64:
                                sauth = json.loads(base64.b64decode(unquote(sauth_b64)))
                                at_jwt = sauth.get("access_token", "")
                                if at_jwt and "." in at_jwt:
                                    payload_b64 = at_jwt.split(".")[1]
                                    payload_b64 += "=" * (-len(payload_b64) % 4)
                                    at_payload = json.loads(base64.b64decode(payload_b64))
                                    exp_ts = at_payload.get("exp", 0)
                                    if exp_ts:
                                        cst = timezone(timedelta(hours=8))
                                        exp_dt = datetime.fromtimestamp(exp_ts, tz=cst)
                                        expiry_str = f" ({exp_dt.month}月{exp_dt.day}日后过期)"
                    except Exception:
                        pass

                    display_name += expiry_str

                    user["client_username"] = display_name
                    resp_data["user"] = user

                    # 同步更新 client_data 中的 display_username
                    cd_raw = user.get("client_data", "")
                    try:
                        cd = json.loads(base64.b64decode(cd_raw)) if cd_raw else {}
                    except Exception:
                        cd = {}
                    cd["display_username"] = display_name
                    user["client_data"] = base64.b64encode(
                        json.dumps(cd, ensure_ascii=False).encode()
                    ).decode()

                    modified = True
                    self.logger.info(f"已确定渠道服显示名称: {display_name}")

                if modified:
                    flow.response.content = json.dumps(resp_data).encode()

            if is_selected:
                if flow.response.status_code == 200 and self.game_helper.get_auto_close_setting(game_id):
                    self._trigger_auto_close()
            else:
                if flow.response.status_code == 200:
                    pending_login_info = self.stack_mgr.pop_pending_login_info(game_id, process_id)
                    if pending_login_info:
                        resp_data = json.loads(flow.response.content)
                        app_state.channels_helper.import_from_scan(
                            pending_login_info, resp_data
                        )
        except Exception:
            self.logger.exception("处理 exchange_token 响应失败")

    def _handle_data_upload_response(self, flow: http.HTTPFlow):
        try:
            form_data = {}
            content_type = flow.request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                from urllib.parse import parse_qs
                raw = flow.request.content.decode("utf-8", errors="replace")
                parsed = parse_qs(raw, keep_blank_values=True)
                form_data = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

            game_id = form_data.get("game_id", "")
            if self.game_helper.get_auto_close_setting(game_id):
                self._trigger_auto_close()
        except Exception:
            self.logger.exception("处理 data/upload 响应失败")

    def _trigger_auto_close(self):
        self.logger.info("检测到登录已完成请求，即将安全触发程序关闭逻辑...")
        def _do_close():
            try:
                import app_state
                if hasattr(app_state, "app") and app_state.app is not None:
                    from PyQt6.QtCore import QMetaObject, Qt
                    QMetaObject.invokeMethod(app_state.app, "quit", Qt.ConnectionType.QueuedConnection)
                    return
            except Exception as e:
                self.logger.error(f"通知主循环退出失败: {e}")
            
            # 兜底：如果 Qt 循环不存在，则手动调用 main 的清理逻辑后强退
            #try:
            #    import __main__
            #    if hasattr(__main__, "handle_exit"):
            #        __main__.handle_exit()
            #except Exception:
            #    pass
            #import os
            #os._exit(0)

        t = threading.Timer(3.0, _do_close)
        t.daemon = True
        t.start()

    def _modify_oversea_config_response(self, flow: http.HTTPFlow):
        try:
            data = json.loads(flow.response.content)
            for i in data.get("game_config", {}).get("account_type", {}).values():
                i["disable_login"] = False
                i["enable"] = True
            data["game_config"]["platform_cross"] = True
            data["game_config"]["quick_login"]["show_role"] = True
            data["game_config"]["quick_login"]["enable"] = True
            flow.response.content = json.dumps(data).encode()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # _idv-login/* local request handling
    # ------------------------------------------------------------------

    def _handle_idv_login_request(self, flow: http.HTTPFlow, path: str):
        """Handle /_idv-login/* routes locally without forwarding upstream.

        The addon creates a response directly so mitmproxy does not
        forward the request to the real server.
        """
        from local_handler import LocalRequestHandler

        handler = LocalRequestHandler(
            game_helper=self.game_helper,
            logger=self.logger,
        )
        status, headers, body = handler.handle(flow.request)
        flow.response = http.Response.make(status, body, headers)
