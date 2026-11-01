# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import hashlib
import json
import time
import xml.etree.ElementTree as ET

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

PUBLIC_SDK_SIGNER_DER_B64 = (
    'MIICdQIBADANBgkqhkiG9w0BAQEFAASCAl8wggJbAgEAAoGBAIe45LhcT4nEnzP6/VuZU60wYjnJ'
    '8308890fxTZyDR1IebWOnFgEZfV+N+09wgz5vEu44oT+1/G2ZzgYpW8lHoKSIgi8X/30/bBVXLws'
    'aWE7iPC6bEk+ypUcTKBhu/uFnl552UOPx/AAcgMRisGqfH3WXWCCvcrMm8zgHVHAI3uDAgMBAAEC'
    'gYAmRUy5b8492uPJlIy8t4+Q1oPcJ8YxGm7pe3vwzlhEWZt4Rq5vY04c22qJy1BcuE8QSyIUvofF'
    '8NlKSCjeHmlrnbfNpnyQU/GTQ8sdmqhp4TJh1M9AT5ZMEKTkkAwj6gynPvkqT8+6oEOjXQ9ZvPcn'
    'suOgWo2Tpk9hpbLPlXYTgQJBANphWKDoIST1G+/d+bVBK2Dx6EN0sOJqb/Jm9vKKIFbRISW8HyCC'
    'uQ5/5Cai23jaBMb4QlioQR3Q0wETggCOIe0CQQCfGkvyi0XdhwoD/mhlyQ/RxqURcFIdjvj1zBm9'
    'bPsDkrUT1MHmYFUWw02szsJWoxK+HbQFipde9Wvys/YzNCUvAkAlq48nSoOVJXbpPKg7fP9nnLMN'
    'F87/b47aCKrmka8carzGDppr+EDF4IfRplwnj6wtSlvfgT6iTwhm8h+Fyv7NAkBHa3uKPiC3p8XE'
    'MzDT+CSky2IP6PbDlAmH9P3c/Y3UdcfZRhlge//WPwegOhQc5gkk1JqaHjODCfJSOZycMZ07AkAJ'
    'dj57Zw5hzQYDFueTrbmdmWf7rPX0Gz5fdc4zD0gkwW4wElvrU+R2l+FVrs9d9wMrcFFOOMDE2FdH'
    'g0ud7SsK'
)

USS = "https://uss.lenovomm.com/"
GAME = "https://vb.lenovo.com/game-ms-user-identify-core/api/gameCertification/"


class LoginError(RuntimeError):
    pass


class InvalidTgtError(LoginError):
    pass


def _invalid_tgt(content):
    if not isinstance(content, bytes) or len(content) > 131072 or b"<!" in content:
        return False
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return False
    return any(node.tag.rsplit("}", 1)[-1].lower() == "code"
               and (node.text or "").strip().upper() == "USS-0120"
               for node in root.iter())


def tgt_renewal_due(saved, now=None):
    issued, ttl = saved.get("tgt_issued_at"), saved.get("tgt_ttl")
    if type(issued) is not int or type(ttl) is not int or issued < 0 or ttl <= 0:
        return True
    now = int(time.time() if now is None else now)
    return now < issued or now >= issued + ttl // 2


def st_is_valid(saved, now=None):
    issued, ttl = saved.get("st_issued_at"), saved.get("st_ttl")
    if type(issued) is not int or type(ttl) is not int or issued < 0 or ttl <= 0:
        return False
    now = int(time.time() if now is None else now)
    return issued <= now < issued + ttl


def password_digest(password):
    first = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
    return hashlib.md5(first.encode("ascii")).hexdigest().upper()


