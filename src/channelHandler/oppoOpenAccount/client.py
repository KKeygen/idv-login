import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from channelHandler.oppoLogin.consts import DEFAULT_CONSTS, OppoNativeConsts
from channelHandler.oppoLogin.consts import build_vip_header_json
from channelHandler.oppoOpenAccount.envinfo import (
    build_env_info_pkg,
    build_env_param_minimal,
)
from channelHandler.oppoOpenAccount.models import AuthorizeRequest, LogoutRequest, RefreshRequest
from logutil import setup_logger
from ssl_utils import should_verify_ssl


DEFAULT_BASE_URL = "https://uc-client-cn.heytapmobi.com/"
OPPO_PROTOCOL_VERSION = "3.0"


@dataclass
class OppoSecureSession:
    base_url: str = DEFAULT_BASE_URL
    consts: OppoNativeConsts = DEFAULT_CONSTS
    session_ticket: str = ""

    def __post_init__(self):
        self.logger = setup_logger()
        self.http = requests.Session()
        self.http.trust_env = False

    def _build_common_headers(self) -> Dict[str, str]:
        h = build_vip_header_json(self.consts)
        # 复用 authro.txt 的做法：open account 标记
        h["is_open_account"] = "true"
        # Content-Type 由请求方法填充
        return h

    def _build_plain_headers(self) -> Dict[str, str]:
        """参考 SecurityRequestInterceptor.plainTextRequest 的明文请求特征。"""

        h = self._build_common_headers()
        h["Accept"] = "application/json"
        # 参考里至少会设置 X-Protocol-Ver=3.0；debug 分支还会带 X-Protocol-Version
        h["X-Protocol-Ver"] = OPPO_PROTOCOL_VERSION
        h["X-Protocol-Version"] = OPPO_PROTOCOL_VERSION
        h["Content-Type"] = "application/json; charset=UTF-8"
        # 明文请求不应该携带加密相关头
        for k in (
            "X-Key",
            "X-I-V",
            "X-Security",
            "X-Safety",
            "X-Protocol",
            "X-Session-Ticket",
        ):
            h.pop(k, None)
        return h

    def post_plain_json(self, path: str, payload_obj: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")
        body_json = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
        headers = self._build_plain_headers()
        r = self.http.post(url, data=body_json, headers=headers, verify=should_verify_ssl())

        new_ticket = r.headers.get("X-Session-Ticket")
        if isinstance(new_ticket, str) and new_ticket:
            self.session_ticket = new_ticket

        try:
            return r.json()
        except Exception:
            return {"success": False, "http": r.status_code, "raw": r.text}

    def post_json(self, path: str, payload_obj: Dict[str, Any], *, allow_plain_fallback: bool = True) -> Dict[str, Any]:
        """发送请求。

        OPPO 服务端接受明文 JSON，因此不再做 AES 加密请求体、222 降级重试与
        响应验签，统一委托给 :meth:`post_plain_json`。

        ``allow_plain_fallback`` 仅为兼容既有调用方保留，已无实际作用。
        """

        return self.post_plain_json(path, payload_obj)


class OppoOpenAccountClient:
    def __init__(
        self,
        consts: OppoNativeConsts = DEFAULT_CONSTS,
        base_url: str = DEFAULT_BASE_URL,
        session_ticket: str = "",
    ):
        self.logger = setup_logger()
        self.consts = consts
        self.session = OppoSecureSession(base_url=base_url, consts=consts, session_ticket=session_ticket)

    def authorize(
        self,
        account_id_token: str,
        biz_app_key: str = "cd73441423364d90a6ac6fe2bc727542",
        app_id: str = "31288517",
        device_id: str = "",
        pkg_name: str = "com.oplus.account.open.sdk",
        pkg_name_sign: str = "00e7ec6745698936072925f64fc2a3e8",
    ) -> Dict[str, Any]:
        if not device_id:
            device_id = self.consts.DEVICE_ID
        env_param = build_env_param_minimal(self.consts)
        env_info = build_env_info_pkg(app_id, device_id, pkg_name, pkg_name_sign, env_param)

        req = AuthorizeRequest(envInfo=env_info, accountIdToken=account_id_token, bizAppKey=biz_app_key)
        req.finalize()
        return self.session.post_json("api/authorize", req.to_dict())

    def token_refresh(
        self,
        refresh_token: str,
        ssoid: str,
        primary_token: str,
        refresh_ticket: str,
        access_token: str,
        secondary_token_map: Optional[Dict[str, str]] = None,
        app_id: str = "31288517",
        device_id: str = "",
        pkg_name: str = "com.oplus.account.open.sdk",
        pkg_name_sign: str = "00e7ec6745698936072925f64fc2a3e8",
        host_package: str = "com.heytap.htms",
    ) -> Dict[str, Any]:
        if not device_id:
            device_id = self.consts.DEVICE_ID
        env_param = build_env_param_minimal(self.consts)
        env_info = build_env_info_pkg(app_id, device_id, pkg_name, pkg_name_sign, env_param)

        package_sign_map = None
        if secondary_token_map and host_package in secondary_token_map:
            package_sign_map = {host_package: secondary_token_map[host_package]}

        req = RefreshRequest(
            refreshToken=refresh_token,
            ssoid=ssoid,
            primaryToken=primary_token,
            packageSignMap=package_sign_map,
            refreshTicket=refresh_ticket,
            envInfo=env_info,
            accessToken=access_token,
        )
        req.finalize()
        return self.session.post_json("api/token/refresh", req.to_dict())

    def logout(self, user_token: str, secondary_token: str = "") -> Dict[str, Any]:
        req = LogoutRequest(userToken=user_token, secondaryToken=secondary_token)
        req.finalize()
        return self.session.post_json("api/v825/logout", req.to_dict())
