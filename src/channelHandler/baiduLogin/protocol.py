# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import struct
import threading
import uuid
import zlib
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

import requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


ENDPOINT = "https://sdkapi.u.duoku.com/game/call"
_SDK_RSA_MODULUS = (
    "B38B9F5D42DEF0BDEF067D3009B1E92475E130399C9DC7CC31F0361D6581D0245"
    "CB3AE5664D9337D9370C5CC842D9362F4F51A259DDF928080457A40E682A2BB"
)
_MAX_BODY = 1024 * 1024
_MAX_WIRE = 2 * _MAX_BODY
_DEVICE_FIELDS = (
    "IMSI", "IMEI", "AdId", "MAC1", "MAC", "ICCID", "MCC", "MNC",
    "CUID_ENCRYPT", "DeviceId", "OAID",
)


class BaiduProtocolError(RuntimeError):

    def __init__(self, code: int = -2, kind: str = "protocol") -> None:
        self.code = code if type(code) is int else -2
        self.kind = kind if kind in {
            "protocol", "config", "input", "transport", "http", "endpoint",
            "response", "header", "server", "handshake", "closed",
        } else "protocol"
        self.stage = self.kind
        super().__init__(f"channel=baidu step={self.kind} code={self.code}")


def _text(value, *, kind: str = "response", required: bool = True,
          limit: int = 8192) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or len(value) > limit or (required and not value):
        raise BaiduProtocolError(-2, kind)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise BaiduProtocolError(-2, kind)
    return value


def _integer(value, *, kind: str = "response") -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value):
        result = int(value)
    else:
        raise BaiduProtocolError(-2, kind)
    if not -(2 ** 31) <= result < 2 ** 31:
        raise BaiduProtocolError(-2, kind)
    return result


def _endpoint(value: str, *, metadata: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            isinstance(value, str) and value == value.strip()
            and not any(ord(ch) < 33 or ord(ch) == 127 for ch in value)
            and parsed.scheme in (("https", "http") if metadata else ("https",))
            and parsed.hostname == "sdkapi.u.duoku.com"
            and parsed.path == "/game/call"
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and parsed.port in (None, 443 if parsed.scheme == "https" else 80)
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise BaiduProtocolError(-2, "endpoint")
    return ENDPOINT


def _aes_encrypt(key: bytes, body: bytes) -> bytes:
    try:
        if len(key) != 16:
            raise ValueError
        return AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(body, 16))
    except (TypeError, ValueError):
        raise BaiduProtocolError(-2, "protocol") from None


def _encode_request(act_id: int, body: dict, app_id: int, *, key: bytes,
                    session_id: bytes = b"", encrypted: bool = True,
                    md5_offset: int | None = None) -> bytes:
    try:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_BODY or len(session_id) not in (0, 32):
            raise ValueError
        wire_body = _aes_encrypt(key, raw) if encrypted else raw
        offset = secrets.randbelow(29) if md5_offset is None else md5_offset
        if type(offset) is not int or not 0 <= offset <= 28:
            raise ValueError
        header = bytearray(64)
        header[:len(session_id)] = session_id
        struct.pack_into("<H", header, 32, 3)
        header[34] = 4 if encrypted else 0
        struct.pack_into("<H", header, 35, act_id)
        header[37] = offset
        digest = hashlib.md5(wire_body).hexdigest().encode("ascii")
        header[38:42] = digest[offset:offset + 4]
        struct.pack_into("<i", header, 42, app_id)
        return bytes(header) + wire_body
    except (TypeError, ValueError, OverflowError, struct.error):
        raise BaiduProtocolError(-2, "protocol") from None