def sign_parameters(values):
    text = "&".join(f"{k}={values[k]}" for k in sorted(values) if values[k] != "")
    key = serialization.load_der_private_key(base64.b64decode(PUBLIC_SDK_SIGNER_DER_B64), None)
    return base64.b64encode(key.sign(text.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode("ascii")


def tickets(content):
    if not isinstance(content, bytes) or len(content) > 131072 or b"<!" in content:
        raise LoginError("联想返回的凭证格式无效")
    try:
        root = ET.fromstring(content)
        result = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() != "ticketinfo":
                continue
            fields = {c.tag.rsplit("}", 1)[-1].lower(): (c.text or "").strip() for c in node}
            if not fields.get("value") or int(fields.get("ttl", "0")) <= 0:
                raise ValueError()
            result.append(fields)
        if not result:
            raise ValueError()
        return result
    except (ET.ParseError, ValueError, TypeError):
        raise LoginError("联想登录未返回有效凭证；请检查账号或在官方客户端完成验证") from None


class LenovoClient:
    def __init__(self, device_id, config, *, session=None, now=time.time):
        required = ("app_id", "package_name", "app_version", "sdk_ver", "certification_app_id")
        if not isinstance(config, dict) or any(
            not isinstance(config.get(key), str) or not config[key] for key in required
        ):
            raise LoginError("联想渠道配置不完整，请刷新云配置后重试")
        self.config = dict(config)
        self.device_id = str(device_id)
        if not self.device_id:
            raise LoginError("缺少联想登录设备标识")
        self.http = session or requests.Session()
        self.http.trust_env = False
        self.now = now

    def _request(self, method, url, **kwargs):
        try:
            response = self.http.request(method, url, timeout=(10, 25), verify=True,
                                         allow_redirects=False, **kwargs)
            if url in (USS + "authen/1.2/st/getbycredential", USS + "authen/1.2/tgt/renew") and _invalid_tgt(response.content):
                raise InvalidTgtError("联想登录凭证已失效，请重新输入账号密码")
            if response.status_code != 200:
                raise LoginError("联想请求未通过；请稍后重试或在官方客户端完成账号验证")
            if len(response.content) > 131072:
                raise LoginError("联想响应过大，已停止登录")
            return response
        except requests.RequestException:
            raise LoginError("连接联想服务失败，请检查网络后重试") from None

    def _json(self, method, url, **kwargs):
        response = self._request(method, url, **kwargs)
        try:
            data = json.loads(response.content)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError, UnicodeError):
            raise LoginError("联想服务返回格式异常") from None

    def _base(self):
        return {"source": f"android:{self.config['package_name']}-{self.config['app_version']}", "lang": "zh-CN"}

    def password_login(self, account, password):
        if not account.strip() or not password:
            raise LoginError("请输入联想手机号或邮箱和密码")
        data = {**self._base(), "deviceidtype": "mac", "deviceid": self.device_id,
                "devicecategory": "unknown", "devicevendor": "Lenovo", "devicefamily": "unknown",
                "devicemodel": "Lenovo", "osversion": "5.1", "osname": "Android", "imsi": "",
                "passwordEncryptionType": "1", "password": password_digest(password)}
        response = self._request("POST", USS + "authen/1.2/tgt/user/get",
                                 params={"email" if "@" in account else "msisdn": account.strip()}, data=data)
        ticket = tickets(response.content)[0]
        if not ticket.get("userid"):
            raise LoginError("联想凭证缺少账号标识")
        return self.resume({"tgt": ticket["value"], "account_id": ticket["userid"],
                            "tgt_ttl": int(ticket["ttl"]), "tgt_issued_at": int(self.now())})

    def resume(self, saved):
        if not isinstance(saved, dict) or not saved.get("tgt") or not saved.get("account_id"):
            raise LoginError("请重新输入联想账号密码")
        current = dict(saved)
        if tgt_renewal_due(current, self.now()):
            try:
                response = self._request("POST", USS + "authen/1.2/tgt/renew",
                                         data={**self._base(), "lpsutgt": current["tgt"]})
                ticket = tickets(response.content)[0]
                if str(ticket.get("userid", "")) != str(current["account_id"]):
                    raise LoginError("联想账号校验不一致，请重新登录")
                current.update(tgt=ticket["value"], tgt_ttl=int(ticket["ttl"]), tgt_issued_at=int(self.now()))
            except InvalidTgtError:
                raise
            except LoginError:
                pass
        response = self._request("GET", USS + "authen/1.2/st/getbycredential",
                                 params={**self._base(), "lpsutgt": current["tgt"], "realm": self.config["app_id"]})
        parsed = tickets(response.content)
        st_ticket = next((t for t in parsed if t.get("name", "").lower() in ("lpsst", "lpsust")), None)
        if st_ticket is None:
            raise LoginError("联想未返回游戏登录凭证")
        st, issued = st_ticket["value"], int(self.now())
        tgt = next((t for t in parsed if t.get("name", "").lower() in ("lpstgt", "lpsutgt")), None)
        if tgt is not None:
            current.update(tgt=tgt["value"], tgt_ttl=int(tgt["ttl"]), tgt_issued_at=issued)
        info = self._json("POST", USS + "verifyst/1.2/getuserinfo",
                          data={"lang": "zh-CN", "realm": self.config["app_id"], "lpsust": st}).get("IdentityInfo")
        if not isinstance(info, dict) or str(info.get("AccountID", "")) != str(saved["account_id"]):
            raise LoginError("联想账号校验不一致，请重新登录")
        verified = str(info.get("verified", ""))
        if verified not in ("0", "1"):
            raise LoginError("联想未返回账号激活状态")
        realname = self._admission(st, verified == "1")
        return {"tgt": current["tgt"], "st": st, "account_id": str(saved["account_id"]), "realname": realname,
                "st_ttl": int(st_ticket["ttl"]), "st_issued_at": issued,
                **{key: current[key] for key in ("tgt_ttl", "tgt_issued_at") if key in current}}

    def _game(self, method, endpoint, st, fields):
        body = {"appId": self.config["certification_app_id"], "openAppId": self.config["app_id"], "sign_type": "RSA2", **fields}
        body["sign"] = sign_parameters(body)
        kwargs = {"json": body} if method == "POST" else {"params": body}
        result = self._json(method, GAME + endpoint, headers={"realm": self.config["app_id"], "token": st}, **kwargs)
        if type(result.get("code")) is not int or result["code"] != 10000 or not isinstance(result.get("data"), dict):
            raise LoginError("联想未批准本次登录；请在官方客户端检查账号状态")
        return result["data"]

    def _admission(self, st, activated):
        gate = self._game("POST", "loginout", st, {
            "activeState": "1" if activated else "0", "channelId": "", "manufacturer": "Lenovo",
            "deviceModel": "Lenovo", "loginOut": "1", "osVersion": "5.1", "sdkVersion": "V" + self.config["sdk_ver"],
        })
        kind = gate.get("loginLimitType")
        if type(kind) is not int or kind != 2:
            if kind == 3:
                raise LoginError("请先在联想官方游戏客户端完成账号激活，再回来登录")
            raise LoginError("联想服务限制本次登录，请按官方客户端提示处理")
        auth = self._game("GET", "certification", st, {})
        status = auth.get("certificationStatus")
        if type(status) is not int:
            raise LoginError("联想未返回实名认证状态")
        if status != 1:
            if auth.get("minors") is not False:
                raise LoginError("联想实名认证限制本次登录，请在官方客户端处理")
            config = auth.get("verifyConfigStatus")
            if config is not None and (not isinstance(config, dict) or config.get("loginForceStatus") != 1):
                raise LoginError("请先在联想官方游戏客户端完成实名认证，再回来登录")
            return None
        age, pi = auth.get("args"), auth.get("pi")
        if type(age) is not int or not 0 <= age <= 150 or not isinstance(pi, str):
            raise LoginError("联想实名认证返回格式异常")
        return {"age": age, "pi": pi, "realname_type": 0 if age >= 18 else 1}
