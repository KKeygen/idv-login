# coding=UTF-8
"""360（奇虎）渠道。

登录方式：Web 登录 https://i.360.cn/login/wap 取得 Q/T cookie，
          再用该 cookie 换取 access_token（免密码，可长期保存）。

会话续期：本地 cookie 仍有效时直接复验；失效则重新拉起浏览器。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, Optional

import channelmgr

from cloudRes import CloudRes
from envmgr import genv
import app_state
from logutil import setup_logger

from channelHandler.channelUtils import buildSAUTH, postSignedData, getShortGameId
from channelHandler.qihooLogin.qihooChannel import QihooLogin
from channelHandler.qihooLogin import consts as QC

TAG = "[360]"
CHANNEL_NAME = "360_assistant"
UUID_PREFIX = "360-"


class qihooChannel(channelmgr.channel):
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
        qt_cookie: str = "",
        userInfo: Optional[Dict[str, Any]] = None,
        token_expire_time: int = 0,
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
        # Web 登录拿到的 Q/T cookie（长期有效，可复验）
        self.qt_cookie: str = qt_cookie or ""
        # 登录接口返回的原始 data（含 qid / qoauth_token）
        self.userInfo: Optional[Dict[str, Any]] = userInfo
        # access_token 过期时间戳（expires_in 约 10 小时）
        self.token_expire_time: int = int(token_expire_time or 0)

        self.app_id, self.app_key, self.protocol_app_channel, self.sdk_ver = \
            self._load_config(game_id)
        self.qihooLogin = QihooLogin(app_id=self.app_id, app_key=self.app_key)

    # ── 配置 ─────────────────────────────────────────────────
    def _load_config(self, game_id: str):
        """从 cloudRes 读取 360 渠道配置，缺省回落到内置值。"""
        app_id = None
        app_key = None
        protocol_app_channel = QC.APP_CHANNEL_FALLBACK
        sdk_ver = QC.UNISDK_SDK_VERSION
        source = "内置默认值"

        try:
            short_gid = getShortGameId(game_id) if game_id else ""
            res = CloudRes().get_channelData(CHANNEL_NAME, short_gid) if short_gid else None
            if res and isinstance(res.get(CHANNEL_NAME), dict):
                cfg = res[CHANNEL_NAME]
                source = "cloudRes"
                app_id = cfg.get("app_id")
                app_key = cfg.get("app_key")
                protocol_app_channel = cfg.get("protocol_app_channel") or protocol_app_channel
                sdk_ver = cfg.get("sdk_ver") or sdk_ver
            else:
                self.logger.warning(
                    f"{TAG} cloudRes 中未找到 {CHANNEL_NAME} 配置 "
                    f"(game_id={game_id})，使用内置默认值"
                )
        except Exception:
            self.logger.exception(f"{TAG} 读取 360 渠道配置失败，使用内置默认值")

        if not app_id:
            app_id = QC.DEFAULT_APP_ID
        if not app_key:
            app_key = QC.DEFAULT_APP_KEY
        try:
            app_id = int(app_id)
        except (TypeError, ValueError):
            self.logger.warning(f"{TAG} 360 app_id 非法: {app_id!r}，回落到默认值")
            app_id = QC.DEFAULT_APP_ID

        self.logger.info(
            f"{TAG} 配置来源={source} app_id={app_id} "
            f"app_channel={protocol_app_channel} sdk_ver={sdk_ver}"
        )
        return app_id, app_key, protocol_app_channel, sdk_ver

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
            qt_cookie=data.get("qt_cookie", ""),
            userInfo=data.get("userInfo"),
            token_expire_time=data.get("token_expire_time", 0),
            uuid=data.get("uuid", ""),
        )

    def before_save(self):
        if self.userInfo is not None:
            json.dumps(self.userInfo)

    # ── 登录状态 ─────────────────────────────────────────────
    def _credentials(self) -> dict:
        return QihooLogin.extract_credentials(self.userInfo or {})

    def _has_valid_token(self) -> bool:
        cred = self._credentials()
        if not (cred["qid"] and cred["access_token"]):
            return False
        # expires_in 约 10 小时；留 5 分钟余量
        if self.token_expire_time and int(time.time()) >= self.token_expire_time - 300:
            self.logger.info(f"{TAG} access_token 已过期，需要重新获取")
            return False
        return True

    def is_token_valid(self) -> bool:
        # 有有效 access_token，或持有可复验的 cookie 都算「可登录」
        if self._has_valid_token():
            return True
        return bool(self.qt_cookie)

    # ── 登录 ─────────────────────────────────────────────────
    def _store_result(self, result: Optional[dict]) -> bool:
        """保存登录接口返回的结果。"""
        if not result or not result.get("ok"):
            return False
        data = result.get("data") or {}
        if not isinstance(data, dict) or not data.get("qid"):
            self.logger.warning(
                f"{TAG} 登录结果缺少 qid，字段="
                f"{sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}"
            )
            return False

        self.userInfo = data
        cred = QihooLogin.extract_credentials(data)
        try:
            expires_in = int(cred.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            self.token_expire_time = int(time.time()) + expires_in

        # 显示名优先用昵称，其次用户名
        display = (data.get("nickname") or "").strip() or (data.get("username") or "").strip()
        if display:
            self.name = display

        self.user_info = {"id": cred["qid"], "token": cred["access_token"]}
        self.logger.info(
            f"{TAG} 已保存 360 凭据: qid={cred['qid']} name={self.name} "
            f"expires_in={expires_in}"
        )
        return True

    def request_user_login(self, on_complete=None):
        """请求用户登录。先用本地 cookie 复验；失败则拉起浏览器。"""
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        def _on_done(result):
            if result is None:
                if on_complete is not None:
                    on_complete(None)
                    return
                return False
            try:
                # 浏览器登录后 Q/T cookie 由 QihooLogin 持有，必须在此回写，
                # 否则 token 过期后没有 cookie 可用，只能再次弹浏览器。
                self._sync_cookie_from_login()
                success = self._store_result(result)
            except Exception:
                self.logger.exception(f"{TAG} 异步登录处理失败")
                success = False
            if on_complete is not None:
                on_complete(success)
                return
            return success

        result = self.qihooLogin.web_login(
            self.qt_cookie, on_complete=_on_done if on_complete else None
        )
        if on_complete is not None:
            # 异步模式：web_login 返回 None，结果由回调给出
            return None

        if result and result.get("ok"):
            self._sync_cookie_from_login()
            return self._store_result(result)
        return False

    def _sync_cookie_from_login(self) -> None:
        """把 QihooLogin 里最新的 Q/T cookie 回写到本对象（用于持久化）。"""
        old = self.qt_cookie or ""
        new = getattr(self.qihooLogin, "qt_cookie", "") or ""
        if not new or new == old:
            return
        self.qt_cookie = new
        self.logger.info(
            f"{TAG} 已保存 Q/T cookie（{len(old)} -> {len(new)} 字符），"
            "token 过期后可免浏览器复验"
        )

    # ── UniSDK 数据 ──────────────────────────────────────────
    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        if not game_id:
            raise RuntimeError("360_assistant 缺少 game_id")

        short_game_id = getShortGameId(game_id)

        if not self._has_valid_token():
            self.logger.info(f"{TAG} access_token 不可用，尝试用 cookie 复验登录")
            if on_complete is not None:

                def _on_login_done(success):
                    if success and self._has_valid_token():
                        try:
                            on_complete(self._build_unisdk_result(short_game_id))
                        except Exception as e:
                            self.logger.error(f"{TAG} UniSDK error: {e}")
                            on_complete(None)
                    else:
                        on_complete(None)

                self.request_user_login(on_complete=_on_login_done)
                return None
            self.request_user_login()

        result = self._build_unisdk_result(short_game_id)
        if on_complete is not None:
            on_complete(result)
            return None
        return result

    def _build_unisdk_result(self, short_game_id: str) -> Optional[Dict[str, Any]]:
        cred = self._credentials()
        qid = cred["qid"]
        access_token = cred["access_token"]
        if not qid or not access_token:
            raise RuntimeError(
                f"360 缺少 qid 或 access_token: qid={qid!r}, "
                f"token_present={bool(access_token)}"
            )

        sdk_version = self.sdk_ver
        app_channel = self.protocol_app_channel or QC.APP_CHANNEL_FALLBACK

        uniBody = buildSAUTH(
            login_channel=CHANNEL_NAME,
            app_channel=app_channel,
            uid=qid,
            session=access_token,
            game_id=short_game_id,
            sdk_version=sdk_version,
        )
        uniData = postSignedData(uniBody, short_game_id, True)

        code = uniData.get("code") if isinstance(uniData, dict) else None
        subcode = uniData.get("subcode") if isinstance(uniData, dict) else None
        if code != 200:
            # subcode 13 表示 sdk_version 未命中白名单，应与 cloudRes 的 sdk_ver 核对
            self.logger.error(
                f"{TAG} uni_sauth 未通过: code={code} subcode={subcode} "
                f"status={uniData.get('status') if isinstance(uniData, dict) else None}"
            )

        uniSDKJSON = json.loads(
            base64.b64decode(uniData["unisdk_login_json"]).decode()
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
        str_data = "&".join(f"{k}={v}" for k, v in str_data.items())

        extra_unisdk = json.dumps({
            "SAUTH_STR": base64.b64encode(str_data.encode()).decode(),
            "SAUTH_JSON": base64.b64encode(json.dumps(json_data).encode()).decode(),
            **extra_data,
        })

        return {
            "user_id": qid,
            "token": base64.b64encode(access_token.encode()).decode(),
            "login_channel": CHANNEL_NAME,
            "udid": fd["udid"],
            "app_channel": app_channel,
            "sdk_version": sdk_version,
            "jf_game_id": short_game_id,
            "pay_channel": CHANNEL_NAME,
            "extra_data": "",
            "extra_unisdk_data": extra_unisdk,
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
