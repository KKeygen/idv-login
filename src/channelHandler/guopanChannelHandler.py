# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
from contextvars import copy_context
import ipaddress
import json
import re
import secrets
import threading
import time

import requests

import app_state
import channelmgr
from cloudRes import CloudRes
from envmgr import genv
from channelHandler.channelUtils import buildSAUTH, calcSign, CustomEncoder, getShortGameId
CHANNEL_NAME = "guopan"
from channelHandler.guopanLogin.protocol import GuopanClient, GuopanProtocolError


class guopanChannel(channelmgr.channel):
    def __init__(self, login_info, user_info=None, ext_info=None, device_info=None,
                 create_time=None, last_login_time=0, name="", game_id="",
                 guopanSession=None, guopanDeviceId=None, uuid=""):
        super().__init__(login_info, user_info or {}, ext_info or {}, device_info or {},
                         int(time.time()) if create_time is None else create_time,
                         last_login_time, name, uuid=uuid)
        self.crossGames = False
        self.game_id = getShortGameId(game_id)
        config = CloudRes().get_channelData(CHANNEL_NAME, self.game_id)
        if not isinstance(config, dict) or not isinstance(config.get(CHANNEL_NAME), dict):
            raise ValueError("此游戏尚无经过验证的果盘渠道配置")
        self.channelConfig = dict(config[CHANNEL_NAME])
        self.guopanSession = self._saved_fields(guopanSession or {})
        self.guopanDeviceId = (guopanDeviceId if isinstance(guopanDeviceId, str)
                               and re.fullmatch(r"[0-9a-f]{32}", guopanDeviceId)
                               else secrets.token_hex(16))
        self._active_dialog = None
        self._login_pending = False
        self._guopan_packet_ready = False

    @staticmethod
    def _saved_fields(data):
        if not isinstance(data, dict):
            return {}
        return {key: data[key] for key in (
            "uin", "login_key", "selected_uin", "game_uin", "name", "certified", "age"
        ) if key in data}

    @classmethod
    def from_dict(cls, data):
        restored = cls(**{key: data[key] for key in (
            "login_info", "user_info", "ext_info", "device_info", "create_time",
            "last_login_time", "name", "game_id", "guopanSession", "guopanDeviceId", "uuid"
        ) if key in data}).restore_common(data)
        restored.game_id = getShortGameId(restored.game_id)
        restored.crossGames = False
        return restored

    def before_save(self):
        self.guopanSession = self._saved_fields(self.guopanSession)
        json.dumps(self.guopanSession)

    def is_token_valid(self):
        return all(isinstance(self.guopanSession.get(key), str) and self.guopanSession[key]
                   for key in ("uin", "login_key", "selected_uin", "game_uin"))

    def cached_unisdk(self, game_id):
        return super().cached_unisdk(game_id) if self._guopan_packet_ready else None

    def cache_unisdk(self, packet, expires_at=None):
        result = super().cache_unisdk(packet, expires_at)
        self._guopan_packet_ready = True
        return result

    def request_unisdk(self, game_id="", on_complete=None, *, interactive=True, force=False, cancel_event=None):
        self._guopan_packet_ready = False
        return super().request_unisdk(game_id, on_complete, interactive=interactive, force=True, cancel_event=cancel_event)

    def _client(self):
        return GuopanClient(self.channelConfig["app_id"], self.channelConfig["app_key"],
                            device_info={"device_id": self.guopanDeviceId})

    def _protocol(self, method, *args):
        client = self._client()
        try:
            return getattr(client, method)(*args)
        finally:
            client.close()

    @staticmethod
    def _network(operation, on_complete=None):
        def execute():
            try:
                return operation()
            except Exception as error:
                return error
        if on_complete is None:
            return execute()
        context = copy_context()
        def worker():
            result = context.run(execute)
            app_state.run_on_main_thread(lambda: on_complete(result))
        threading.Thread(target=worker, daemon=True).start()
        return None

    @staticmethod
    def _require_profile(result):
        if not isinstance(result, dict) or not all(
            isinstance(result.get(k), str) and result[k] for k in ("uin", "login_key")
        ):
            raise ValueError("field=guopan.session: missing provider credentials")
        return result

    def _accept_session(self, result, *, interactive=False):
        self._require_profile(result)
        if not all(isinstance(result.get(k), str) and result[k]
                   for k in ("selected_uin", "game_uin")):
            raise ValueError("field=guopan.selection: missing server confirmed identity")
        if not interactive:
            for key in ("uin", "selected_uin", "game_uin"):
                if self.guopanSession.get(key) != result[key]:
                    raise ValueError("field=guopan.identity: differs from selected account")
        self.guopanSession = self._saved_fields(result)
        self.user_info = {"id": result["game_uin"], "token": result["login_key"]}
        self.observe_sdkuid(result["game_uin"], self.game_id)
        name = result.get("name")
        if isinstance(name, str) and name.strip():
            self.name = name.strip()
        if interactive:
            self.mark_manual_login_success()
        return True

    def _refresh(self):
        previous = dict(self.guopanSession)
        client = self._client()
        try:
            result = self._require_profile(client.refresh(previous))
            if result["uin"] != previous["uin"]:
                raise ValueError("field=guopan.identity: differs from selected account")
            accounts = result.get("accounts")
            if not isinstance(accounts, list):
                raise ValueError("field=guopan.accounts: missing provider account list")
            matching = [a for a in accounts if isinstance(a, dict)
                        and a.get("game_uin") == previous["game_uin"]
                        and a.get("uin") == previous["selected_uin"]]
            if len(matching) != 1:
                raise ValueError("field=guopan.selection: previous account unavailable")
            return client.select_account(result, previous["game_uin"])
        finally:
            client.close()

    def request_user_login(self, on_complete=None, *, interactive=True):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not interactive:
            if not self.is_token_valid():
                return on_complete(False) if on_complete else False
            def refreshed(result):
                if not isinstance(result, Exception):
                    try:
                        result = self._accept_session(result)
                    except Exception as error:
                        result = error
                return on_complete(result) if on_complete else result
            return self._network(self._refresh, refreshed) if on_complete else refreshed(self._network(self._refresh))
        if self._login_pending:
            return on_complete(False) if on_complete else False
        from channelHandler.guopanLogin.dialog import GuopanLoginDialog
        self._login_pending = True
        completed = False

        def authenticate(username, password, callback):
            def operation():
                return self._require_profile(self._protocol("login", username, password))
            self._network(operation, callback)

        def send_sms(phone, callback):
            self._network(lambda: self._protocol("request_sms", phone), callback)

        def sms_authenticate(phone, code, callback):
            self._network(lambda: self._require_profile(self._protocol("login_sms", phone, code)), callback)

        def select_account(session, game_uin, callback):
            self._network(lambda: self._protocol("select_account", session, game_uin), callback)

        def finished(result):
            nonlocal completed
            if completed:
                return None
            completed = True
            self._login_pending = False
            self._active_dialog = None
            if result is not None:
                try:
                    result = self._accept_session(result, interactive=True)
                except Exception as error:
                    result = error
            return on_complete(result) if on_complete else result

        try:
            dialog = GuopanLoginDialog(authenticate, select_account, finished if on_complete else None,
                                       send_sms=send_sms, sms_authenticate=sms_authenticate)
            self._active_dialog = dialog
            if on_complete:
                dialog.show()
                return None
            dialog.exec()
            return finished(dialog.result_value)
        except Exception:
            self._login_pending = False
            self._active_dialog = None
            raise

    @staticmethod
    def _public_ip():
        try:
            with requests.get("https://who.nie.netease.com/", timeout=5,
                              verify=True, allow_redirects=False, stream=True) as response:
                if response.status_code == 200:
                    raw = bytearray()
                    for chunk in response.iter_content(chunk_size=4096):
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            return "127.0.0.1"
                    return str(ipaddress.ip_address(json.loads(raw).get("ip")))
        except Exception:
            pass
        return "127.0.0.1"

    def _post_sauth(self, body):
        url = f"https://mgbsdk.matrix.netease.com/{self.game_id}/sdk/uni_sauth"
        key = CloudRes().get_by_game_id_and_key(self.game_id, "log_key")
        if not isinstance(key, str) or not key:
            raise ValueError("field=guopan.uni_sauth: missing game signing key")
        payload = json.dumps(body, cls=CustomEncoder)
        headers = {"X-Client-Sign": calcSign(url, "POST", payload, key),
                   "Content-Type": "application/json",
                   "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)"}
        try:
            with requests.post(url, data=payload, headers=headers, timeout=15,
                               verify=True, allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError
                raw = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ValueError
                return json.loads(raw)
        except Exception:
            raise ValueError("field=guopan.uni_sauth: request failed") from None

    def _build_packet(self):
        login = self._require_profile(self.guopanSession)
        if not self.is_token_valid():
            raise ValueError("field=guopan.session: missing provider credentials")
        cfg = self.channelConfig
        body = buildSAUTH(CHANNEL_NAME, cfg["protocol_app_channel"], login["game_uin"],
                          login["login_key"], self.game_id, cfg["sdk_ver"],
                          ip_address=self._public_ip())
        result = self._post_sauth(body)
        if not isinstance(result, dict) or result.get("code") != 200:
            raise ValueError("field=guopan.uni_sauth: provider rejected game credentials")
        try:
            decoded = json.loads(base64.b64decode(result["unisdk_login_json"], validate=True))
            username = decoded["username"]
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise ValueError("field=guopan.uni_sauth: invalid login response") from None
        if not isinstance(username, str) or not username:
            raise ValueError("field=guopan.uni_sauth: missing username")
        auth = {"sdk_udid": app_state.fake_device["udid"], **body}
        auth_string = "&".join(f"{k}={v}" for k, v in {**auth, "username": username}.items())
        extra = {"SAUTH_STR": base64.b64encode(auth_string.encode()).decode(),
                 "SAUTH_JSON": base64.b64encode(json.dumps(auth).encode()).decode()}
        return {"user_id": login["game_uin"],
                "token": base64.b64encode(login["login_key"].encode()).decode(),
                "login_channel": CHANNEL_NAME, "udid": app_state.fake_device["udid"],
                "app_channel": cfg["protocol_app_channel"], "sdk_version": cfg["sdk_ver"],
                "jf_game_id": self.game_id, "pay_channel": CHANNEL_NAME,
                "extra_data": "", "extra_unisdk_data": json.dumps(extra),
                "gv": str(cfg["version_code"]), "gvn": cfg["version_name"], "cv": "a1.5.0"}

    def get_uniSdk_data(self, game_id="", on_complete=None, *, interactive=True):
        if getShortGameId(game_id or self.game_id) != self.game_id:
            raise ValueError("field=guopan.game_id: differs from selected game")
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        def deliver(result):
            return on_complete(result) if on_complete else result
        def build():
            return self._network(self._build_packet, on_complete) if on_complete else self._network(self._build_packet)
        def logged_in(result):
            return build() if result is True else deliver(result)
        def refreshed(result):
            if isinstance(result, Exception):
                if (interactive and isinstance(result, GuopanProtocolError)
                        and result.kind == "auth" and result.code in (1001, 1002)):
                    return (self.request_user_login(logged_in) if on_complete
                            else logged_in(self.request_user_login()))
                return deliver(result)
            try:
                self._accept_session(result)
            except Exception as error:
                return deliver(error)
            return build()
        if self.is_token_valid():
            return self._network(self._refresh, refreshed) if on_complete else refreshed(self._network(self._refresh))
        if not interactive:
            return deliver(False)
        return self.request_user_login(logged_in) if on_complete else logged_in(self.request_user_login())
