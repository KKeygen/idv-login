# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import math
import re
import secrets
import struct
import time

import requests

_MAX_PLAIN = 1024 * 1024
_MAX_WIRE = 2 * _MAX_PLAIN
_MAX_FIELDS = 4096
_MASK = 0xFFFFFFFF
_DELTA = 0x9E3779B9


class GuopanProtocolError(Exception):

    def __init__(self, stage="protocol", *, code=None, kind="protocol"):
        self.stage = stage if isinstance(stage, str) and re.fullmatch(r"[a-z0-9_]{1,48}", stage) else "protocol"
        self.kind = kind if isinstance(kind, str) and re.fullmatch(r"[a-z0-9_]{1,48}", kind) else "protocol"
        self.code = code if type(code) is int else None
        self.authentication_expired = self.kind == "auth" and self.code in (1001, 1002)
        safe_code = str(self.code) if self.code is not None else "invalid"
        super().__init__(f"channel=guopan step={self.stage}_{self.kind} code={safe_code}")


def _fail(stage="protocol", *, code=None, kind="protocol"):
    raise GuopanProtocolError(stage, code=code, kind=kind) from None


def _varint(value):
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        _fail("protobuf")
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _uint(number, value):
    return _varint(number << 3) + _varint(value)


def _blob(number, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _parse(data):
    if not isinstance(data, bytes) or len(data) > _MAX_PLAIN:
        _fail("protobuf")
    position = 0
    fields = {}
    count = 0

    def read_varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                _fail("protobuf")
            byte = data[position]
            position += 1
            if shift == 63 and byte > 1:
                _fail("protobuf")
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        _fail("protobuf")

    while position < len(data):
        count += 1
        if count > _MAX_FIELDS:
            _fail("protobuf")
        tag = read_varint()
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 1 << 29:
            _fail("protobuf")
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            size = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - position:
                _fail("protobuf")
            value = data[position:position + size]
            position += size
        else:
            _fail("protobuf")
        fields.setdefault(number, []).append((wire, value))
    return fields


def _one(fields, number, wire, *, required=False, default=None):
    values = fields.get(number, [])
    if not values:
        if required:
            _fail("protobuf")
        return default
    if len(values) != 1 or values[0][0] != wire:
        _fail("protobuf")
    return values[0][1]


def _number(fields, number, *, required=False, default=None, bits=64):
    value = _one(fields, number, 0, required=required, default=default)
    if value is not None and not 0 <= value < 1 << bits:
        _fail("protobuf")
    return value


def _int32(fields, number, *, required=False):
    value = _number(fields, number, required=required)
    if value is None:
        return None
    if value > 0xFFFFFFFF and value < (1 << 64) - (1 << 31):
        _fail("protobuf")
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & (1 << 31) else value


def _text(fields, number, *, required=False, default="", limit=4096):
    value = _one(fields, number, 2, required=required)
    if value is None:
        return default
    if len(value) > limit:
        _fail("protobuf")
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeError:
        _fail("protobuf")


def _message(fields, number, *, required=True):
    return _parse(_one(fields, number, 2, required=required, default=b""))


def _mx(z, y, total, key, position, e):
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^
            ((total ^ y) + (key[position & 3 ^ e] ^ z))) & _MASK


