# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import base64
import json
import time
import threading
from contextvars import copy_context
import uuid as uuidlib

import app_state
import channelmgr
from channelHandler.channelUtils import buildSAUTH, getShortGameId, postSignedData
from cloudRes import CloudRes
from channelHandler.lenovoLogin.protocol import InvalidTgtError, LenovoClient, LoginError, st_is_valid


def _b64(value):
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


class lenovoChannel(channelmgr.channel):
    def __init__(self, login_info, user_info=None, ext_info=None, device_info=None,
                 create_time=None, last_login_time=0, name="", uuid="", game_id="",
                 lenovoSession=None, lenovo_device_id=""):
        super().__init__(login_info, user_info or {}, ext_info or {}, device_info or {},
                         int(time.time()) if create_time is None else create_time,
                         last_login_time, name, uuid=uuid)
        self.game_id = game_id
        entry = CloudRes().get_channelData("lenovo_open", getShortGameId(game_id))
        if not isinstance(entry, dict) or not isinstance(entry.get("lenovo_open"), dict):
            raise LoginError("此游戏尚无联想渠道配置，请刷新云配置后重试")
        self.channelConfig = {**entry["lenovo_open"], "package_name": entry.get("package_name", "")}
        self.crossGames = False
        self.lenovoSession = dict(lenovoSession or {})
        self.lenovo_device_id = lenovo_device_id or uuidlib.uuid4().hex
        self._lenovo_packet_ready = False
        self.active_sdkuid = "0" if self.lenovoSession.get("st") else ""
        self._login_dialog = None

    @classmethod
    def from_dict(cls, data):
        keys = ("login_info", "user_info", "ext_info", "device_info", "create_time",
                "last_login_time", "name", "uuid", "game_id", "lenovoSession", "lenovo_device_id")
        restored = cls(**{k: data[k] for k in keys if k in data})
        restored.restore_common(data)
        restored.crossGames = False
        restored._lenovo_packet_ready = False
        return restored

    def restore_common(self, data):
        super().restore_common(data)
        self.crossGames = False
        self._lenovo_packet_ready = False
        return self

    def _client(self):
        return LenovoClient(self.lenovo_device_id, self.channelConfig)

    def _check_game(self, game_id):
        if getShortGameId(game_id or "") != "h55":
            raise LoginError("联想账号目前仅支持第五人格 h55")

    def _store(self, result):
        if not isinstance(result, dict) or not all(result.get(k) for k in ("tgt", "st", "account_id")):
            raise LoginError("联想未返回完整登录会话")
        self.lenovoSession = {k: result[k] for k in
                              ("tgt", "st", "account_id", "realname", "tgt_ttl", "tgt_issued_at",
                               "st_ttl", "st_issued_at") if k in result}
        self.unisdk_cache.clear()
        self.observe_sdkuid("0")
        self._lenovo_packet_ready = False

    def is_token_valid(self):
        return bool(self.lenovoSession.get("tgt") and self.lenovoSession.get("account_id"))

    def cached_unisdk(self, game_id):
        if not self._lenovo_packet_ready or not st_is_valid(self.lenovoSession):
            return None
        return super().cached_unisdk(game_id)

    def cache_unisdk(self, packet, expires_at=None):
        result = super().cache_unisdk(packet, expires_at)
        self._lenovo_packet_ready = True
        return result

    def request_unisdk(self, game_id="", on_complete=None, *, interactive=True, force=False):
        self._lenovo_packet_ready = False
        return super().request_unisdk(game_id, on_complete, interactive=interactive, force=True)

    def request_user_login(self, on_complete=None, *, interactive=True, force=False):
        self._check_game(self.game_id)
        def deliver(value):
            if on_complete is not None:
                on_complete(value)
                return None
            return value
        if self.is_token_valid() and not (interactive and force):
            saved = dict(self.lenovoSession)
            def refresh():
                client = self._client()
                try:
                    return client.resume(saved)
                finally:
                    client.http.close()
            def refreshed(result):
                if isinstance(result, InvalidTgtError) and interactive:
                    return self.request_user_login(on_complete=on_complete, force=True)
                if isinstance(result, Exception):
                    return deliver(result)
                self._store(result)
                return deliver(True)
            if on_complete is not None:
                self._background(refresh, refreshed)
                return None
            try:
                result = refresh()
            except InvalidTgtError:
                if interactive:
                    return self.request_user_login(force=True)
                raise
            self._store(result)
            return True
        if not interactive:
            return deliver(False)
        if self._login_dialog is not None:
            return deliver(LoginError("联想登录窗口已打开，请先完成或取消当前登录"))
        from channelHandler.lenovoLogin.dialog import show_login
        def done(result):
            self._login_dialog = None
            if result is None:
                return deliver(None)
            try:
                self._store(result)
                self.mark_manual_login_success()
            except LoginError as error:
                return deliver(error)
            except Exception:
                return deliver(LoginError("联想账号会话保存失败，请重试"))
            return deliver(True)
        if on_complete is not None:
            self._login_dialog = show_login(self._client, done)
            return None
        return done(show_login(self._client))

    def get_uniSdk_data(self, game_id="", on_complete=None, *, interactive=True):
        game_id = game_id or self.game_id
        self._check_game(game_id)
        def done(success):
            if success is True and on_complete is not None:
                self._background(self._build_packet, on_complete)
                return None
            result = self._build_packet() if success is True else success
            if on_complete is not None:
                on_complete(result)
                return None
            return result
        if on_complete is not None:
            self.request_user_login(on_complete=done, interactive=interactive)
            return None
        return done(self.request_user_login(interactive=interactive))

    @staticmethod
    def _background(action, on_complete):
        def worker():
            try:
                result = action()
            except LoginError as error:
                result = error
            except Exception:
                result = LoginError("联想登录校验失败，请稍后重试")
            app_state.run_on_main_thread(lambda: on_complete(result))
        context = copy_context()
        threading.Thread(target=lambda: context.run(worker), daemon=True).start()

    def _build_packet(self):
        session = self.lenovoSession
        if not session.get("st"):
            raise LoginError("缺少联想游戏凭证")
        extra = {"deviceid": self.lenovo_device_id}
        realname = session.get("realname")
        if realname is not None:
            extra["realname"] = json.dumps(realname, separators=(",", ":"))
        config = self.channelConfig
        body = buildSAUTH("lenovo_open", config["protocol_app_channel"], "0", session["st"], "h55", config["sdk_ver"],
                          custom_data=extra)
        result = postSignedData(body, "h55", True)
        if not isinstance(result, dict) or result.get("code") != 200:
            raise LoginError("网易未接受联想渠道凭证，请在官方客户端确认账号后重试")
        try:
            login = json.loads(base64.b64decode(result["unisdk_login_json"], validate=True))
            username = login["username"]
            if not isinstance(username, str) or not username:
                raise ValueError()
        except (KeyError, ValueError, TypeError, UnicodeError):
            raise LoginError("网易返回的联想登录数据格式异常") from None
        auth = {"get_access_token": "1", "sdk_udid": app_state.fake_device["udid"], **body}
        sauth_str = "&".join(f"{key}={value}" for key, value in {**auth, "username": username}.items())
        unisdk = {"SAUTH_STR": _b64(sauth_str), "SAUTH_JSON": _b64(json.dumps(auth))}
        if "realname" in extra:
            unisdk["realname"] = extra["realname"]
        return {"user_id": "0", "token": _b64(session["st"]), "login_channel": "lenovo_open",
                "udid": app_state.fake_device["udid"], "app_channel": config["protocol_app_channel"], "sdk_version": config["sdk_ver"],
                "jf_game_id": "h55", "pay_channel": "lenovo_open", "extra_data": "",
                "extra_unisdk_data": json.dumps(unisdk), "gv": str(config["version_code"]), "gvn": config["version_name"], "cv": "a1.5.0"}
