import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from channelHandler.oppoLogin.consts import DEFAULT_CONSTS, OppoNativeConsts
from channelHandler.oppoLogin.consts import build_vip_header_json
from channelHandler.oppoOpenAccount.crypto import (
    OPPO_PROTOCOL_VERSION,
    SecurityKey,
    build_security_headers,
    verify_rsa_signature_of_text,
)
from channelHandler.oppoOpenAccount.envinfo import (
    build_device_security_header_plain,
    build_env_info_pkg,
    build_env_param_minimal,
)
from channelHandler.oppoOpenAccount.models import AuthorizeRequest, LogoutRequest, RefreshRequest
from logutil import setup_logger
from ssl_utils import should_verify_ssl


DEFAULT_BASE_URL = "https://uc-client-cn.heytapmobi.com/"


@dataclass
class OppoSecureSession:
    base_url: str = DEFAULT_BASE_URL
    consts: OppoNativeConsts = DEFAULT_CONSTS
    session_ticket: str = ""
    device_security_header_plain: str = ""  # DeviceSecurityHeader 明文（此阶段按 mockNative.js 全空实现）

    def __post_init__(self):
        self.logger = setup_logger()
        self.http = requests.Session()
        self.http.trust_env = False
        if not self.device_security_header_plain:
            self.device_security_header_plain = build_device_security_header_plain()

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
        try:
            return r.json()
        except Exception:
            return {"success": False, "http": r.status_code, "raw": r.text}

    def post_json(self, path: str, payload_obj: Dict[str, Any], *, allow_plain_fallback: bool = True) -> Dict[str, Any]:
        """发送加密 JSON 请求，并在成功时解密返回 JSON。

        参考实现：当响应返回 222 且签名校验通过时，会认为需要“降级”，最终回退到明文请求。
        这里在首次遇到 222 时直接尝试一次明文重试（避免多次无意义的加密重试）。
        """

        url = self.base_url.rstrip("/") + "/" + path.lstrip("/")

        # 每次请求生成随机 AES key + IV
        security_key = SecurityKey.generate()
        security_key.session_ticket = self.session_ticket

        sec_headers = build_security_headers(security_key, self.device_security_header_plain, xor_key_name="key")

        body_json = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
        enc_body = security_key.encrypt(body_json)

        headers = self._build_common_headers()
        headers.update(sec_headers)
        headers["Content-Type"] = "application/encrypted-json; charset=UTF-8"

        r = self.http.post(url, data=enc_body, headers=headers, verify=should_verify_ssl())
        text = r.text

        new_ticket = r.headers.get("X-Session-Ticket")
        if isinstance(new_ticket, str) and new_ticket:
            self.session_ticket = new_ticket

        # 正常成功：直接 AES 解密 body
        if r.status_code != 222 and r.ok:
            try:
                dec = security_key.decrypt(text)
                return json.loads(dec)
            except Exception as e:
                raise RuntimeError(f"解密/解析失败: {e}; raw={text[:200]}")

        # 降级/验签：status=222（decrypt fail）
        if r.status_code == 222:
            sig = r.headers.get("X-Signature", "")
            if not sig:
                raise RuntimeError("服务端返回 222 但缺少 X-Signature")

            md5_v1 = _md5_hex(security_key.header_signature_v1)
            md5_v2 = _md5_hex(security_key.header_signature_v2)
            if not (
                verify_rsa_signature_of_text(md5_v1, sig)
                or verify_rsa_signature_of_text(md5_v2, sig)
            ):
                raise RuntimeError("222 响应签名校验失败")

            if allow_plain_fallback:
                self.logger.info("收到 222(downgrade)，按参考行为改用明文请求重试一次")
                return self.post_plain_json(path, payload_obj)

            return {"success": False, "code": 222, "message": "response decrypt downgrade", "raw": text}

        # 其他 HTTP 错误：尽量返回 JSON，否则返回 raw
        try:
            return r.json()
        except Exception:
            return {"success": False, "http": r.status_code, "raw": text}


def _md5_hex(s: str) -> str:
    import hashlib

    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


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