def _xxtea_encrypt(data, key):
    if not isinstance(data, bytes) or not data or len(data) > _MAX_PLAIN or len(key) != 16:
        _fail("crypto")
    padded = data + b"\0" * (-len(data) % 4)
    words = list(struct.unpack("<" + "I" * (len(padded) // 4), padded)) + [len(data)]
    key_words = struct.unpack("<4I", key)
    n = len(words) - 1
    z, total = words[n], 0
    for _ in range(6 + 52 // (n + 1)):
        total = (total + _DELTA) & _MASK
        e = total >> 2 & 3
        for p in range(n):
            words[p] = (words[p] + _mx(z, words[p + 1], total, key_words, p, e)) & _MASK
            z = words[p]
        words[n] = (words[n] + _mx(z, words[0], total, key_words, n, e)) & _MASK
        z = words[n]
    return struct.pack("<" + "I" * len(words), *words)


def _xxtea_decrypt(data, key):
    if (not isinstance(data, bytes) or len(data) < 8 or len(data) % 4 or
            len(data) > _MAX_PLAIN + 8 or len(key) != 16):
        _fail("crypto")
    words = list(struct.unpack("<" + "I" * (len(data) // 4), data))
    key_words = struct.unpack("<4I", key)
    n = len(words) - 1
    y = words[0]
    total = ((6 + 52 // (n + 1)) * _DELTA) & _MASK
    while total:
        e = total >> 2 & 3
        for p in range(n, 0, -1):
            words[p] = (words[p] - _mx(words[p - 1], y, total, key_words, p, e)) & _MASK
            y = words[p]
        words[0] = (words[0] - _mx(words[n], y, total, key_words, 0, e)) & _MASK
        y = words[0]
        total = (total - _DELTA) & _MASK
    length = words[-1]
    if not len(data) - 7 <= length <= len(data) - 4 or length > _MAX_PLAIN:
        _fail("crypto")
    return struct.pack("<" + "I" * n, *words[:-1])[:length]


def _identifier(value, stage):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(stage, kind="input")
    string = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", string) or int(string) >= 1 << 64:
        _fail(stage, kind="input")
    return int(string)


def _input_text(value, stage, *, empty=False, maximum=4096):
    if not isinstance(value, str) or (not value and not empty) or len(value) > maximum:
        _fail(stage, kind="input")
    try:
        if len(value.encode("utf-8")) > maximum:
            _fail(stage, kind="input")
    except UnicodeError:
        _fail(stage, kind="input")
    return value


class GuopanClient:

    def __init__(self, app_id, app_key, device_info=None, *, session=None, timeout=15):
        self.app_id = _identifier(app_id, "config")
        if self.app_id >= 1 << 32:
            _fail("config", kind="input")
        if not isinstance(app_key, str) or not re.fullmatch(r"[\x21-\x7e]{16}", app_key):
            _fail("config", kind="input")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            _fail("config", kind="input")
        if device_info is not None and not isinstance(device_info, dict):
            _fail("config", kind="input")
        self._key = app_key.encode("ascii")
        self._endpoint = f"https://userapi.guopan.cn/gamesdk/{self.app_id}"
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._closed = False
        self._device = dict(device_info or {})
        self._device.setdefault("device_id", secrets.token_hex(16))
        self._device.setdefault("random_id", self._device["device_id"])
        self._reset()
        self._common()

    def _reset(self):
        self._uin = 0
        self._header_uin = 0
        self._login_key = ""
        self._username = ""
        self._result = None
        self._accounts = {}

    def close(self):
        self._reset()
        self._key = b""
        self._closed = True
        try:
            self._session.close()
        except Exception:
            pass

    def _common(self):
        d = self._device
        values = {1: d["device_id"], 3: "sdk_5.5.0.2", 4: d.get("os_version", ""),
                  9: d["device_id"], 13: self._username, 20: d["device_id"],
                  22: d.get("model", ""), 28: d.get("oaid", ""),
                  29: d["random_id"], 30: d.get("fdid", ""),
                  33: d.get("vaid", ""), 34: d.get("aaid", ""),
                  35: d.get("android_id", ""), 36: d.get("ad_package", "")}
        ints = {2: 104, 5: 100, 6: 102, 7: 1004, 12: 0, 14: 0,
                15: d.get("network", 0), 21: self.app_id,
                23: d.get("android_sdk", 0), 26: 0, 32: 0}
        if ints[15] not in (0, 1, 2):
            _fail("config", kind="input")
        for number, value in ints.items():
            if type(value) is not int or not 0 <= value < 1 << 32:
                _fail("config", kind="input")
        parts = {n: _blob(n, _input_text(v, "config", empty=True)) for n, v in values.items()}
        parts.update({n: _uint(n, v) for n, v in ints.items()})
        return b"".join(parts[n] for n in sorted(parts))

    def _post(self, payload, stage):
        if self._closed:
            _fail(stage, kind="closed")
        response = None
        try:
            encoded = base64.encodebytes(_xxtea_encrypt(payload, self._key))
            deadline = time.monotonic() + self._timeout
            response = self._session.post(
                self._endpoint, data=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept-Encoding": "identity"},
                timeout=self._timeout, verify=True, allow_redirects=False, stream=True)
            if response.status_code != 200:
                _fail(stage, code=response.status_code, kind="http")
            if getattr(response, "history", None) or getattr(response, "url", self._endpoint) != self._endpoint:
                _fail(stage, kind="redirect")
            length = response.headers.get("Content-Length")
            if length is not None and (not str(length).isdigit() or int(length) > _MAX_WIRE):
                _fail(stage, kind="size")
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() > deadline:
                    _fail(stage, kind="timeout")
                chunks.extend(chunk)
                if len(chunks) > _MAX_WIRE:
                    _fail(stage, kind="size")
            clean = re.sub(rb"[\t\r\n ]", b"", bytes(chunks))
            try:
                encrypted = base64.b64decode(clean, validate=True)
            except (ValueError, binascii.Error):
                _fail(stage, kind="encoding")
            return _xxtea_decrypt(encrypted, self._key)
        except GuopanProtocolError:
            raise
        except Exception:
            _fail(stage, kind="network")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _request(self, operation, field, body, stage):
        common = self._common()
        header = (_uint(1, 1205) + _uint(2, self._header_uin) + _blob(3, self._login_key) +
                  _uint(4, 1) + _blob(7, common))
        inner = _uint(1, 1) + _uint(2, operation) + _blob(field, body)
        outer = _parse(self._post(_uint(1, 0) + _blob(2, header) + _blob(3, inner), stage))
        code = _int32(outer, 1, required=True)
        if code:
            _fail(stage, code=code, kind="auth" if code in (1001, 1002) else "server")
        response_header = _message(outer, 2)
        if _number(response_header, 1, required=True, bits=32) != 1205:
            _fail(stage, kind="command")
        result = _message(outer, 3)
        code = _int32(result, 1, required=True)
        _int32(result, 2, required=True)
        if code:
            kind = ("account_cancellation" if code == 1035 and
                    stage in ("login102", "login105") else "server")
            _fail(stage, code=code, kind=kind)
        return result

    def _login_request(self, subtype, username, password_hash=None, *, sms_code=None):
        body = _uint(1, subtype) + _blob(2, self._common()) + _blob(3, username)
        if password_hash is not None:
            body += _blob(4, password_hash)
        if sms_code is not None:
            body += _blob(5, sms_code)
        fields = _message(self._request(15, 17, body, f"login{subtype}"), 18)
        _int32(fields, 1, required=True)
        return fields

    def _adopt_auth(self, fields):
        key = _text(fields, 2, required=True)
        uin = _number(fields, 3, required=True)
        if not key or not uin:
            _fail("auth", kind="missing")
        self._login_key, self._uin = key, uin
        self._header_uin = uin

    def _profile_accounts(self):
        profile = _message(self._request(13, 15, _blob(1, self._common()), "profile"), 16)
        self._username = _text(profile, 1, required=True)
        certified = _number(profile, 19, bits=32, default=0) == 1
        age = _number(profile, 20, bits=32)
        listing = _message(self._request(37, 40, _uint(1, self.app_id) + _uint(2, self._uin), "accounts"), 41)
        accounts, originals = [], {}
        if len(listing.get(1, [])) > 256:
            _fail("accounts", kind="size")
        for wire, original in listing.get(1, []):
            if wire != 2:
                _fail("accounts")
            account = _parse(original)
            uin = _number(account, 2, required=True)
            game_uin = _text(account, 3, required=True)
            if not uin or not game_uin or game_uin in originals:
                _fail("accounts", kind="ambiguous")
            accounts.append({"uin": str(uin), "game_uin": game_uin, "name": game_uin})
            originals[game_uin] = original
        result = {"uin": str(self._uin), "login_key": self._login_key,
                  "certified": certified, "age": age, "accounts": accounts}
        self._accounts = originals
        self._result = copy.deepcopy(result)
        return result

    def login(self, username, password):
        self._reset()
        username = _input_text(username, "login", maximum=256)
        password = _input_text(password, "login", maximum=1024)
        try:
            salt_response = self._login_request(101, username, "")
            salt = _text(salt_response, 4, required=True)
            if not salt:
                _fail("salt", kind="missing")
            self._uin = _number(salt_response, 3, default=0)
            self._header_uin = self._uin
            self._login_key = _text(salt_response, 2)
            self._username = username
            digest = hashlib.md5((salt + password).encode("utf-8")).hexdigest()
            self._adopt_auth(self._login_request(102, username, digest))
            return self._profile_accounts()
        except Exception:
            self._reset()
            raise

    def request_sms(self, phone):
        self._reset()
        phone = _input_text(phone, "sms_send", maximum=64)
        self._request(3, 5, _blob(1, self._common()) + _blob(2, phone) +
                      _uint(3, 108), "sms_send")
        return {"sent": True}

    def login_sms(self, phone, code):
        self._reset()
        phone = _input_text(phone, "sms_login", maximum=64)
        code = _input_text(code, "sms_login", maximum=64)
        try:
            self._adopt_auth(self._login_request(105, phone, sms_code=code))
            self._username = phone
            return self._profile_accounts()
        except Exception:
            self._reset()
            raise

    def refresh(self, saved_session):
        self._reset()
        if not isinstance(saved_session, dict):
            _fail("refresh", kind="input")
        expected_uin = _identifier(saved_session.get("uin"), "refresh")
        self._login_key = _input_text(saved_session.get("login_key"), "refresh")
        try:
            self._adopt_auth(self._login_request(104, ""))
            if self._uin != expected_uin:
                _fail("refresh", kind="identity")
            return self._profile_accounts()
        except Exception:
            self._reset()
            raise

    def select_account(self, saved_session, game_uin):
        game_uin = _input_text(game_uin, "selection")
        if not isinstance(saved_session, dict):
            _fail("selection", kind="input")
        if (self._result is None or saved_session.get("uin") != self._result["uin"] or
                saved_session.get("login_key") != self._result["login_key"]):
            self._reset()
            self._uin = _identifier(saved_session.get("uin"), "selection")
            self._header_uin = self._uin
            self._login_key = _input_text(saved_session.get("login_key"), "selection")
            try:
                self._profile_accounts()
            except Exception:
                self._reset()
                raise
        result = copy.deepcopy(self._result)
        matches = [a for a in result["accounts"] if a["game_uin"] == game_uin]
        if len(matches) != 1 or game_uin not in self._accounts:
            _fail("selection", kind="account_unavailable")
        chosen = matches[0]
        if (saved_session.get("game_uin") == game_uin and saved_session.get("selected_uin") is not None
                and saved_session["selected_uin"] != chosen["uin"]):
            _fail("selection", kind="identity")
        self._request(43, 46, _uint(1, self.app_id) + _blob(2, self._accounts[game_uin]), "selection")
        self._header_uin = int(chosen["uin"])
        result.update(selected_uin=chosen["uin"], game_uin=game_uin)
        return result
