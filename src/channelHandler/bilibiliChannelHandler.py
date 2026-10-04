# coding=UTF-8
"""Bilibili (哔哩哔哩) 网页登录渠道。

登录流程：QWebEngine 打开 sdk.biligame.com/login/ → 用户 OTP 登录 → 拦截响应 → SAUTH 换 token。
"""
from __future__ import annotations

import time
import json
from typing import Any, Dict, Optional

import channelmgr
from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger

from channelHandler.channelUtils import buildSAUTH, postSignedData, getShortGameId
from channelHandler.bilibiliLogin.bilibiliChannel import BilibiliLogin, call_auto_login


class bilibiliChannel(channelmgr.channel):
    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        game_id: str = "",
        loginResp: Optional[Dict[str, Any]] = None,
        uuid: str = "",
    ) -> None:
        super().__init__(
            login_info,
            user_info,
            ext_info,
            device_info,
            create_time,
            last_login_time,
            name,
            uuid=uuid,
        )
        self.logger = setup_logger()
        self.crossGames = False
        self.game_id = game_id
        self.loginResp: Optional[Dict[str, Any]] = loginResp

        # 从 cloudRes 读取 bilibili 渠道配置
        short_gid = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData("bilibili_sdk", short_gid)
        if res and isinstance(res.get("bilibili_sdk"), dict):
            bili_cfg = res["bilibili_sdk"]
            bili_game_id = bili_cfg.get("bili_game_id", "183")
            bili_app_key = bili_cfg.get("app_key", "h9Ejat5tFh81cq8")
            bili_sdk_ver = bili_cfg.get("sdk_ver", "5.9.6")
        else:
            self.logger.warning(f"cloudRes 中未找到 bilibili_sdk 配置 (game_id={short_gid})，使用默认值")
            bili_game_id = "183"
            bili_app_key = "h9Ejat5tFh81cq8"
            bili_sdk_ver = "5.9.6"

        self.bili_sdk_ver = bili_sdk_ver
        self.biliLogin = BilibiliLogin(
            game_id=bili_game_id,
            app_key=bili_app_key,
            cache_game_id=short_gid,
        )

    # ── 序列化 / 反序列化 ────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            login_info=data.get("login_info", {}),
            user_info=data.get("user_info", {}),
            ext_info=data.get("ext_info", {}),
            device_info=data.get("device_info", {}),
            create_time=data.get("create_time", int(time.time())),
            last_login_time=data.get("last_login_time", 0),
            name=data.get("name", ""),
            game_id=data.get("game_id", ""),
            loginResp=data.get("loginResp"),
            uuid=data.get("uuid", ""),
        )

    def before_save(self):
        if self.loginResp is not None:
            json.dumps(self.loginResp)

    # ── 登录状态 ──────────────────────────────────────────────

    def _get_login_data(self) -> Optional[Dict[str, Any]]:
        """从 loginResp 中提取 data 字段（兼容 data 在根级或嵌套）。"""
        if not isinstance(self.loginResp, dict):
            return None
        data = self.loginResp.get("data")
        if isinstance(data, dict):
            return data
        # 某些响应格式中 uid/access_key 直接在根级
        if "uid" in self.loginResp and "access_key" in self.loginResp:
            return self.loginResp
        return None

    def is_token_valid(self) -> bool:
        data = self._get_login_data()
        if not data:
            return False
        access_key = str(data.get("access_key") or "").strip()
        if not access_key or not data.get("uid"):
            return False
        # 检查过期时间（留 5 分钟安全余量）
        expires = data.get("expires")
        if expires is not None:
            try:
                exp_ts = int(expires)
                if exp_ts < int(time.time()) + 300:
                    return False
            except (ValueError, TypeError):
                pass
        return True

    def validate_token_online(self) -> bool:
        """通过 auto.login API 验证并刷新 token。

        auto.login 响应为根级格式（无 data 包裹，无 access_key/expires），
        成功表示当前 access_key 仍有效。
        更新用户信息（uid/uname/face），并延长 expires 30 天。
        """
        data = self._get_login_data()
        if not data:
            return False
        access_key = str(data.get("access_key") or "").strip()
        if not access_key:
            return False
        try:
            resp = call_auto_login(access_key)
            if not isinstance(resp, dict) or resp.get("code") != 0:
                self.logger.warning(f"Bilibili auto.login 刷新失败: {resp}")
                return False

            self.logger.info("Bilibili auto.login 刷新成功")
            # auto.login 返回根级字段（uid/uname/face等），不含 access_key/expires
            refreshed_data = dict(data)
            for key in ("uid", "uname", "face"):
                if key in resp and resp[key] is not None:
                    refreshed_data[key] = resp[key]
            # access_key 验证通过，延长过期时间 30 天
            if not refreshed_data.get("uid"):
                return False
            refreshed_data["expires"] = int(time.time()) + 30 * 86400
            data.update(refreshed_data)
            self.observe_sdkuid(data["uid"])
            return True
        except Exception as e:
            self.logger.error(f"Bilibili auto.login 请求异常: {e}")
            return False

    # ── 登录 ──────────────────────────────────────────────────

    def request_user_login(self, on_complete=None, login_method="qr"):
        """请求用户登录。

        Args:
            on_complete: 异步回调，接收 bool。None 时为同步模式。
            login_method: "qr" 二维码扫码（默认）或 "web" 网页 OTP 登录。
        """
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        def _process_resp(resp):
            if resp is None:
                return None
            if not isinstance(resp, dict):
                return False
            data = resp.get("data", resp)
            if not isinstance(data, dict) or not data.get("uid") or not data.get("access_key"):
                return False
            self.loginResp = resp
            uname = str(data.get("uname") or "")
            if uname:
                self.name = uname
            self.mark_manual_login_success()
            return True

        def _on_done(resp):
            result = _process_resp(resp)
            if on_complete is not None:
                on_complete(result)
            return result

        if login_method == "qr":
            resp = self.biliLogin.qr_login()
            if resp is None and not self.biliLogin._qr_cancelled:
                resp = False
            result = _on_done(resp)
            return None if on_complete is not None else result

        if on_complete is not None:
            self.biliLogin.web_login(on_complete=_on_done)
            return None
        return _on_done(self.biliLogin.web_login())

    # ── UniSDK 数据 ──────────────────────────────────────────

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        game_id = game_id or self.game_id
        if not game_id:
            if on_complete is not None:
                on_complete(False)
                return None
            return False
        short_game_id = getShortGameId(game_id)

        def _deliver(result):
            if on_complete is not None:
                on_complete(result)
                return None
            return result

        def _build():
            try:
                return self._build_unisdk_result(short_game_id)
            except Exception:
                self.logger.exception("生成渠道登录数据失败")
                return False

        def _on_login_done(success):
            if success is not True:
                return _deliver(None if success is None else False)
            return _deliver(_build() if self.is_token_valid() else False)

        if self.is_token_valid():
            return _deliver(_build())
        if bool(self._get_login_data() and self._get_login_data().get("access_key")) and self.validate_token_online():
            return _deliver(_build() if self.is_token_valid() else False)
        if not interactive:
            return _deliver(False)
        if on_complete is not None:
            self.request_user_login(on_complete=_on_login_done, login_method="web")
            return None
        return _on_login_done(self.request_user_login())

    def _build_unisdk_result(self, short_game_id: str) -> Optional[Dict[str, Any]]:
        data = self._get_login_data()
        if not data:
            raise RuntimeError("bilibili loginResp 数据缺失")

        uid = str(data.get("uid") or "")
        access_key = str(data.get("access_key") or "")
        if not uid or not access_key:
            raise RuntimeError(f"bilibili 缺少 uid 或 access_key: uid={uid}")

        import base64 as b64mod

        sdk_version = self.bili_sdk_ver
        uniBody = buildSAUTH(
            login_channel="bilibili_sdk",
            app_channel="bilibili_sdk",
            uid=uid,
            session=access_key,
            game_id=short_game_id,
            sdk_version=sdk_version,
        )
        uniData = postSignedData(uniBody, short_game_id, True)
        uniSDKJSON = json.loads(
            b64mod.b64decode(uniData["unisdk_login_json"]).decode()
        )

        fd = app_state.fake_device

        # 构造 extra_unisdk_data（与 vivo/wechat 一致）
        extra_data = {
            "realname": json.dumps({"realname_type": 0, "age": 22}),
        }
        json_data = {
            "extra_data": extra_data.get("extra_data"),
            "get_access_token": "1",
            "sdk_udid": fd["udid"],
            "realname": extra_data.get("realname"),
        }
        json_data.update(uniBody)

        str_data = json_data.copy()
        str_data.update({"username": uniSDKJSON["username"]})
        str_data = "&".join([f"{k}={v}" for k, v in str_data.items()])

        extra_unisdk = json.dumps({
            "SAUTH_STR": b64mod.b64encode(str_data.encode()).decode(),
            "SAUTH_JSON": b64mod.b64encode(json.dumps(json_data).encode()).decode(),
            **extra_data,
        })

        return {
            "user_id": uid,
            "token": b64mod.b64encode(access_key.encode()).decode(),
            "login_channel": "bilibili_sdk",
            "udid": fd["udid"],
            "app_channel": "bilibili_sdk",
            "sdk_version": sdk_version,
            "jf_game_id": short_game_id,
            "pay_channel": "bilibili_sdk",
            "extra_data": "",
            "extra_unisdk_data": extra_unisdk,
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
