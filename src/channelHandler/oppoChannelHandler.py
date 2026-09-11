# coding=UTF-8
"""Oppo(heytap) 网页登录渠道。

当前阶段：WebView 登录 -> 捕获 `loginResp` -> 走 OpenAccount `api/authorize` 并持久化。
使用阶段：每次实际使用前调用 OpenAccount `api/token/refresh`，将返回的 token 合并回本地状态。
"""
from __future__ import annotations

import time
import json
import base64
import re
import uuid
from typing import Any, Dict, Optional

import channelmgr
from envmgr import genv
import app_state
from logutil import setup_logger

from cloudRes import CloudRes
from channelHandler.channelUtils import getShortGameId

from channelHandler.oppoLogin.oppoChannel import OppoLogin
from channelHandler.oppoLogin.consts import DEFAULT_CONSTS, prefer_device_id_from_login_resp


DEFAULT_OPPO_BIZ_APP_KEY = "cd73441423364d90a6ac6fe2bc727542"


def _now_ms() -> int:
    return int(time.time() * 1000)


_UUID_HEX_RE = re.compile(r"[0-9a-f]+", re.IGNORECASE)


def _normalize_uuid32(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    hex_only = "".join(_UUID_HEX_RE.findall(raw))
    if len(hex_only) < 32:
        return ""
    return hex_only[:32]


def _generate_oppo_vaid(uuid_source: str) -> str:
    prefix = time.strftime("%y%m%d%H", time.localtime())
    u32 = _normalize_uuid32(uuid_source)
    if not u32:
        u32 = uuid.uuid4().hex
    return prefix + u32


def _get_or_create_oppo_guid_uuid32() -> str:
    key = "oppo.guid_uuid"
    raw = str(genv.get(key, "") or "").strip()
    u32 = _normalize_uuid32(raw)
    if u32:
        return u32

    u32 = uuid.uuid4().hex
    genv.set(key, u32, True)
    return u32


def _get_or_create_oppo_vaid() -> str:
    """获取/生成 OPPO GameSDK 的 vaid，并写入 genv 的持久缓存。

    规则：yyMMddHH + uuid32。
    持久化键：oppo.vaid
    uuid32 来源：oppo.guid_uuid
    """

    key = "oppo.vaid"
    current = str(genv.get(key, "") or "").strip().lower()
    if re.fullmatch(r"\d{8}[0-9a-f]{32}", current or ""):
        return current

    u32 = _get_or_create_oppo_guid_uuid32()
    new_vaid = _generate_oppo_vaid(u32)
    genv.set(key, new_vaid, True)
    return new_vaid


def _merge_dict_inplace(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in (src or {}).items():
        if v is not None:
            dst[k] = v
    return dst


class oppoChannel(channelmgr.channel):
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
        oppo_open_account: Optional[Dict[str, Any]] = None,
        chosen_account_id: str = "",
        start_url: str = "",
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
        self.crossGames = False if game_id else True

        self.game_id = game_id
        self.chosen_account_id = chosen_account_id
        if self.chosen_account_id=='':
            self.crossGames=True
        self.loginResp: Optional[Dict[str, Any]] = loginResp
        # OpenAccount 相关状态（authorize/refresh 的落盘区）
        self.oppo_open_account: Dict[str, Any] = oppo_open_account if isinstance(oppo_open_account, dict) else {}
        self.start_url = start_url
        self.oppoLogin = OppoLogin(start_url=start_url) if start_url else OppoLogin()

        self.uniBody = None
        self.uniData = None
        self.uniSDKJSON = None

    def _extract_login_tokens(self) -> Dict[str, Any]:
        """从 loginResp 里提取 refresh 所需字段。

        这里不做“吞异常”的宽容处理：缺字段/类型不符直接抛出，便于排查数据结构变化。
        """

        if not isinstance(self.loginResp, dict):
            self.logger.error(f"loginResp 数据结构异常，预期 dict 实际 {type(self.loginResp)}，内容: {self.loginResp}")
            raise TypeError("oppo loginResp 必须为 dict. 请检查是否处于境外环境导致登录失败！")

        lr = self.loginResp
        account_token = lr.get("accountToken")
        if not isinstance(account_token, dict):
            raise TypeError("oppo loginResp.accountToken 必须为 dict")

        secondary_token_map = lr.get("secondaryTokenMap")
        if secondary_token_map is None:
            secondary_token_map = {}
        if not isinstance(secondary_token_map, dict):
            raise TypeError("oppo loginResp.secondaryTokenMap 必须为 dict")

        return {
            "id_token": str(account_token.get("idToken") or "").strip(),
            "access_token": str(account_token.get("accessToken") or "").strip(),
            "refresh_token": str(account_token.get("refreshToken") or "").strip(),
            "ssoid": str(lr.get("ssoid") or "").strip(),
            "primary_token": str(lr.get("primaryToken") or "").strip(),
            "refresh_ticket": str(lr.get("refreshTicket") or "").strip(),
            "secondary_token_map": secondary_token_map,
            "device_id": str(lr.get("deviceId") or "").strip(),
        }

    def _ensure_authorized(self) -> bool:
        """确保已做过一次 authorize，并将返回结果持久化到 `oppo_open_account`。"""

        tokens = self._extract_login_tokens()
        if not tokens["id_token"]:
            raise RuntimeError("Oppo loginResp.accountToken.idToken 为空")

        auth = self.oppo_open_account.get("authorize")
        already_ok = bool(isinstance(auth, dict) and auth.get("success"))
        if already_ok:
            return True

        from channelHandler.oppoOpenAccount.client import OppoOpenAccountClient, DEFAULT_BASE_URL

        consts = prefer_device_id_from_login_resp(self.loginResp, DEFAULT_CONSTS)
        session_ticket = str(self.oppo_open_account.get("session_ticket") or "").strip()
        client = OppoOpenAccountClient(consts=consts, base_url=DEFAULT_BASE_URL, session_ticket=session_ticket)

        resp = client.authorize(account_id_token=tokens["id_token"], biz_app_key=DEFAULT_OPPO_BIZ_APP_KEY)

        # 持久化原始响应 + 抽取关键字段
        self.oppo_open_account["authorize"] = {
            "ts": _now_ms(),
            "resp": resp,
            "success": bool(isinstance(resp, dict) and resp.get("success")),
        }
        self.oppo_open_account["session_ticket"] = client.session.session_ticket

        if isinstance(resp, dict):
            data = resp.get("data") or {}
            if not isinstance(data, dict):
                raise TypeError("OpenAccount authorize resp.data 必须为 dict")
            biz_token = data.get("bizToken")
            if biz_token is not None:
                self.oppo_open_account["bizToken"] = biz_token
            device_id = str(data.get("deviceId") or "").strip()
            if device_id:
                self.oppo_open_account["deviceId"] = device_id
                if isinstance(self.loginResp, dict) and not str(self.loginResp.get("deviceId") or "").strip():
                    self.loginResp["deviceId"] = device_id

        return bool(self.oppo_open_account["authorize"]["success"])

    def refresh_before_use(self) -> Dict[str, Any]:
        """每次“实际使用”前刷新一次 open-account token。

        返回 refresh 的原始响应（并把关键信息合并回 loginResp/oppo_open_account）。
        """

        # refresh 依赖 authorize 期望的 sessionTicket；若没做过，尽量先补一次
        self._ensure_authorized()

        tokens = self._extract_login_tokens()
        missing = [
            k
            for k in ("refresh_token", "ssoid", "primary_token", "refresh_ticket", "access_token")
            if not tokens.get(k)
        ]
        if missing:
            raise RuntimeError(f"oppo refresh 缺少字段: {','.join(missing)}")

        from channelHandler.oppoOpenAccount.client import OppoOpenAccountClient, DEFAULT_BASE_URL

        consts = prefer_device_id_from_login_resp(self.loginResp, DEFAULT_CONSTS)
        session_ticket = str(self.oppo_open_account.get("session_ticket") or "").strip()
        client = OppoOpenAccountClient(consts=consts, base_url=DEFAULT_BASE_URL, session_ticket=session_ticket)

        resp = client.token_refresh(
            refresh_token=tokens["refresh_token"],
            ssoid=tokens["ssoid"],
            primary_token=tokens["primary_token"],
            refresh_ticket=tokens["refresh_ticket"],
            access_token=tokens["access_token"],
            secondary_token_map=tokens["secondary_token_map"] or None,
        )

        self.oppo_open_account["refresh"] = {
            "ts": _now_ms(),
            "resp": resp,
            "success": bool(isinstance(resp, dict) and resp.get("success")),
        }
        self.oppo_open_account["session_ticket"] = client.session.session_ticket

        if not isinstance(resp, dict):
            raise TypeError("OpenAccount refresh 返回必须为 dict")
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            raise TypeError("OpenAccount refresh resp.data 必须为 dict")

        # 1) secondaryTokenMap：最适合合并回 loginResp.secondaryTokenMap（同一语义、且 refresh 请求下一次也需要它）
        sec_map = data.get("secondaryTokenMap")
        if isinstance(sec_map, dict) and sec_map:
            self.oppo_open_account["secondaryTokenMap"] = sec_map
            if isinstance(self.loginResp, dict):
                cur = self.loginResp.get("secondaryTokenMap")
                if not isinstance(cur, dict):
                    cur = {}
                    self.loginResp["secondaryTokenMap"] = cur
                _merge_dict_inplace(cur, sec_map)

        # 2) v3TokenResp：更像 authorize 返回的 bizToken（OpenAccount 的 token），因此更新到 oppo_open_account 下
        v3 = data.get("v3TokenResp")
        if isinstance(v3, dict) and v3:
            self.oppo_open_account["v3TokenResp"] = v3
            biz = self.oppo_open_account.get("bizToken")
            if not isinstance(biz, dict):
                biz = {}
                self.oppo_open_account["bizToken"] = biz
            _merge_dict_inplace(biz, v3)

        device_id = str(data.get("deviceId") or "").strip()
        if device_id:
            self.oppo_open_account["deviceId"] = device_id
            if isinstance(self.loginResp, dict) and not str(self.loginResp.get("deviceId") or "").strip():
                self.loginResp["deviceId"] = device_id

        return resp

    def request_user_login(self, on_complete=None):
        genv.set("GLOB_LOGIN_UUID", self.uuid)

        # deviceId 初始留空；若旧 loginResp.deviceId 非空，则本次注入优先使用旧值。
        consts = prefer_device_id_from_login_resp(self.loginResp, DEFAULT_CONSTS)

        def _process_resp(resp):
            if not resp:
                self.loginResp = None
                return False
            old_device_id = ""
            if isinstance(self.loginResp, dict):
                old_device_id = str(self.loginResp.get("deviceId") or "").strip()
            new_device_id = str(resp.get("deviceId") or "").strip()
            if not new_device_id and old_device_id:
                resp["deviceId"] = old_device_id
            self.loginResp = resp
            self._ensure_authorized()
            return True

        if on_complete is not None:
            def _on_done(resp):
                try:
                    success = _process_resp(resp)
                except Exception:
                    self.logger.exception("OPPO异步登录处理失败")
                    success = False
                on_complete(success)
            self.oppoLogin.webLogin(consts=consts, on_complete=_on_done)
            return

        resp = self.oppoLogin.webLogin(consts=consts)
        return _process_resp(resp)

    def _pick_gamesdk_pkg_name(self, short_game_id: str) -> str:
        cloud = CloudRes()
        item = cloud.get_channelData("oppo", short_game_id)
        if item is None:
            raw_pkg_name=cloud.get_netease_style_pkgname_by_game_id(short_game_id)
            if raw_pkg_name:
                self.logger.warning(f"云配置缺失 OPPO 渠道的 pkgName，使用其他渠道构建出{raw_pkg_name}.nearme.gamecenter")
                return f"{raw_pkg_name}.nearme.gamecenter"
            self.logger.error(f"云配置缺失 OPPO 渠道的 pkgName，且未找到其他渠道的网易风格 pkgName，无法构建 gamesdk 登录所需的 pkgName")
            return ""
        else:
            if not isinstance(item, dict):
                return ""
            pkg = item.get("oppo") or item.get("package_name")
            pkg = str(pkg or "").strip()
            return pkg

    def _pick_secondary_token(self) -> str:
        if not isinstance(self.loginResp, dict):
            raise RuntimeError("loginResp 为空，无法获取 secondaryToken")
        lr = self.loginResp
        consts = prefer_device_id_from_login_resp(self.loginResp, DEFAULT_CONSTS)
        host_pkg = getattr(consts, "PKG_HOST", "") or ""

        sec_map = lr.get("secondaryTokenMap") or {}
        if not isinstance(sec_map, dict):
            raise TypeError("loginResp.secondaryTokenMap 必须为 dict")
        if host_pkg and isinstance(sec_map, dict):
            v = str(sec_map.get(host_pkg) or "").strip()
            if v:
                return v

        sec_map2 = self.oppo_open_account.get("secondaryTokenMap") or {}
        if not isinstance(sec_map2, dict):
            raise TypeError("oppo_open_account.secondaryTokenMap 必须为 dict")
        if host_pkg and isinstance(sec_map2, dict):
            v = str(sec_map2.get(host_pkg) or "").strip()
            if v:
                return v

        # 若只有一个值，直接用
        for m in (sec_map, sec_map2):
            if isinstance(m, dict) and len(m) == 1:
                only_val = str(next(iter(m.values())) or "").strip()
                if only_val:
                    return only_val

        return ""

    def _build_extra_unisdk_data(self, short_game_id: str = "") -> str:
        fd = app_state.fake_device
        udid = fd["udid"]
        res = {
            "SAUTH_STR": "",
            "SAUTH_JSON": "",
        }

        json_data = {
            "extra_data": "",
            "get_access_token": "1",
            "sdk_udid": udid,
            "realname": json.dumps({"realname_type": 0, "age": 22}),
        }
        if isinstance(self.uniBody, dict):
            json_data.update(self.uniBody)

        str_data = json_data.copy()
        if isinstance(self.uniSDKJSON, dict) and "username" in self.uniSDKJSON:
            str_data.update({"username": self.uniSDKJSON["username"]})

        # 阴阳师(g37)：SAUTH_STR 中的 sessionid 即本次登录 token（gamesdk ticket），
        # 可能是 base64（含 + / =）。游戏端按 application/x-www-form-urlencoded 解析，
        # 裸 + 会被当成空格导致 session 损坏，故仅对该 token 字段做 URL 编码。
        # SAUTH_JSON 保持明文（与华为渠道一致），只影响 SAUTH_STR。
        if short_game_id == "g37" and "sessionid" in str_data:
            from urllib.parse import quote
            str_data["sessionid"] = quote(str(str_data["sessionid"]), safe="")

        str_data = "&".join([f"{k}={v}" for k, v in str_data.items()])

        res["SAUTH_STR"] = base64.b64encode(str_data.encode()).decode()
        res["SAUTH_JSON"] = base64.b64encode(json.dumps(json_data).encode()).decode()
        return json.dumps(res)

    def get_uniSdk_data(self, game_id: str = "", on_complete=None):
        """按“其他渠道范式”返回用于 confirm_login 的 unisdk 数据。

        关键：
        - user_id 使用所选角色的 account_id
        - sessionid 使用 gamesdk user/login 返回的 ticket
        """

        genv.set("GLOB_LOGIN_UUID", self.uuid)
        if not game_id:
            game_id = self.game_id
        if not game_id:
            raise RuntimeError("oppo 缺少 game_id")

        short_game_id = getShortGameId(game_id)

        if not self.is_token_valid():
            if on_complete is not None:
                def _on_login_done(success):
                    if success and self.is_token_valid():
                        try:
                            result = self._build_oppo_unisdk_result(short_game_id)
                            on_complete(result)
                        except Exception as e:
                            self.logger.error(f"OPPO UniSDK error: {e}")
                            on_complete(None)
                    else:
                        on_complete(None)
                self.request_user_login(on_complete=_on_login_done)
                return None
            else:
                self.request_user_login()

        # 使用前 refresh 一次，尽量拿到最新 secondaryTokenMap（失败直接抛出）
        self.refresh_before_use()

        secondary_token = self._pick_secondary_token()
        if not secondary_token:
            raise RuntimeError("未找到 secondaryToken（loginResp.secondaryTokenMap 为空）")

        pkg_name = self._pick_gamesdk_pkg_name(short_game_id)
        if not pkg_name:
            raise RuntimeError(f"未从云配置获取 gamesdk pkgName: game_id={short_game_id}")

        # 构建 sign2 profile
        fd = app_state.fake_device
        if not isinstance(fd, dict):
            raise TypeError("FAKE_DEVICE 必须为 dict")
        oppo_vaid = _get_or_create_oppo_vaid()

        consts = prefer_device_id_from_login_resp(self.loginResp, DEFAULT_CONSTS)
        screen_w = int(getattr(getattr(consts, "DEVICE", None), "screen_wd", 1600) or 1600)
        screen_h = int(getattr(getattr(consts, "DEVICE", None), "screen_ht", 900) or 900)

        from channelHandler.oppoGameSdk.client import OppoGameSdkClient, Sign2Profile

        profile = Sign2Profile(
            brand="Xiaomi",
            model=str(fd.get("device_model") or "M2102K1AC"),
            api=32,
            os_ver=str(fd.get("os_ver") or "12"),
            rom="unknown",
            sdkversion=6070105,
            ch="2401",
            pid="1001",
            locale="-;cn",
            country="CN",
            net="wifi",
            sdktype="0",
            appversion="2.0.15",
            appid="OPPO#1001#CN",
            udid=str(fd.get("udid") or ""),
            oaid=str(fd.get("oaid") or ""),
            mkmix_id=str(fd.get("MkMixId") or ""),
            vaid=oppo_vaid,
            screen_w=screen_w,
            screen_h=screen_h,
        )

        client = OppoGameSdkClient(profile)

        login_res = client.user_login(pkg_name=pkg_name, secondary_token=secondary_token)
        self.oppo_open_account["gamesdk_user_login"] = {
            "ts": _now_ms(),
            "code": login_res.code,
            "msg": login_res.msg,
            "trace_id": getattr(login_res, "trace_id", ""),
        }
        if login_res.code and login_res.code != "200":
            raise RuntimeError(f"gamesdk user/login failed: {login_res.code} {login_res.msg}")

        ticket = str(getattr(login_res, "ticket", "") or "").strip()
        if not ticket:
            raise RuntimeError("gamesdk user/login 未返回 ticket")
        self.oppo_open_account["gamesdk_ticket"] = {
            "ts": _now_ms(),
            "ticket": ticket,
        }

        user_dto = getattr(login_res, "user_dto", None)
        if user_dto is not None and not isinstance(user_dto, dict):
            raise TypeError("gamesdk user/login user_dto 必须为 dict")
        self.oppo_open_account["gamesdk_user_dto"] = {
            "ts": _now_ms(),
            "user_dto": user_dto or {},
        }

        latest_res, accounts = client.account_latest_role(pkg_name=pkg_name, secondary_token=secondary_token)
        self.oppo_open_account["gamesdk_latest_role"] = {
            "ts": _now_ms(),
            "code": latest_res.code,
            "msg": latest_res.msg,
            "accounts": accounts,
        }
        if latest_res.code and latest_res.code != "200":
            raise RuntimeError(f"gamesdk account-latest-role failed: {latest_res.code} {latest_res.msg}")
        if not accounts:
            raise RuntimeError("gamesdk 未返回任何角色/账号")

        # 选角：若指定 chosen_account_id 则优先；否则按 login_time 最大；再兜底取第一个
        chosen = None
        if self.chosen_account_id:
            for a in accounts:
                if str(a.get("account_id") or "").strip() == self.chosen_account_id:
                    chosen = a
                    break

        if chosen is None and len(accounts) > 1:
            # 多账号且未指定默认：弹 Qt 菜单让用户选 accountName；是否记住由勾选框决定
            def _label_for(a: Dict[str, Any], default_idx: int) -> str:
                name = str(a.get("account_name") or "").strip()
                if name.startswith("GU"):
                    return f"默认账号{default_idx}"
                return name or str(a.get("account_id") or "").strip()

            labels: list[str] = []
            label_to_account: Dict[str, Dict[str, Any]] = {}
            gu_i = 0
            for a in accounts:
                name = str(a.get("account_name") or "").strip()
                if name.startswith("GU"):
                    gu_i += 1
                    label = _label_for(a, gu_i)
                else:
                    label = _label_for(a, 0)

                # 防止重名覆盖
                if label in label_to_account:
                    label = f"{label} ({str(a.get('account_id') or '').strip()})"

                labels.append(label)
                label_to_account[label] = a

            from PyQt6.QtWidgets import (
                QApplication,
                QCheckBox,
                QDialog,
                QDialogButtonBox,
                QLabel,
                QListWidget,
                QVBoxLayout,
                QWidget,
            )

            app_inst = QApplication.instance()
            if app_inst is None:
                import sys

                app_inst = QApplication(sys.argv)

            parent = QWidget()
            dialog = QDialog(parent)
            dialog.setWindowTitle("选择账号")
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("请选择要使用的账号："))

            lst = QListWidget(dialog)
            for label in labels:
                lst.addItem(label)
            lst.setCurrentRow(0)
            layout.addWidget(lst)

            remember_cb = QCheckBox("下次自动登录此角色", dialog)
            remember_cb.setChecked(False)
            layout.addWidget(remember_cb)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                raise RuntimeError("用户取消选择账号")

            row = lst.currentRow()
            if row < 0:
                raise RuntimeError("未选择任何账号")

            picked_label = labels[row]
            chosen = label_to_account[picked_label]

            if remember_cb.isChecked():
                chosen_id = str(chosen.get("account_id") or "").strip()
                if chosen_id:
                    self.chosen_account_id = chosen_id

        if chosen is None:
            # 单账号（或有默认但没匹配到）时：按 login_time 最大；再兜底取第一个
            chosen = sorted(accounts, key=lambda x: int(x.get("login_time") or 0), reverse=True)[0]

        account_id = str(chosen.get("account_id") or "").strip()
        if not account_id:
            raise RuntimeError("gamesdk 返回的 account_id 为空")

        self.oppo_open_account["chosen_account"] = {
            "account_id": account_id,
            "role_id": str(chosen.get("role_id") or "").strip(),
            "role_name": str(chosen.get("role_name") or "").strip(),
            "realm_id": str(chosen.get("realm_id") or "").strip(),
            "realm_name": str(chosen.get("realm_name") or "").strip(),
        }

        import channelHandler.channelUtils as channelUtils

        age = 0
        if isinstance(user_dto, dict):
            try:
                age = int(user_dto.get("age") or 0)
            except Exception:
                age = 0

        extra_data = json.dumps({"adv_channel": "0", "adid": "0"}, ensure_ascii=False)
        realname = json.dumps({"realname_type": 0, "age": age}, ensure_ascii=False)

        self.uniBody = channelUtils.buildSAUTH(
            self.channel_name,
            self.channel_name,
            account_id,
            ticket,
            short_game_id,
            str(profile.sdkversion),
            {
                "get_access_token": "1",
                "extra_data": extra_data,
                "realname": realname,
            },
        )

        self.uniData = channelUtils.postSignedData(self.uniBody, short_game_id, True)
        self.uniSDKJSON = json.loads(base64.b64decode(self.uniData["unisdk_login_json"]).decode())

        fd2 = app_state.fake_device
        udid2 = fd2["udid"]

        result = {
            "user_id": account_id,
            "token": base64.b64encode(ticket.encode()).decode(),
            "login_channel": self.channel_name,
            "udid": udid2,
            "app_channel": self.channel_name,
            "sdk_version": str(profile.sdkversion),
            "jf_game_id": short_game_id,
            "pay_channel": self.channel_name,
            "extra_data": "",
            "extra_unisdk_data": self._build_extra_unisdk_data(short_game_id),
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }
        
        if on_complete is not None:
            on_complete(result)
            return None
        return result

    def _build_oppo_unisdk_result(self, short_game_id: str):
        """构建 OPPO UniSDK 数据（用于异步回调时调用）"""
        return self.get_uniSdk_data(game_id=short_game_id)

    def is_token_valid(self):
        return isinstance(self.loginResp, dict) and bool(self.loginResp)

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
            loginResp=data.get("loginResp", None),
            oppo_open_account=data.get("oppo_open_account", None),
            chosen_account_id=data.get("chosen_account_id", ""),
            start_url=data.get("start_url", ""),
            uuid=data.get("uuid", ""),
        )

    def before_save(self):
        # 持久化网页 loginResp + open-account 状态
        if self.loginResp is not None:
            json.dumps(self.loginResp)

        json.dumps(self.oppo_open_account)
