# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
from contextvars import copy_context
import json
import ipaddress
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
CHANNEL_NAME = "baidu"
from channelHandler.baiduLogin.protocol import BaiduProtocol


class baiduChannel(channelmgr.channel):
    def __init__(self, login_info, user_info=None, ext_info=None, device_info=None,
                 create_time=None, last_login_time=0, name="", game_id="",
                 baiduSession=None, baiduDeviceId=None, uuid=""):
        super().__init__(login_info, user_info or {}, ext_info or {}, device_info or {},
                         int(time.time()) if create_time is None else create_time,
                         last_login_time, name, uuid=uuid)
        self.crossGames = False
        self.game_id = getShortGameId(game_id)
        config = CloudRes().get_channelData(CHANNEL_NAME, self.game_id)
        if not isinstance(config, dict) or not isinstance(config.get(CHANNEL_NAME), dict):
            raise ValueError("此游戏尚无经过验证的百度渠道配置")
        self.channelConfig = dict(config[CHANNEL_NAME])
        self.baiduSession = dict(baiduSession) if isinstance(baiduSession, dict) else {}
        self.baiduDeviceId = (baiduDeviceId if isinstance(baiduDeviceId, str)
                              and re.fullmatch(r"[0-9a-f]{32}", baiduDeviceId)
                              else secrets.token_hex(16))
        self._active_browser = None
        self._login_pending = False
        self._baidu_packet_ready = False

    @classmethod
    def from_dict(cls, data):
        restored = cls(**{key: data[key] for key in (
            "login_info", "user_info", "ext_info", "device_info", "create_time",
            "last_login_time", "name", "game_id", "baiduSession", "baiduDeviceId", "uuid"
        ) if key in data}).restore_common(data)
        restored.game_id = getShortGameId(restored.game_id)
        restored.crossGames = False
        return restored

    def before_save(self):
        json.dumps(self.baiduSession)

    def is_token_valid(self):
        return all(isinstance(self.baiduSession.get(key), str) and self.baiduSession[key]
                   for key in ("uid", "access_token", "account_id", "auto_login_sign"))

    def cached_unisdk(self, game_id):
        return super().cached_unisdk(game_id) if self._baidu_packet_ready else None

    def cache_unisdk(self, packet, expires_at=None):
        result = super().cache_unisdk(packet, expires_at)
        self._baidu_packet_ready = True
        return result

    def request_unisdk(self, game_id="", on_complete=None, *, interactive=True, force=False, cancel_event=None):
        self._baidu_packet_ready = False
        return super().request_unisdk(game_id, on_complete, interactive=interactive, force=True, cancel_event=cancel_event)

    def _call_protocol(self, method, value):
        client = BaiduProtocol({**self.channelConfig, "device_id": self.baiduDeviceId})
        try:
            return getattr(client, method)(value)
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

    def _accept_session(self, result, *, interactive=False):
        if not isinstance(result, dict) or not all(
            isinstance(result.get(k), str) and result[k]
            for k in ("uid", "access_token", "account_id", "auto_login_sign")
        ):
            raise ValueError("field=baidu.session: missing required SDK credentials")
        previous = self.baiduSession.get("account_id")
        if not interactive and previous and result["account_id"] != previous:
            raise ValueError("field=baidu.account_id: differs from selected account")
        if not interactive and self.baiduSession.get("uid") and result["uid"] != self.baiduSession["uid"]:
            raise ValueError("field=baidu.uid: differs from selected account")
        self.baiduSession = dict(result)
        self.user_info = {"id": result["uid"], "token": result["access_token"]}
        self.observe_sdkuid(result["uid"], self.game_id)
        display = result.get("display_name") or result.get("user_name")
        if isinstance(display, str) and display.strip():
            self.name = display.strip()
        if interactive:
            self.mark_manual_login_success()

    def request_user_login(self, on_complete=None, *, interactive=True, cancel_event=None):
        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        if cancelled():
            return on_complete(None) if on_complete else None
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not interactive:
            def finish(result):
                if cancelled():
                    return on_complete(None) if on_complete else None
                if isinstance(result, Exception):
                    value = result
                else:
                    try:
                        self._accept_session(result)
                        value = True
                    except Exception as error:
                        value = error
                return on_complete(value) if on_complete else value
            if not self.is_token_valid():
                return on_complete(False) if on_complete else False
            op = lambda: self._call_protocol("refresh", self.baiduSession)
            if on_complete:
                return self._network(op, finish)
            return finish(self._network(op))
        if self._login_pending:
            return on_complete(False) if on_complete else False

        from channelHandler.baiduLogin.browser import BaiduBrowser
        self._login_pending = True
        try:
            browser = BaiduBrowser()
        except Exception:
            self._login_pending = False
            raise
        self._active_browser = browser
        completed = False

        def finish(result):
            self._login_pending = False
            if cancelled():
                return on_complete(None) if on_complete else None
            if isinstance(result, Exception):
                value = result
            else:
                try:
                    self._accept_session(result, interactive=True)
                    value = True
                except Exception as error:
                    value = error
            return on_complete(value) if on_complete else value

        def browser_done(ref):
            nonlocal completed
            if completed:
                return None
            completed = True
            self._active_browser = None
            bduss = ref.cookies.get("BDUSS", "") if ref.result else ""
            ref.bduss = ""
            ref.cookies = {}
            if cancelled() or not bduss:
                self._login_pending = False
                return on_complete(None) if on_complete else None
            op = lambda: self._call_protocol("login_by_bduss", bduss)
            if on_complete:
                return self._network(op, finish)
            return finish(self._network(op))

        if on_complete:
            browser._async_completion_callback = browser_done
        try:
            result = browser.run()
        except Exception:
            self._active_browser = None
            self._login_pending = False
            raise
        if result is None:
            if on_complete is None:
                browser._async_completion_callback = browser_done
            return None
        return browser_done(browser)

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
            raise ValueError("field=baidu.uni_sauth: missing game signing key")
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
            raise ValueError("field=baidu.uni_sauth: request failed") from None

    def _build_packet(self):
        login = self.baiduSession
        if not self.is_token_valid():
            raise ValueError("field=baidu.session: missing required SDK credentials")
        cfg = self.channelConfig
        sdk_version = cfg["sdk_ver"]
        app_channel = cfg["protocol_app_channel"]
        body = buildSAUTH(CHANNEL_NAME, app_channel, login["uid"], login["access_token"],
                          self.game_id, sdk_version,
                          {"is_unisdk_guest": int(bool(login.get("is_guest")))},
                          ip_address=self._public_ip())
        result = self._post_sauth(body)
        if not isinstance(result, dict) or result.get("code") != 200:
            raise ValueError("field=baidu.uni_sauth: provider rejected game credentials")
        try:
            decoded = json.loads(base64.b64decode(result["unisdk_login_json"], validate=True))
            username = decoded["username"]
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise ValueError("field=baidu.uni_sauth: invalid login response") from None
        if not isinstance(username, str) or not username:
            raise ValueError("field=baidu.uni_sauth: missing username")
        auth = {"sdk_udid": app_state.fake_device["udid"], **body}
        auth_string = "&".join(f"{k}={v}" for k, v in {**auth, "username": username}.items())
        extra = {
            "SAUTH_STR": base64.b64encode(auth_string.encode()).decode(),
            "SAUTH_JSON": base64.b64encode(json.dumps(auth).encode()).decode(),
        }
        return {
            "user_id": login["uid"],
            "token": base64.b64encode(login["access_token"].encode()).decode(),
            "login_channel": CHANNEL_NAME, "udid": app_state.fake_device["udid"],
            "app_channel": app_channel, "sdk_version": sdk_version,
            "jf_game_id": self.game_id, "pay_channel": CHANNEL_NAME,
            "extra_data": "", "extra_unisdk_data": json.dumps(extra),
            "gv": str(cfg["version_code"]), "gvn": cfg["version_name"], "cv": "a1.5.0",
        }

    def get_uniSdk_data(self, game_id="", on_complete=None, *, interactive=True):
        if getShortGameId(game_id or self.game_id) != self.game_id:
            raise ValueError("field=baidu.game_id: differs from selected game")
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        def deliver(result):
            return on_complete(result) if on_complete else result

        def build():
            return self._network(self._build_packet, on_complete) if on_complete else self._network(self._build_packet)

        def logged_in(result):
            return build() if result is True else deliver(result)

        def refreshed(result):
            if isinstance(result, Exception):
                if interactive and getattr(result, "code", None) in (36005, 36100):
                    if on_complete:
                        return self.request_user_login(logged_in)
                    return logged_in(self.request_user_login())
                return deliver(result)
            try:
                self._accept_session(result)
            except Exception as error:
                return deliver(error)
            return build()

        if self.is_token_valid():
            op = lambda: self._call_protocol("refresh", self.baiduSession)
            return self._network(op, refreshed) if on_complete else refreshed(self._network(op))
        if not interactive:
            return deliver(False)
        if on_complete:
            return self.request_user_login(logged_in)
        return logged_in(self.request_user_login())
