# coding=UTF-8
"""UC/九游 (uc_platform) 登录渠道。

登录方式：SMS 短信验证码。
登录流程：认证 → SID + refreshToken → SAUTH 换 token。
会话续期：refreshToken → refreshLogin → 新 SID。
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
from channelHandler.ucLogin.ucChannel import UCLogin
from channelHandler.ucLogin.consts import UC_SDK_VERSION, UC_GAME_ID, UC_H55_VERSION_CODE, UC_H55_VERSION_NAME


class ucChannel(channelmgr.channel):
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
        ucSession: Optional[Dict[str, Any]] = None,
        uuid: str = "",
        refreshToken: str = "",
        sid_expire_time: int = 0,
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
        self.ucSession: Optional[Dict[str, Any]] = ucSession
        self.refreshToken: str = refreshToken
        self.sid_expire_time: int = sid_expire_time

        short_gid = getShortGameId(game_id)
        cloudRes = CloudRes()
        res = cloudRes.get_channelData("uc_platform", short_gid)
        if res and isinstance(res.get("uc_platform"), dict):
            uc_cfg = res["uc_platform"]
            self.uc_sdk_ver = uc_cfg.get("sdk_ver", UC_SDK_VERSION)
            self.uc_game_id = uc_cfg.get("uc_game_id", UC_GAME_ID)
            self.uc_version_code = uc_cfg.get("version_code", UC_H55_VERSION_CODE)
            self.uc_version_name = uc_cfg.get("version_name", UC_H55_VERSION_NAME)
        else:
            self.uc_sdk_ver = UC_SDK_VERSION
            self.uc_game_id = UC_GAME_ID
            self.uc_version_code = UC_H55_VERSION_CODE
            self.uc_version_name = UC_H55_VERSION_NAME

        self.ucLogin = UCLogin(
            game_id=short_gid,
            uc_game_id=self.uc_game_id,
            uc_version_code=self.uc_version_code,
            uc_version_name=self.uc_version_name,
        )

    # ── 序列化 / 反序列化 ────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict):
        # refreshToken 可能存在 ucSession 内或顶层
        uc_session = data.get("ucSession")
        refresh = data.get("refreshToken", "")
        if not refresh and isinstance(uc_session, dict):
            refresh = uc_session.get("refreshToken", "")
        return cls(
            login_info=data.get("login_info", {}),
            user_info=data.get("user_info", {}),
            ext_info=data.get("ext_info", {}),
            device_info=data.get("device_info", {}),
            create_time=data.get("create_time", int(time.time())),
            last_login_time=data.get("last_login_time", 0),
            name=data.get("name", ""),
            game_id=data.get("game_id", ""),
            ucSession=uc_session,
            uuid=data.get("uuid", ""),
            refreshToken=refresh,
            sid_expire_time=data.get("sid_expire_time", 0),
        )

    def before_save(self):
        if self.ucSession is not None:
            json.dumps(self.ucSession)

    # ── 登录状态 ──────────────────────────────────────────────

    def _get_session_data(self) -> Optional[Dict[str, Any]]:
        """从 ucSession 提取会话数据。"""
        if not isinstance(self.ucSession, dict):
            return None
        if "sid" in self.ucSession:
            return self.ucSession
        data = self.ucSession.get("data")
        if isinstance(data, dict) and "sid" in data:
            return data
        return None

    def is_token_valid(self) -> bool:
        data = self._get_session_data()
        if data:
            sid = str(data.get("sid") or "").strip()
            account_id = str(data.get("accountId") or data.get("ucid") or "").strip()
            if sid and account_id:
                return True
        # sid 可能已过期，但有 refreshToken 仍可续期
        if self.refreshToken:
            return True
        return False

    def _store_session(self, session_data: Optional[Dict[str, Any]]) -> bool:
        """保存登录结果并提取 refreshToken，计算 sid 过期时间。"""
        if not session_data:
            self.ucSession = None
            return False
        self.ucSession = session_data
        # 提取 refreshToken
        rt = session_data.get("refreshToken", "")
        if rt:
            self.refreshToken = rt
        # 计算 sid 过期时间（timeout 是持续秒数，默认 86400 = 24h）
        timeout = session_data.get("timeout", 86400)
        self.sid_expire_time = int(time.time()) + int(timeout)
        data = self._get_session_data()
        if data:
            # SMS 登录响应中 mobile 字段为脱敏手机号，优先用作显示名
            display = str(data.get("mobile") or data.get("nickName") or data.get("userName") or "")
            if display:
                self.name = display
        return True

    # ── 会话刷新 ──────────────────────────────────────────────

    def _try_refresh(self) -> bool:
        """尝试用 refreshToken 续期 session。成功返回 True。"""
        data = self._get_session_data()
        old_sid = str(data.get("sid", "")) if data else ""
        if not self.refreshToken:
            return False
        if not old_sid:
            return False
        try:
            new_data = self.ucLogin.do_refresh(old_sid, self.refreshToken)
            if new_data and isinstance(new_data, dict) and new_data.get("sid"):
                self._store_session(new_data)
                self.logger.debug("UC session 自动续期成功")
                return True
        except Exception:
            self.logger.exception("UC 会话续期异常")
        return False

    # ── 登录 ──────────────────────────────────────────────────

    def request_user_login(self, on_complete=None):
        """请求用户登录（SMS 短信验证码）。"""
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        def _on_done(session_data):
            if session_data is None:
                if on_complete is not None:
                    on_complete(None)
                    return
                return False
            try:
                success = self._store_session(session_data)
            except Exception:
                self.logger.exception("UC 异步登录处理失败")
                success = False
            if on_complete is not None:
                on_complete(success)
                return
            return success

        if on_complete is not None:
            self.ucLogin.sms_login_dialog(on_complete=_on_done)
            return
        session_data = self.ucLogin.sms_login_dialog()
        return self._store_session(session_data)

    # ── UniSDK 数据 ──────────────────────────────────────────

    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        if not game_id:
            raise RuntimeError("uc_platform 缺少 game_id")

        short_game_id = getShortGameId(game_id)

        # 尝试 refreshToken 续期（如有）
        if not self._has_valid_sid() and self.refreshToken:
            self._try_refresh()

        if not self._has_valid_sid():
            # 需要重新登录
            if on_complete is not None:
                def _on_login_done(success):
                    if success and self._has_valid_sid():
                        try:
                            result = self._build_unisdk_result(short_game_id)
                            on_complete(result)
                        except Exception as e:
                            self.logger.error(f"UC 生成登录数据失败: {e}")
                            on_complete(None)
                    else:
                        on_complete(None)

                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                self.request_user_login()

        result = self._build_unisdk_result(short_game_id)
        if on_complete is not None:
            on_complete(result)
            return None
        return result

    def _has_valid_sid(self) -> bool:
        """检查 ucSession 是否有有效的 sid + accountId，且未过期。"""
        data = self._get_session_data()
        if not data:
            return False
        sid = str(data.get("sid") or "").strip()
        account_id = str(data.get("accountId") or data.get("ucid") or "").strip()
        if not (sid and account_id):
            return False
        # 检查 sid 是否已过期
        if self.sid_expire_time > 0 and int(time.time()) >= self.sid_expire_time:
            self.logger.debug("UC sid 已过期，需要续期")
            return False
        return True

    def _build_unisdk_result(self, short_game_id: str) -> Optional[Dict[str, Any]]:
        data = self._get_session_data()
        if not data:
            raise RuntimeError("UC ucSession 数据缺失")

        sid = str(data.get("sid") or "")
        account_id = str(data.get("accountId") or data.get("ucid") or "")
        if not sid or not account_id:
            raise RuntimeError(
                f"UC 缺少 sid 或 accountId: sid={sid}, accountId={account_id}"
            )

        import base64 as b64mod

        sdk_version = self.uc_sdk_ver
        uniBody = buildSAUTH(
            login_channel="uc_platform",
            app_channel="uc_platform",
            uid=account_id,
            session=sid,
            game_id=short_game_id,
            sdk_version=sdk_version,
        )
        uniData = postSignedData(uniBody, short_game_id, True)
        uniSDKJSON = json.loads(
            b64mod.b64decode(uniData["unisdk_login_json"]).decode()
        )

        fd = app_state.fake_device

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
            "user_id": account_id,
            "token": b64mod.b64encode(sid.encode()).decode(),
            "login_channel": "uc_platform",
            "udid": fd["udid"],
            "app_channel": "uc_platform",
            "sdk_version": sdk_version,
            "jf_game_id": short_game_id,
            "pay_channel": "uc_platform",
            "extra_data": "",
            "extra_unisdk_data": extra_unisdk,
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