def _decode_response(wire: bytes, key: bytes) -> dict:
    try:
        if not isinstance(wire, bytes) or not wire or len(wire) > _MAX_WIRE:
            raise ValueError
        packet = base64.b64decode(re.sub(rb"[\t\r\n ]", b"", wire), validate=True)
        if len(packet) < 32:
            raise ValueError
        code = struct.unpack_from("<i", packet, 3)[0]
        if code:
            raise BaiduProtocolError(code, "header")
        body = packet[32:]
        offset = packet[7]
        if not body or offset > 28:
            raise ValueError
        digest = hashlib.md5(body).hexdigest().encode("ascii")
        if not hmac.compare_digest(packet[8:12].lower(), digest[offset:offset + 4]):
            raise ValueError
        encoding = packet[0]
        if encoding == 4:
            if len(key) != 16:
                raise ValueError
            body = unpad(AES.new(key, AES.MODE_CBC, iv=key).decrypt(body), 16)
        elif encoding == 1:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = inflater.decompress(body, _MAX_BODY + 1)
            if not inflater.eof or inflater.unconsumed_tail or inflater.unused_data:
                raise ValueError
        elif encoding != 0:
            raise ValueError
        if len(body) > _MAX_BODY:
            raise ValueError
        obj = json.loads(body.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError
        _integer(obj.get("ResultCode"))
        if "ErrorCode" in obj and _integer(obj["ErrorCode"]) == -5000:
            raise BaiduProtocolError(-5000, "server")
        return obj
    except (TypeError, ValueError, UnicodeError, binascii.Error, zlib.error,
            RecursionError, struct.error):
        raise BaiduProtocolError(-2, "response") from None


def _check_result(body: dict) -> None:
    code = _integer(body.get("ResultCode"))
    if code:
        raise BaiduProtocolError(code, "server")


def _normalise_login(body: dict) -> dict:
    _check_result(body)
    return {
        "uid": _text(body.get("Uid")),
        "access_token": _text(body.get("AccessToken")),
        "account_id": _text(body.get("AccountID")),
        "auto_login_sign": _text(body.get("AutoLoginSign")),
        "user_name": _text(body.get("UserName", "") or "", required=False),
        "display_name": _text(body.get("DisplayName", "") or "", required=False),
        "has_phone_num": _integer(body.get("HasPhoneNum")) != 0,
        "is_guest": _integer(body.get("IsGuest")) != 0,
    }


class BaiduProtocol:

    def __init__(self, config: Mapping, *, session=None, timeout: float = 15) -> None:
        if not isinstance(config, Mapping):
            raise BaiduProtocolError(-2, "config")
        self._app_id = _integer(config.get("app_id"), kind="config")
        if self._app_id <= 0:
            raise BaiduProtocolError(-2, "config")
        self._app_key = _text(config.get("app_key"), kind="config")
        self._version_name = _text(config.get("version_name"), kind="config")
        self._version_code = _integer(config.get("version_code"), kind="config")
        if self._version_code < 0:
            raise BaiduProtocolError(-2, "config")
        self._account_prefer = _integer(config.get("account_type_prefer", 0), kind="config")
        self._endpoint = _endpoint(config.get("endpoint", ENDPOINT))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
            raise BaiduProtocolError(-2, "config")
        self._timeout = timeout
        device_info = config.get("device_info", {})
        if not isinstance(device_info, Mapping):
            raise BaiduProtocolError(-2, "config")
        self._device = {
            field: _text(device_info.get(field, ""), kind="config", required=False)
            for field in _DEVICE_FIELDS
        }
        self._device["DeviceId"] = _text(config.get("device_id") or self._device["DeviceId"]
                                        or hashlib.md5(uuid.uuid4().hex.encode()).hexdigest(),
                                        kind="config")
        header_values = {
            "Platform": "2", "SDKVersion": "4.4.9",
            "FWVersion": config.get("fw_version", "10"),
            "PhoneType": config.get("phone_type", "Android"),
            "Resolution": config.get("resolution", "1080x1920"),
            "Network": "1", "Jailbreak": "0",
            "ChannelID": config.get("sdk_channel_id"),
        }
        self._headers = {
            name: quote(_text(value, kind="config"), safe="_-.!~'()*")
            for name, value in header_values.items()
        }
        self._headers.update({"Accept": "*/*", "Accept-Language": "zh-cn",
                              "Accept-Charset": "UTF-8"})
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._session_id = b""
        self._key = b""
        self._rsa = RSA.construct((int(_SDK_RSA_MODULUS, 16), 65537))
        self._lock = threading.RLock()
        self._closed = False

    def _post(self, act_id: int, body: dict, *, key: bytes,
              encrypted: bool, session_id: bytes = b"") -> dict:
        payload = _encode_request(act_id, body, self._app_id, key=key,
                                  session_id=session_id, encrypted=encrypted)
        response = None
        try:
            response = self._session.post(
                _endpoint(self._endpoint), data=payload, headers=self._headers,
                timeout=self._timeout, allow_redirects=False, verify=True, stream=True,
            )
            status = response.status_code
            if type(status) is not int or not 200 <= status < 300:
                raise BaiduProtocolError(status if type(status) is int else -1, "http")
            wire = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                wire.extend(chunk)
                if len(wire) > _MAX_WIRE:
                    raise BaiduProtocolError(-2, "response")
            return _decode_response(bytes(wire), key)
        except BaiduProtocolError:
            raise
        except Exception:
            raise BaiduProtocolError(-1, "transport") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _handshake(self) -> None:
        self._session_id, self._key = b"", b""
        redirected = rotated = reset = False
        for _ in range(4):
            key = secrets.token_bytes(16)
            try:
                sign = PKCS1_v1_5.new(self._rsa).encrypt(key).hex()
            except (TypeError, ValueError):
                raise BaiduProtocolError(-2, "handshake") from None
            body = {"Sign": sign,
                    "AppKey": _aes_encrypt(key, self._app_key.encode("utf-8")).hex()}
            body.update({name: _aes_encrypt(key, value.encode("utf-8")).hex()
                         for name, value in self._device.items()})
            body.update({"AccountTypePrefer": str(self._account_prefer),
                         "AppVersionName": self._version_name,
                         "AppVersionCode": str(self._version_code)})
            result = self._post(1, body, key=key, encrypted=False)
            code = _integer(result.get("ResultCode"))
            if code == 1004 and not redirected:
                self._endpoint = _endpoint(result.get("SdkSysBackCallUrl"), metadata=True)
                redirected = True
                continue
            if code == 1002 and not rotated:
                modulus = _text(result.get("PubKey"), limit=1024)
                if not re.fullmatch(r"[0-9a-fA-F]{128,1024}", modulus):
                    raise BaiduProtocolError(-2, "handshake")
                try:
                    self._rsa = RSA.construct((int(modulus, 16), 65537))
                    if not 512 <= self._rsa.size_in_bits() <= 4096:
                        raise ValueError
                except (ValueError, TypeError):
                    raise BaiduProtocolError(-2, "handshake") from None
                rotated = True
                continue
            if code == 1003 and not reset:
                reset = True
                continue
            _check_result(result)
            if result.get("BackCallUrl"):
                self._endpoint = _endpoint(result["BackCallUrl"], metadata=True)
            try:
                session_id = _text(result.get("SessionId"), limit=32).encode("ascii")
                if len(session_id) != 32:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise BaiduProtocolError(-2, "handshake") from None
            self._session_id, self._key = session_id, key
            return
        raise BaiduProtocolError(-2, "handshake")

    def _login(self, act_id: int, body: dict) -> dict:
        with self._lock:
            if self._closed:
                raise BaiduProtocolError(-2, "closed")
            for attempt in range(2):
                if not self._session_id:
                    self._handshake()
                try:
                    result = self._post(act_id, body, key=self._key, encrypted=True,
                                        session_id=self._session_id)
                    return _normalise_login(result)
                except BaiduProtocolError as exc:
                    if exc.kind == "header" and exc.code == 2:
                        self._session_id, self._key = b"", b""
                        if attempt == 0:
                            continue
                    raise
        raise BaiduProtocolError(-2, "protocol")

    def login_by_bduss(self, bduss: str) -> dict:
        if not isinstance(bduss, str):
            raise BaiduProtocolError(-2, "input")
        value = _text(bduss, kind="input")
        if any(ch.isspace() or ch in ";\r\n" or ord(ch) > 126 for ch in value):
            raise BaiduProtocolError(-2, "input")
        return self._login(2, {"BDUSS": value})

    def refresh(self, login_info: Mapping) -> dict:
        if not isinstance(login_info, Mapping):
            raise BaiduProtocolError(-2, "input")
        return self._login(46, {
            "AccountIDType": "0",
            "AccountIDValue": _text(login_info.get("account_id"), kind="input"),
            "AutoLoginSign": _text(login_info.get("auto_login_sign"), kind="input"),
        })

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._session_id, self._key = b"", b""
            if self._owns_session:
                try:
                    self._session.close()
                except Exception:
                    raise BaiduProtocolError(-1, "transport") from None
