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

import base64
import json
import os
import re
import sys
import threading
import time

import app_state
from channelHandler.channelUtils import getShortGameId
from mitmproxy import http
from mpay_request_policy import (
    ROLE_BRIDGED_GAME,
    ROLE_HOSTED_FEVER_MPAY,
    ROLE_NATIVE_GAME,
    _request_values,
    classify_mpay_request,
)


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
        self._hosted_targets_by_process = {}
        self._held_hosted_query_flows = {}
        self._pending_tool_auth = set()

        self.auth_status_domain = str(
            genv.get("DOMAIN_TARGET_AUTH_STATUS", "") or ""
        ).strip().lower()
        self.oversea_domain = str(
            genv.get("DOMAIN_TARGET_OVERSEA", "sdk-os.mpsdk.easebar.com")
        ).strip().lower()
        self.target_domains = {
            str(genv.get("DOMAIN_TARGET", "service.mkey.163.com")).lower(),
            self.oversea_domain,
        }
        if self.auth_status_domain:
            self.target_domains.add(self.auth_status_domain)

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
        host = flow.request.pretty_host.lower()
        path = flow.request.path.split("?")[0]

        # Local API requests must be answered before upstream host filtering.
        if path.startswith("/_idv-login/") and (host == "localhost" or host in self.target_domains):
            self._handle_idv_login_request(flow, path)
            return

        if host not in self.target_domains:
            return

        if host == getattr(self, "auth_status_domain", ""):
            if path.endswith("/sdk/uni_sauth"):
                try:
                    data = json.loads(flow.request.content or b"{}")
                except (TypeError, ValueError, UnicodeDecodeError):
                    data = None
                sync = getattr(app_state.channels_helper, "db_sync", None)
                if isinstance(data, dict) and sync and sync.intercept_auth(data):
                    key = (getShortGameId(str(data.get('gameid', ''))),
                           str(data.get('login_channel', '')), str(data.get('sdkuid', '')))
                    self._pending_tool_auth.discard(key)
                    flow.metadata["idv_local_auth_cancel"] = True
                    self._cancel_login(flow)
            return

        # Overseas MPay uses a different API namespace.  Keep its
        # requests untouched; only pc/config is intentionally patched
        # on the response side below.
        if host == self.oversea_domain:
            return

        if path == "/mpay/api/users/login/qrcode/exchange_token":
            flow.metadata["idv_selected_uuid"] = self.genv.get("CHANNEL_ACCOUNT_SELECTED", "")

        request_role = self._classify_mpay_request(flow)
        if request_role == ROLE_BRIDGED_GAME:
            return
        if request_role == ROLE_HOSTED_FEVER_MPAY:
            self._remember_hosted_request(flow)
            # The hosted instance uses the target game's native init identity,
            # but its QR login traffic follows the same mapping path as the
            # real Fever client when both coexist with this tool.
            if path == "/mpay/api/qrcode/create_login":
                self._discard_held_hosted_query(
                    self._request_process_id(flow)
                )
                self._modify_create_login_request(flow)
            elif path == "/mpay/api/users/login/qrcode/exchange_token":
                self._modify_exchange_token_request(flow)
            elif path == "/mpay/games/pc_config":
                if flow.request.query.get("game_id", "") != "aecglf6ee4aaaarz-g-a50":
                    flow.request.query["cv"] = self.cv
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
            self._modify_exchange_token_request(flow)
        elif path == "/mpay/api/qrcode/query":
            pass  # handled in response
        elif path == "/mpay/api/data/upload":
            pass  # handled in response
        elif not path.startswith("/mpay/api/qrcode/") and not path.startswith("/mpay/api/reverify/"):
            # Global catch-all: add CV to query + POST body, remove arch
            flow.request.query["cv"] = self.cv
            if flow.request.method == "POST":
                self._modify_post_body_cv(flow)

    def response(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host.lower()
        if host not in self.target_domains:
            return

        path = flow.request.path.split("?")[0]

        if host == getattr(self, "auth_status_domain", ""):
            if path.endswith("/sdk/uni_sauth"):
                self._check_uni_sauth_response(flow)
            return

        # ── _idv-login routes are fully handled in request() ──
        if path.startswith("/_idv-login/"):
            return

        try:
            if host == self.oversea_domain:
                if path == "/api/games/pc/config":
                    self._modify_oversea_config_response(flow)
                return

            request_role = self._classify_mpay_request(flow)
            if request_role == ROLE_BRIDGED_GAME:
                # op14 only transfers the one-shot ticket/code to the game.
                # Login is complete from the game's perspective only after its
                # own MPay instance exchanges that value successfully.
                if path == "/mpay/api/users/login/qrcode/exchange_token":
                    self._handle_bridged_game_exchange_token_response(flow)
                return
            if request_role == ROLE_HOSTED_FEVER_MPAY:
                if self._re_login_methods.match(path):
                    self._modify_login_methods_response(flow)
                elif path == "/mpay/games/pc_config":
                    if flow.request.query.get("game_id", "") != "aecglf6ee4aaaarz-g-a50":
                        self._modify_pc_config_response(flow)
                elif path == "/mpay/api/qrcode/image":
                    self._modify_qrcode_image_response(flow)
                elif path == "/mpay/api/qrcode/create_login":
                    target_game_id = self._effective_hosted_game_id(flow)
                    process_id = flow.request.query.get("process_id", "")
                    if target_game_id and process_id:
                        self._hosted_targets_by_process[str(process_id)] = target_game_id
                    self._modify_create_login_response(flow, target_game_id)
                elif path == "/mpay/api/qrcode/query":
                    effective_game_id = self._effective_hosted_game_id(flow)
                    self._handle_qrcode_query_response(
                        flow, effective_game_id
                    )
                    self._hold_hosted_channel_query(flow, effective_game_id)
                elif path == "/mpay/api/users/login/qrcode/exchange_token":
                    self._handle_exchange_token_response(
                        flow,
                        self._effective_hosted_game_id(flow),
                    )
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

    @staticmethod
    def _cancel_login(flow):
        message = "请在渠道服管理界面完成登录后重试"
        flow.response = http.Response.make(
            200, json.dumps({"code": 401, "subcode": 1, "msg": message,
                             "reason": message, "status": message}, ensure_ascii=False).encode(),
            {"Content-Type": "application/json; charset=utf-8"})

    def _check_uni_sauth_response(self, flow: http.HTTPFlow):
        if flow.metadata.get("idv_local_auth_cancel"):
            return
        try:
            data = json.loads(flow.request.content or b"{}")
        except (TypeError, ValueError, UnicodeDecodeError):
            self.logger.debug("SAUTH请求缺少可关联身份，无法标记具体账号；不猜测其他账号")
            return
        if not isinstance(data, dict):
            return
        try:
            payload = json.loads(flow.response.content or b"{}") if flow.response else None
        except (TypeError, ValueError, UnicodeDecodeError):
            payload = None
        success = bool(flow.response and flow.response.status_code == 200 and isinstance(payload, dict)
                       and payload.get("code") == 200 and payload.get("subcode") == 0)
        manager = app_state.channels_helper
        key = (getShortGameId(str(data.get("gameid", ""))),
               str(data.get("login_channel", "")), str(data.get("sdkuid", "")))
        self._pending_tool_auth.discard(key)
        http_status = flow.response.status_code if flow.response else None
        code = payload.get('code') if isinstance(payload, dict) else None
        subcode = payload.get('subcode') if isinstance(payload, dict) else None
        transport_error = getattr(flow, 'error', None)
        network_reason = None
        if transport_error:
            # Only emit a fixed diagnostic category, never the raw proxy message/URL.
            message = str(getattr(transport_error, 'msg', '')).lower()
            if 'timed out' in message or 'timeout' in message:
                network_reason = 'timeout'
            elif 'certificate' in message or 'tls' in message or 'ssl' in message:
                network_reason = 'tls'
            elif 'connection reset' in message or 'connection refused' in message or 'connection closed' in message:
                network_reason = 'connection'
            else:
                network_reason = 'transport_error'
        self.logger.debug('game={} channel={} stage=sauth.response network_type={} network_reason={} http={} code={} subcode={} success={}',
                         key[0], key[1], type(transport_error).__name__ if transport_error else None,
                         network_reason, http_status, code if isinstance(code, int) else None,
                         subcode if isinstance(subcode, int) else None, success)
        try:
            matched = bool(manager and manager.record_sauth(data, success))
        except Exception as error:
            from mpay_db_sync import log_failure
            log_failure(self.logger, f'[channel] game={key[0]} channel={key[1]} stage=sauth.record 登录状态记录失败', error)
            matched = False
        if success:
            if self.game_helper.get_auto_close_setting(key[0]):
                self._trigger_auto_close()
        elif matched:
            self.logger.debug('game={} channel={} stage=sauth.result 登录未成功，关联账号已标记过期', key[0], key[1])
            if key[1] == 'huawei' and isinstance(payload, dict) and payload.get('code') == 401:
                try:
                    debug = json.loads(base64.b64decode(payload.get('debug_message', '')))
                except (ValueError, TypeError, UnicodeError):
                    debug = {}
                if (isinstance(debug, dict) and debug.get('rtnCode') == 3001
                        and re.fullmatch(r'param \[ts:\d+\] should between \[\d+,\d+\] \.', str(debug.get('errMsg', '')))):
                    app_state.toast('华为账号须在工具启动后的 5 分钟内登录。若需中途切换华为账号，请在渠道服管理界面选择该账号，通过二维码重新登录后再试。', duration=10000)

    def error(self, flow: http.HTTPFlow):
        if (flow.request.pretty_host.lower() == getattr(self, "auth_status_domain", "")
                and flow.request.path.split("?")[0].endswith("/sdk/uni_sauth")):
            self._check_uni_sauth_response(flow)

    # ------------------------------------------------------------------
    # Request modification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _request_process_id(flow: http.HTTPFlow) -> str:
        return str(_request_values(flow.request).get("process_id", "") or "")

    def _classify_mpay_request(self, flow: http.HTTPFlow) -> str:
        bridge = app_state.fever_bridge
        hosted_active = bool(
            getattr(getattr(bridge, "ipc", None), "hwnd", None)
        )
        role = classify_mpay_request(
            flow.request,
            app_state.fever_bridge_target_game_ids,
            hosted_mpay_active=hosted_active,
        )
        process_id = self._request_process_id(flow)
        if (
            hosted_active
            and process_id
            and (
                process_id == str(os.getpid())
                or process_id in self._hosted_targets_by_process
            )
        ):
            return ROLE_HOSTED_FEVER_MPAY
        if role == ROLE_BRIDGED_GAME and bridge is not None and process_id:
            game_id = _request_values(flow.request).get("game_id", "")
            ownership = bridge.bridged_process_state(game_id, process_id)
            if ownership is False:
                return ROLE_NATIVE_GAME
        return role

    def _remember_hosted_request(self, flow: http.HTTPFlow) -> None:
        process_id = self._request_process_id(flow)
        if process_id:
            target_game_id = self._effective_hosted_game_id(flow)
            if target_game_id:
                self._hosted_targets_by_process[process_id] = target_game_id

    def _discard_held_hosted_query(self, process_id: str) -> None:
        if not process_id:
            return
        held = self._held_hosted_query_flows.pop(process_id, None)
        if held is None or not getattr(held, "intercepted", False):
            return
        try:
            held.kill()
        except Exception:
            self.logger.exception("清理旧的托管 MPay 二维码响应失败")

    def done(self):
        """Release intercepted responses when mitmproxy shuts down."""
        for process_id in list(self._held_hosted_query_flows):
            self._discard_held_hosted_query(process_id)

    def _modify_post_body_cv(self, flow: http.HTTPFlow):
        """为 POST 请求的 body 注入 cv 并移除 arch（全局 catch-all 用）。"""
        content_type = flow.request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qs, urlencode
            raw = flow.request.content.decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=True)
            parsed["cv"] = [self.cv]
            parsed.pop("arch", None)
            flat = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            flow.request.content = urlencode(flat).encode()
        elif "application/json" in content_type:
            try:
                body = json.loads(flow.request.content)
                body["cv"] = self.cv
                body.pop("arch", None)
                flow.request.content = json.dumps(body).encode()
            except Exception:
                pass

    def _modify_create_login_request(self, flow: http.HTTPFlow):
        query = dict(flow.request.query)
        game_id = query.get("dst_jf_game_id", "") or query.get("game_id", "")
        if self.create_login_query_hook:
            self.create_login_query_hook(query, game_id)
            flow.request.query.update(query)

    _EXCHANGE_TOKEN_OVERRIDE_KEYS = frozenset({
        "opt_fields", "app_type", "app_mode", "app_channel",
        "_cloud_extra_base64", "sc", "cv",
        "gv", "gvn", "sv",
    })

    def _modify_exchange_token_request(self, flow: http.HTTPFlow):
        """覆写 exchange_token 请求参数（query + body），与 v5.9.1 行为一致。"""
        flow.metadata["idv_selected_uuid"] = self.genv.get("CHANNEL_ACCOUNT_SELECTED", "")
        game_id = flow.request.query.get("game_id", "")
        dst_game_id = flow.request.query.get("dst_jf_game_id", "")
        if not game_id or not dst_game_id:
            content_type = flow.request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                from urllib.parse import parse_qs
                raw = flow.request.content.decode("utf-8", errors="replace")
                parsed = parse_qs(raw, keep_blank_values=True)
                game_id = game_id or parsed.get("game_id", [""])[0]
                dst_game_id = parsed.get("dst_jf_game_id", [dst_game_id])[0]
            elif "application/json" in content_type:
                try:
                    body = json.loads(flow.request.content)
                    game_id = game_id or body.get("game_id", "")
                    dst_game_id = body.get("dst_jf_game_id", dst_game_id)
                except Exception:
                    pass

        config = self.cloud_res().get_qrcode_login_config(dst_game_id or game_id)
        if not config:
            return

        overrides = {k: str(config[k]) for k in self._EXCHANGE_TOKEN_OVERRIDE_KEYS if k in config}
        if not overrides:
            return

        # query: 覆写 cv
        if "cv" in overrides:
            flow.request.query["cv"] = overrides["cv"]

        # body: 覆写所有 7 个参数 + 移除 arch
        content_type = flow.request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qs, urlencode
            raw = flow.request.content.decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=True)
            for k, v in overrides.items():
                parsed[k] = [v]
            parsed.pop("arch", None)
            flat = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}
            flow.request.content = urlencode(flat).encode()
        elif "application/json" in content_type:
            try:
                body = json.loads(flow.request.content)
                body.update(overrides)
                body.pop("arch", None)
                flow.request.content = json.dumps(body).encode()
            except Exception:
                pass

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
        if flow.response.status_code != 200:
            return
        data = json.loads(flow.response.content)
        user = data["user"]
        # Channel credentials are refreshed in the MPay DB before game launch.
        # Preserve that complete bundle here; a second proxy renewal would mix
        # native SDK state with a different session/timestamp.
        if user.get("pc_ext_info", {}).get("extra_unisdk_data"):
            return
        user["pc_ext_info"] = PC_INFO
        flow.response.content = json.dumps(data).encode("utf-8")

    def _modify_qrcode_image_response(self, flow: http.HTTPFlow):
        if not self.genv.get("SCAN_RECORD_ENABLED", True):
            return
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

    def _modify_create_login_response(
        self, flow: http.HTTPFlow, hosted_target_game_id: str = ""
    ):
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
            effective_raw_game_id = (
                dst_jf_game_id or hosted_target_game_id or game_id
            )
            effective_game_id = getShortGameId(effective_raw_game_id)
            if dst_jf_game_id:
                qr_data["dst_jf_game_id"] = dst_jf_game_id
                if (not self.genv.get("has_opened_admin", False)) and (not query.get("_cloud_extra_base64", "")):
                    self.genv.set("has_opened_admin", True)
                    if self.ui_manager:
                        self.ui_manager.open_for_game(dst_jf_game_id, "accounts")
                self.stack_mgr.push_cached_qrcode_data(effective_game_id, process_id, qr_data)
                self.stack_mgr.ensure_pending_stack(effective_game_id)
            else:
                self.stack_mgr.push_cached_qrcode_data(effective_game_id, process_id, qr_data)
                self.stack_mgr.ensure_pending_stack(effective_game_id)

            # Auto-login
            auto_uuid = self.genv.get(f"auto-{effective_game_id}", "")
            if auto_uuid:
                delay = self.game_helper.get_login_delay(effective_game_id)
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
            is_compat = getattr(getattr(app_state, "proxy_mgr", None), "mode", "") == "compat"
            if is_compat:
                qr_url = (
                    "https://localhost/_idv-login/index"
                    f"?game_id={effective_game_id}&view=accounts"
                )
            else:
                qr_url = (
                    f"idvlogin://open?game_id={effective_game_id}&view=accounts"
                )
            data["qrcode_scanners"][0]["url"] = qr_url

            if self.genv.get("SCAN_RECORD_ENABLED", True):
                data["scanner_guide_text"] = "已开启扫码登录【官服/渠道服】账号，如需长期保存，点击上方图标"
                data["scanner_download_guide_text"] = "如果您正在为代肝/共号扫码，请注意保护账号安全，谨防诈骗"

            flow.response.content = json.dumps(data).encode()
        except Exception:
            self.logger.exception("处理 create_login 响应失败")

    def _effective_hosted_game_id(self, flow: http.HTTPFlow) -> str:
        query = flow.request.query
        process_id = str(query.get("process_id", "") or "")
        bridge = app_state.fever_bridge
        return str(
            query.get("dst_jf_game_id", "")
            or getattr(bridge, "active_target_long_game_id", "")
            or self._hosted_targets_by_process.get(process_id, "")
            or query.get("game_id", "")
        )

    def _handle_qrcode_query_response(
        self, flow: http.HTTPFlow, effective_game_id: str = ""
    ):
        if self.genv.get("CHANNEL_ACCOUNT_SELECTED"):
            return
        try:
            data = json.loads(flow.response.content)
            game_id = effective_game_id or flow.request.query.get("game_id", "")
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

    def _hold_hosted_channel_query(
        self, flow: http.HTTPFlow, effective_game_id: str = ""
    ) -> bool:
        """Hold a channel success response and hand its code to the game.

        The response must not reach hosted MPay: it would exchange the one-shot
        code before the game receives it through op14.
        """
        handed_off = False
        try:
            if flow.response.status_code != 200:
                return False
            data = json.loads(flow.response.content)
            if data.get("qrcode", {}).get("status") != 2:
                return False
            login_info = data.get("login_info", {})
            if not isinstance(login_info, dict):
                return False
            login_channel = str(login_info.get("login_channel", "") or "")
            ticket = str(login_info.get("code", "") or "")
            if not login_channel or login_channel == "netease" or not ticket:
                return False

            bridge = app_state.fever_bridge
            if bridge is None:
                return False
            process_id = self._request_process_id(flow)
            if not process_id:
                return False
            flow.intercept()
            target_process_id = bridge.accept_channel_qrcode_ticket(
                login_channel, ticket
            )
            if target_process_id is None:
                flow.resume()
                return False
            handed_off = True
            pending_login_info = self.stack_mgr.pop_pending_login_info(
                effective_game_id, process_id
            )
            if pending_login_info:
                self.stack_mgr.push_pending_login_info(
                    effective_game_id,
                    str(target_process_id),
                    pending_login_info,
                )
            previous = self._held_hosted_query_flows.pop(process_id, None)
            if (
                previous is not None
                and previous is not flow
                and getattr(previous, "intercepted", False)
            ):
                try:
                    previous.kill()
                except Exception:
                    self.logger.exception("清理重复的渠道服登录响应失败")
            self._held_hosted_query_flows[process_id] = flow
            self.logger.info(
                "已挂起托管 MPay 的渠道服登录响应并转交游戏: "
                f"game={getShortGameId(effective_game_id)}, "
                f"channel={login_channel}"
            )
            return True
        except Exception:
            if not handed_off and getattr(flow, "intercepted", False):
                flow.resume()
            self.logger.exception("接管托管 MPay 渠道服登录 code 失败")
            return False

    def _handle_exchange_token_response(
        self,
        flow: http.HTTPFlow,
        effective_game_id: str = "",
    ):
        from account_list_policy import AccountListPolicy, native_scan_channels
        from channelmgr import channel

        selected_uuid = flow.metadata.get("idv_selected_uuid", "")
        try:
            if flow.response.status_code != 200:
                return
            resp_data = json.loads(flow.response.content)
            user = resp_data.get("user", {})
            if not isinstance(user, dict) or not user.get("id"):
                return
            values = _request_values(flow.request)
            game_id = effective_game_id or values.get("game_id", "")
            process_id = self._request_process_id(flow)
            manager = app_state.channels_helper
            selected = manager.query_channel(selected_uuid) if manager and selected_uuid else None
            login_channel = str(user.get("login_channel", ""))
            if selected and selected.channel_name != login_channel:
                selected = None
            record = selected
            if not selected_uuid and manager and self.genv.get("SCAN_RECORD_ENABLED", True):
                pending = self.stack_mgr.pop_pending_login_info(game_id, process_id)
                if pending:
                    manager.import_from_scan(pending, resp_data, game_id)
                    from channel_cache import packet_identity
                    ext = user.get('pc_ext_info') or resp_data.get('ext_info') or {}
                    try:
                        _, sdk_channel, sdkuid = packet_identity({'login_channel': login_channel,
                            'extra_unisdk_data': ext.get('extra_unisdk_data', '')})
                        record = next((r for r in manager.channels
                                       if r.record_source == "scan" and r.channel_name == sdk_channel
                                       and r.current_sdkuid() == sdkuid
                                       and getShortGameId(r.game_id) == getShortGameId(game_id)), None)
                    except ValueError:
                        self.logger.debug('扫码结果缺少有效SAUTH身份；不使用MPay ID匹配渠道记录')
            if not login_channel.startswith("netease"):
                remember = bool((selected and selected.record_source == "manual")
                                or (not selected_uuid and login_channel in native_scan_channels(game_id)))
                ext_info = resp_data.setdefault("ext_info", {})
                ext_info["is_remember"] = remember
                pc_ext = user.setdefault("pc_ext_info", {})
                if isinstance(pc_ext, dict):
                    pc_ext["is_remember"] = remember
                label_record = record or channel(
                    {"login_channel": login_channel, "code": str(user["id"])}, user, ext_info,
                    resp_data.get("device", {}),
                    name=str(user.get("client_username") or user.get("nickname") or user["id"]))
                if record is None:
                    label_record.record_source = "scan"
                    label_record.game_id = getShortGameId(game_id)
                display_name = AccountListPolicy(manager).account_label(label_record)
                user["client_username"] = display_name
                try:
                    cd = json.loads(base64.b64decode(user.get("client_data", "")))
                except (TypeError, ValueError, UnicodeDecodeError):
                    cd = {}
                if not isinstance(cd, dict):
                    cd = {}
                cd["display_username"] = display_name
                user["client_data"] = base64.b64encode(
                    json.dumps(cd, ensure_ascii=False).encode()).decode()
                flow.response.content = json.dumps(resp_data).encode()
            if login_channel and not login_channel.startswith('netease'):
                from channel_cache import packet_identity
                try:
                    identity = packet_identity({'login_channel': login_channel,
                        'extra_unisdk_data': (user.get('pc_ext_info') or {}).get('extra_unisdk_data', '')})
                    self._pending_tool_auth.add(identity)
                except ValueError:
                    self.logger.debug("exchange_token 的渠道 SAUTH 身份无效；不使用外层账号 ID 代替 SDKUID")
        except Exception as error:
            from mpay_db_sync import log_failure
            log_failure(self.logger, f'[channel] game={effective_game_id} stage=exchange.observe 处理登录回包失败；保留原游戏响应', error)

    def _handle_bridged_game_exchange_token_response(
        self, flow: http.HTTPFlow
    ) -> None:
        """Observe the game's exchange; completion waits for uni_sauth."""
        self._handle_exchange_token_response(flow)

    def _handle_data_upload_response(self, flow: http.HTTPFlow):
        try:
            # data/upload is the legacy completion signal.  Some clients send
            # JSON while older clients use form encoding; accept both through
            # the same request parser used by the other MPay routes.
            game_id = _request_values(flow.request).get("game_id", "")
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
