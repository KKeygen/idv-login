# coding=UTF-8
"""UC/九游 SDK 登录客户端。

SMS 短信验证码登录：sendSmsCode → loginBySmsCode → SID

会话管理：
- refreshToken 可续期 session（ucid.user.refreshLogin）
- session 有效期 86400 秒（24 小时）
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QButtonGroup, QScrollArea, QWidget,
    QFrame,
)

from channelHandler.ucLogin.consts import (
    UC_APP_ID,
    UC_DEFAULT_HOST,
    UC_GAME_ID,
    UC_H55_VERSION_CODE,
    UC_H55_VERSION_NAME,
    UC_HOST_MAP,
    UC_SDK_VERSION,
    SVC_GET_SECURITY_KEY,
    SVC_SEND_SMS_CODE,
    SVC_SMS_LOGIN,
    SVC_REFRESH_LOGIN,
    SVC_SI_APPLY,
)
from channelHandler.ucLogin.crypto import encrypt_request, decrypt_response, update_rsa_key, get_rsa_version
from logutil import setup_logger
from ssl_utils import should_verify_ssl


# ── SMS 登录对话框 ────────────────────────────────────────────
class _SmsWorkerSignals(QObject):
    """后台线程 → 主线程信号。"""
    sms_sent = pyqtSignal(bool, str)        # (success, message)
    login_result = pyqtSignal(object)       # dict | None
    error = pyqtSignal(str)


class UCSmsLoginDialog(QDialog):
    """UC/九游 SMS 短信验证码登录对话框。

    状态流: 输入手机号 → 输入验证码 → (可选)选择账号 → 登录成功自动关闭。
    所有网络 API 调用在后台线程执行，通过信号更新 UI。
    """

    _last_phone: str = ""  # 类级别记住上次使用的号码

    def __init__(self, parent=None, game_data: dict = None):
        super().__init__(parent)
        self.setWindowTitle("九游账号 - 短信登录")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setFixedSize(380, 260)
        self.setModal(True)

        self._game_data = game_data

        self._session_data: Optional[Dict[str, Any]] = None
        self._signals = _SmsWorkerSignals()
        self._signals.sms_sent.connect(self._on_sms_sent)
        self._signals.login_result.connect(self._on_login_result)
        self._signals.error.connect(self._on_error)
        self._countdown = 0
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._tick_countdown)

        # 保存验证码登录中间态
        self._current_mobile = ""
        self._current_country_code = "86"
        self._current_sms_code = ""
        self._pending_accounts: List[dict] = []
        self._pending_ticket = ""

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(20, 16, 20, 16)
        self._main_layout.setSpacing(10)

        self._build_phone_page()

    # ── 页面构建 ──────────────────────────────────────────────

    def _clear_layout(self):
        # 切换页面前停止倒计时，避免引用已删除的控件
        self._countdown_timer.stop()
        while self._main_layout.count():
            item = self._main_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
            elif item.layout():
                sub = item.layout()
                while sub.count():
                    sub_item = sub.takeAt(0)
                    sw = sub_item.widget()
                    if sw:
                        sw.setParent(None)
                        sw.deleteLater()
            elif item.spacerItem():
                pass  # spacers are removed automatically

    def _new_status_label(self) -> QLabel:
        lbl = QLabel("")
        lbl.setStyleSheet("color: #cc0000; font-size: 12px;")
        lbl.setWordWrap(True)
        self._status_label = lbl
        return lbl

    def _build_phone_page(self):
        """状态 1: 手机号输入。"""
        self._clear_layout()
        self.setFixedSize(380, 220)

        title = QLabel("请输入手机号接收验证码")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        self._main_layout.addWidget(title)

        row = QHBoxLayout()
        self._country_input = QLineEdit("86")
        self._country_input.setFixedWidth(50)
        self._country_input.setPlaceholderText("区号")
        row.addWidget(QLabel("+"))
        row.addWidget(self._country_input)

        self._phone_input = QLineEdit(UCSmsLoginDialog._last_phone)
        self._phone_input.setPlaceholderText("手机号")
        row.addWidget(self._phone_input)
        self._main_layout.addLayout(row)

        self._send_btn = QPushButton("发送验证码")
        self._send_btn.setFixedHeight(36)
        self._send_btn.clicked.connect(self._on_send_sms)
        self._main_layout.addWidget(self._send_btn)

        self._main_layout.addWidget(self._new_status_label())
        self._main_layout.addStretch()

    def _build_code_page(self):
        """状态 2: 验证码输入。"""
        self._clear_layout()
        self.setFixedSize(380, 260)

        masked = self._mask_phone(self._current_mobile)
        hint = QLabel(f"验证码已发送到 {masked}")
        hint.setStyleSheet("font-size: 13px;")
        self._main_layout.addWidget(hint)

        self._code_input = QLineEdit()
        self._code_input.setPlaceholderText("请输入4位验证码")
        self._code_input.setMaxLength(4)
        self._main_layout.addWidget(self._code_input)

        btn_row = QHBoxLayout()
        self._login_btn = QPushButton("登录")
        self._login_btn.setFixedHeight(36)
        self._login_btn.clicked.connect(self._on_login)
        btn_row.addWidget(self._login_btn)

        self._resend_btn = QPushButton()
        self._resend_btn.setFixedHeight(36)
        self._resend_btn.clicked.connect(self._on_send_sms)
        self._resend_btn.setEnabled(False)
        btn_row.addWidget(self._resend_btn)
        self._main_layout.addLayout(btn_row)

        back_btn = QPushButton("← 返回")
        back_btn.setFlat(True)
        back_btn.setStyleSheet("color: #0066cc; text-align: left; font-size: 12px;")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.clicked.connect(self._build_phone_page)
        self._main_layout.addWidget(back_btn)

        self._main_layout.addWidget(self._new_status_label())
        self._main_layout.addStretch()

        self._start_countdown()

    def _build_account_page(self, accounts: list):
        """状态 3: 账号选择。"""
        self._clear_layout()
        height = min(140 + len(accounts) * 52, 500)
        self.setFixedSize(380, height)

        title = QLabel("请选择游戏账号")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        self._main_layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        scroll_layout = QVBoxLayout(container)
        scroll_layout.setSpacing(4)

        self._account_group = QButtonGroup(self)
        for i, acct in enumerate(accounts):
            desc = acct.get("description", "")
            remark = acct.get("accountRemark", "")
            label = desc or remark or f"账号 {i + 1}"
            if remark and desc:
                label = f"{desc}  ({remark})"
            rb = QRadioButton(label)
            rb.setProperty("acct_index", i)
            self._account_group.addButton(rb, i)
            scroll_layout.addWidget(rb)
            if i == 0:
                rb.setChecked(True)

        scroll.setWidget(container)
        self._main_layout.addWidget(scroll)

        confirm_btn = QPushButton("确认")
        confirm_btn.setFixedHeight(36)
        confirm_btn.clicked.connect(self._on_account_selected)
        self._main_layout.addWidget(confirm_btn)

        self._main_layout.addWidget(self._new_status_label())

    # ── 倒计时 ────────────────────────────────────────────────

    def _start_countdown(self):
        self._countdown = 60
        self._resend_btn.setEnabled(False)
        self._resend_btn.setText(f"重新发送({self._countdown}s)")
        self._countdown_timer.start()

    def _tick_countdown(self):
        self._countdown -= 1
        # 若按钮已被页面切换销毁，仅停止计时器
        if not hasattr(self, '_resend_btn') or self._resend_btn is None:
            self._countdown_timer.stop()
            return
        try:
            if self._countdown <= 0:
                self._countdown_timer.stop()
                self._resend_btn.setEnabled(True)
                self._resend_btn.setText("重新发送")
            else:
                self._resend_btn.setText(f"重新发送({self._countdown}s)")
        except RuntimeError:
            self._countdown_timer.stop()

    # ── 事件处理 ──────────────────────────────────────────────

    def _on_send_sms(self):
        phone = self._phone_input.text().strip() if hasattr(self, '_phone_input') else self._current_mobile
        country = self._country_input.text().strip() if hasattr(self, '_country_input') else self._current_country_code
        if not phone:
            self._status_label.setText("请输入手机号")
            return
        self._current_mobile = phone
        self._current_country_code = country or "86"
        UCSmsLoginDialog._last_phone = phone

        self._status_label.setText("正在发送验证码…")
        if hasattr(self, '_send_btn'):
            self._send_btn.setEnabled(False)
        if hasattr(self, '_resend_btn'):
            self._resend_btn.setEnabled(False)

        def _worker():
            try:
                result = send_sms_code(phone, country or "86", game_data=self._game_data)
                state = result.get("state", {})
                if state.get("code") == 1:
                    self._signals.sms_sent.emit(True, "")
                else:
                    msg = state.get("msg", "发送失败")
                    self._signals.sms_sent.emit(False, msg)
            except Exception as e:
                self._signals.sms_sent.emit(False, str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_sms_sent(self, success: bool, msg: str):
        if success:
            self._build_code_page()
        else:
            self._status_label.setText(f"发送失败: {msg}")
            if hasattr(self, '_send_btn'):
                self._send_btn.setEnabled(True)

    def _on_login(self):
        code = self._code_input.text().strip()
        if not code:
            self._status_label.setText("请输入验证码")
            return
        self._current_sms_code = code
        self._status_label.setText("正在登录…")
        self._login_btn.setEnabled(False)

        mobile = self._current_mobile
        country = self._current_country_code

        def _worker():
            try:
                result = login_by_sms_code(mobile, code, country, game_data=self._game_data)
                self._signals.login_result.emit(result)
            except Exception as e:
                self._signals.error.emit(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_login_result(self, result):
        if result is None:
            self._status_label.setText("登录失败，请检查验证码")
            if hasattr(self, '_login_btn'):
                self._login_btn.setEnabled(True)
            return

        if result.get("needSelect"):
            self._pending_accounts = result.get("accounts", [])
            self._pending_ticket = result.get("serviceTicket", "")
            if not self._pending_accounts:
                self._status_label.setText("无可用账号")
                return
            self._build_account_page(self._pending_accounts)
            return

        # 登录成功
        self._session_data = result
        self.accept()

    def _on_account_selected(self):
        idx = self._account_group.checkedId()
        if idx < 0:
            idx = 0
        selected = self._pending_accounts[idx]
        self._status_label.setText("正在登录…")

        mobile = self._current_mobile
        sms_code = self._current_sms_code
        country = self._current_country_code
        ticket = self._pending_ticket
        account_id = selected.get("accountId", "")

        def _worker():
            try:
                result = login_by_sms_code(
                    mobile, sms_code, country,
                    service_ticket=ticket,
                    account_id=account_id,
                    game_data=self._game_data,
                )
                self._signals.login_result.emit(result)
            except Exception as e:
                self._signals.error.emit(str(e))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_error(self, msg: str):
        self._status_label.setText(f"错误: {msg}")
        if hasattr(self, '_login_btn'):
            self._login_btn.setEnabled(True)

    # ── 工具 ──────────────────────────────────────────────────

    @staticmethod
    def _mask_phone(phone: str) -> str:
        if len(phone) >= 7:
            return phone[:3] + "****" + phone[-4:]
        return phone

    def get_session_data(self) -> Optional[Dict[str, Any]]:
        return self._session_data


# ── UC SDK API 调用 ───────────────────────────────────────────


_cached_si = ""
_cached_ci = ""
_cached_csid = None
_cached_werewolf = ""


def _get_device_ids():
    """生成稳定的设备标识（基于机器名）。

    SDK 中各指纹字段的来源 (DeviceHashParam.java):
    - mikasa = MD5(MAC)      → 空时默认 "ffffffffffffffff"
    - sola   = MD5(IMEI)     → 空时默认 "ffffffffffffffff"
    - ackerman = MD5(AndroidID)
    - kaisa  = MD5(CSID)     → CSID 是随机 UUID
    - uya    = MD5(UTDID)    → 阿里 UT 跟踪 ID
    """
    import hashlib
    import platform
    global _cached_csid
    machine_id = hashlib.md5(platform.node().encode()).hexdigest()
    # 模拟 Android 设备标识
    android_id = machine_id[:16]
    utdid = machine_id[:24]
    if _cached_csid is None:
        import uuid
        _cached_csid = str(uuid.uuid4())
    # IMEI 和 MAC 在模拟器上通常为空，SDK 默认用 "ffffffffffffffff"
    return android_id, utdid, _cached_csid


def _build_client_data(game_data: dict = None) -> dict:
    """构建 UC SDK 请求的 client 字段。"""
    import hashlib
    android_id, utdid, csid = _get_device_ids()

    def md5_upper(s: str) -> str:
        """MD5 → 大写十六进制（匹配 SDK EndecodeUtil.byteToHexString）。"""
        if not s:
            s = "ffffffffffffffff"
        return hashlib.md5(s.encode()).hexdigest().upper()

    mikasa = md5_upper("")       # MAC 空 → 默认值
    sola = md5_upper("")         # IMEI 空 → 默认值
    ackerman = md5_upper(android_id)
    kaisa = md5_upper(csid)
    uya = md5_upper(utdid)

    # ex 字段 (ClientExtraInfo.get() + DeviceHashParam)
    ex_parts = [
        f"werewolf:{_cached_werewolf}" if _cached_werewolf else "werewolf:",
        "orient:L",
        f"sola:{sola}",
        f"csid:{csid}",
        "netType:-2",
        "bssid:02:00:00:00:00:00",
        f"utdid:{utdid}",
        "net_id:-1",
        "imsi:",
        f"osId:{android_id}",
        "resY:1080",
        "resX:1920",
        "ssid:<unknown ssid>",
        "mac:",
        f"kaisa:{kaisa}",
        f"ackerman:{ackerman}",
        "imei:",
        f"mikasa:{mikasa}",
        "model:Pixel 6",
        "mobi:",
        "net:wifi",
        f"uya:{uya}",
        "oaId:null",
    ]

    return {
        "ex": "|".join(ex_parts),
        "fr": "API Level-33 - Google Pixel 6-13",
        "os": "android",
        "si": _cached_si,
        "ve": UC_SDK_VERSION,
        "mve": UC_SDK_VERSION,
        "appId": UC_APP_ID,
        "apiLevel": 33,
        "ssid": "<unknown ssid>",
    }


def init_sdk_config(game_data: dict = None) -> bool:
    """初始化 UC SDK：获取 RSA 密钥 → 申请 SI token。

    UC SDK 初始化链 (SystemConfigInitializer.java):
    1. getSecurityKey → 获取服务端 RSA 公钥 + werewolf CRC code
    2. si.apply → 获取风控 SI token

    必须在任何业务 API 调用前完成。
    """
    global _cached_si, _cached_ci, _cached_werewolf
    logger = setup_logger()

    if game_data is None:
        game_data = _build_game_data()

    # Step 1: getSecurityKey — 获取服务端 RSA 密钥 + werewolf
    logger.info("UC SDK 初始化: Step 1 - getSecurityKey ...")
    try:
        sk_result = call_uc_api(
            service=SVC_GET_SECURITY_KEY,
            data={},
            game_data=game_data,
        )
        sk_state = sk_result.get("state", {})
        if sk_state.get("code") == 1:
            sk_data = sk_result.get("data", {})
            if isinstance(sk_data, dict):
                security_key = sk_data.get("securityKey", "")
                if security_key and update_rsa_key(security_key):
                    logger.info(f"UC RSA 密钥已更新: version={get_rsa_version()}")
                else:
                    logger.warning(f"UC RSA 密钥更新失败: {security_key[:40] if security_key else 'empty'}")
                # 提取 werewolf: getSecurityKey 返回 "device" 字段作为设备标识
                # SDK 内部存储为 PersistedObjs.crcCode，用作 DeviceHashParam.werewolf
                werewolf = sk_data.get("device", "") or sk_data.get("crcCode", "")
                if werewolf:
                    _cached_werewolf = werewolf
                    logger.info(f"UC werewolf CRC 获取: {werewolf[:20]}...")
        else:
            logger.warning(f"UC getSecurityKey 失败: code={sk_state.get('code')}, msg={sk_state.get('msg')}")
    except Exception as e:
        logger.warning(f"UC getSecurityKey 异常 (继续): {e}")

    # Step 2: si.apply — 获取风控 SI token
    logger.info("UC SDK 初始化: Step 2 - si.apply ...")
    try:
        si_result = call_uc_api(
            service=SVC_SI_APPLY,
            data={},
            game_data=game_data,
        )
        si_state = si_result.get("state", {})
        if si_state.get("code") == 1:
            si_data = si_result.get("data", {})
            if isinstance(si_data, dict):
                si = si_data.get("si", "")
                if si:
                    _cached_si = si
                    logger.info(f"UC SI token 获取成功: {si[:20]}...")
                    return True
        logger.warning(f"UC si.apply 失败: code={si_state.get('code')}, subCode={si_state.get('subCode')}, msg={si_state.get('msg')}")
        return False
    except Exception as e:
        logger.error(f"UC si.apply 异常: {e}")
        return False


def _build_game_data(uc_game_id: int = None,
                     uc_version_code: int = None,
                     uc_version_name: str = None) -> dict:
    """构建完整的 game 字段，模拟 SDK GameData.toJson()。

    参数从 cloudRes 配置传入，支持多游戏。
    """
    return {
        "cpId": 0,
        "gameId": uc_game_id if uc_game_id is not None else UC_GAME_ID,
        "channelId": "",
        "serverId": 0,
        "serverName": "",
        "roleId": "",
        "roleName": "",
        "roleLevel": "",
        "zoneId": "",
        "zoneName": "",
        "apkChannelId": "",
        "versionCode": uc_version_code if uc_version_code is not None else UC_H55_VERSION_CODE,
        "versionName": uc_version_name if uc_version_name is not None else UC_H55_VERSION_NAME,
        "brandId": "JY",
        "firstInstallTime": int(time.time() * 1000),
        "packType": "net",
        "sign": "",
        "runtimeType": "",
    }


def call_uc_api(service: str, data: dict, game_data: dict = None) -> dict:
    """调用 UC SDK 加密 API。

    请求格式 (SDKHttpRequest.java + NetworkSecurity.java):
    - 请求体为 JSON {"k":"RSA(aes_key)","i":"RSA(aes_iv)","d":"AES(body)","v":ver}
    - Content-Type 为 application/x-www-form-urlencoded（但实际发 raw JSON）
    - URL 为 http://{host}/ng/client/{service}?ver=0&df=adat&os=android
    """
    logger = setup_logger()

    if game_data is None:
        game_data = _build_game_data()

    body = {
        "id": int(time.time() * 1000),
        "service": service,
        "data": data,
        "game": game_data,
        "client": _build_client_data(game_data),
    }

    payload, aes_key, aes_iv = encrypt_request(body)

    host = UC_HOST_MAP.get(service, UC_DEFAULT_HOST)
    url = f"http://{host}/ng/client/{service}?ver=0&df=adat&os=android"

    # SDK 发送 raw JSON bytes，Content-Type 标为 form-urlencoded
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    try:
        resp = requests.post(
            url,
            data=raw_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "okhttp/${project.version}",
                "Host": host,
            },
            timeout=15,
            verify=should_verify_ssl(),
            proxies={"http": None, "https": None},
        )
        resp_data = resp.json()
    except Exception as e:
        logger.error(f"UC API 请求失败 ({service} → {host}): {e}")
        raise

    result = decrypt_response(resp_data, aes_key, aes_iv)
    state = result.get("state", {})
    logger.debug(f"UC API {service} → code={state.get('code')}, msg={state.get('msg')}")
    return result


# ============ SMS 短信验证码登录 ============

def send_sms_code(mobile: str, country_code: str = "86", game_data: dict = None) -> dict:
    """发送 UC 短信验证码。

    调用 unifiedAccount.sendSmsCode → sdk-account.9game.cn。

    Returns:
        API 响应 dict。code=1 表示发送成功。
    """
    logger = setup_logger()
    if not _cached_si:
        init_sdk_config(game_data=game_data)

    result = call_uc_api(
        service=SVC_SEND_SMS_CODE,
        data={"mobile": mobile, "countryCode": country_code},
        game_data=game_data,
    )
    state = result.get("state", {})
    if state.get("code") == 1:
        logger.info(f"UC 短信验证码已发送到 {mobile}")
    else:
        logger.warning(f"UC 短信发送失败: code={state.get('code')}, msg={state.get('msg')}")
    return result


def login_by_sms_code(
    mobile: str,
    sms_code: str,
    country_code: str = "86",
    service_ticket: str = None,
    account_id: str = None,
    game_data: dict = None,
) -> Optional[Dict[str, Any]]:
    """用短信验证码登录。

    流程:
    1. 首次调用 → 可能返回 code=214 "请选择游戏账号" + accountList
    2. 带 serviceTicket + accountId 再次调用 → code=1 登录成功

    Returns:
        成功返回完整登录数据 dict（sid, accountId, refreshToken 等），
        需要选择账号时返回 {"needSelect": True, "accounts": [...], "serviceTicket": "..."}.
        失败返回 None。
    """
    logger = setup_logger()

    data = {
        "mobile": mobile,
        "countryCode": country_code,
        "smsCode": sms_code,
        "loginType": 1,
    }
    if service_ticket:
        data["serviceTicket"] = service_ticket
    if account_id:
        data["accountId"] = account_id

    result = call_uc_api(service=SVC_SMS_LOGIN, data=data, game_data=game_data)
    state = result.get("state", {})
    code = state.get("code", -1)

    if code == 214:
        # 需要选择游戏账号
        resp_data = result.get("data", {})
        accounts = resp_data.get("accountList", [])
        ticket = resp_data.get("serviceTicket", "")
        logger.info(f"UC 需选择账号: {len(accounts)} 个")
        return {
            "needSelect": True,
            "accounts": accounts,
            "serviceTicket": ticket,
            "mobile": resp_data.get("mobile", ""),
        }

    if code == 1:
        resp_data = result.get("data", {})
        logger.info(
            f"UC SMS 登录成功: sid={resp_data.get('sid', '?')[:20]}..., "
            f"accountId={resp_data.get('accountId', '?')}"
        )
        return resp_data

    logger.warning(f"UC SMS 登录失败: code={code}, msg={state.get('msg')}")
    return None


# ============ 会话刷新 ============

def refresh_login(sid: str, refresh_token: str, game_data: dict = None) -> Optional[Dict[str, Any]]:
    """用 refreshToken 续期 UC session。

    调用 ucid.user.refreshLogin → sdk.9game.cn。

    Returns:
        成功返回新的 session 数据（包含新 sid, refreshToken 等）。
        失败返回 None。
    """
    logger = setup_logger()
    if not _cached_si:
        init_sdk_config(game_data=game_data)

    result = call_uc_api(
        service=SVC_REFRESH_LOGIN,
        data={"sid": sid, "refreshToken": refresh_token},
        game_data=game_data,
    )
    state = result.get("state", {})
    code = state.get("code", -1)

    if code == 1:
        resp_data = result.get("data", {})
        logger.info(f"UC session 续期成功: 新sid={resp_data.get('sid', '?')[:20]}...")
        return resp_data
    else:
        logger.warning(f"UC session 续期失败: code={code}, msg={state.get('msg')}")
        return None


class UCLogin:
    """UC/九游登录管理器。"""

    def __init__(self, game_id: str = "h55",
                 uc_game_id: int = None,
                 uc_version_code: int = None,
                 uc_version_name: str = None):
        self.logger = setup_logger()
        self.game_id = game_id
        # 游戏相关参数（从 cloudRes 传入，或使用 consts.py 默认值）
        self.uc_game_id = uc_game_id if uc_game_id is not None else UC_GAME_ID
        self.uc_version_code = uc_version_code if uc_version_code is not None else UC_H55_VERSION_CODE
        self.uc_version_name = uc_version_name if uc_version_name is not None else UC_H55_VERSION_NAME
        self._game_data = _build_game_data(self.uc_game_id, self.uc_version_code, self.uc_version_name)

    def do_refresh(self, sid: str, refresh_token: str) -> Optional[Dict[str, Any]]:
        """用 refreshToken 续期 session。"""
        return refresh_login(sid, refresh_token, game_data=self._game_data)

    def sms_login_dialog(self, parent=None, on_complete=None) -> Optional[Dict[str, Any]]:
        """显示 SMS 登录对话框。

        Args:
            parent: 父窗口。
            on_complete: 异步回调 (session_data | None)。为 None 时同步阻塞。

        Returns:
            同步模式返回 session_data dict 或 None；异步模式始终返回 None。
        """
        import app_state

        def _show_dialog():
            dlg = UCSmsLoginDialog(parent, game_data=self._game_data)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                return dlg.get_session_data() or False
            return None

        if on_complete is not None:
            import threading as _th
            if _th.current_thread() is _th.main_thread():
                result = _show_dialog()
                on_complete(result)
            else:
                app_state.run_on_main_thread(lambda: on_complete(_show_dialog()))
            return None

        # 同步模式 — 必须在主线程
        return _show_dialog()
