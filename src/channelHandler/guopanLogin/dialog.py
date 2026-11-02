# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import math
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton,
)

from native_ui.dialogs import DialogComboBox, StatusLabel, dialog_layout, primary_button


class GuopanLoginDialog(QDialog):
    def __init__(self, authenticate, select_account, on_complete=None, *,
                 send_sms=None, sms_authenticate=None):
        super().__init__()
        self.setWindowTitle("果盘账号登录")
        self._authenticate = authenticate
        self._sms_authenticate = sms_authenticate
        self._send_sms = send_sms
        self._select_account = select_account
        self._callback = on_complete
        self._finished = False
        self._busy = False
        self._session = None
        self._single_game_uin = ""
        self._sms_deadline = 0.0
        self.result_value = None
        layout = dialog_layout(self)
        note = QLabel("密码和短信验证码仅用于本次登录，不会保存。\n第三方发行商账号请使用游戏扫码登录（临时记住）。")
        note.setWordWrap(True)
        note.setProperty("role", "muted")
        layout.addWidget(note)
        self.mode = DialogComboBox()
        self.mode.addItem("密码登录", "password")
        if send_sms is not None and sms_authenticate is not None:
            self.mode.addItem("短信登录", "sms")
        layout.addWidget(self.mode)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.username_label = QLabel("账号")
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.secret_label = QLabel("密码")
        self.password = QLineEdit()
        self.password.setMaxLength(256)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.sms_button = QPushButton("获取验证码")
        self.sms_button.hide()
        secret_row = QHBoxLayout()
        secret_row.setSpacing(8)
        secret_row.addWidget(self.password)
        secret_row.addWidget(self.sms_button)
        form.addRow(self.username_label, self.username)
        form.addRow(self.secret_label, secret_row)
        layout.addLayout(form)
        self.sms_note = QLabel("未注册的手机号可能由果盘按其协议创建账号。")
        self.sms_note.setWordWrap(True)
        self.sms_note.setProperty("role", "muted")
        self.sms_note.hide()
        layout.addWidget(self.sms_note)
        self.consent = QCheckBox("我已阅读并同意果盘用户协议和隐私政策")
        layout.addWidget(self.consent)
        terms = QLabel('<a style="color:#cbad8e" href="https://www.guopan.cn/zcxy.html">果盘用户协议</a> · '
                       '<a style="color:#cbad8e" href="https://user.guopan.cn/conceal.html">果盘隐私政策</a>')
        terms.setOpenExternalLinks(True)
        layout.addWidget(terms)
        self.accounts = DialogComboBox()
        self.accounts.hide()
        layout.addWidget(self.accounts)
        self.status = StatusLabel()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.layout().setSpacing(8)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.submit = primary_button(QPushButton("登录"))
        buttons.addButton(self.submit, QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit.clicked.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sms_timer = QTimer(self)
        self._sms_timer.setInterval(250)
        self._sms_timer.timeout.connect(self._update_sms_button)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.sms_button.clicked.connect(self._request_sms)
        self.finished.connect(self._closed)
        self.password.returnPressed.connect(self._submit)
        self.username.setFocus()

    def _is_sms(self):
        return self.mode.currentData() == "sms"

    def _mode_changed(self, _index):
        self.password.clear()
        sms = self._is_sms()
        self.username_label.setText("手机号" if sms else "账号")
        self.secret_label.setText("验证码" if sms else "密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if sms else QLineEdit.EchoMode.Password)
        self.password.setMaxLength(64 if sms else 256)
        self.sms_button.setVisible(sms)
        self.sms_note.setVisible(sms)
        self.status.clear()
        self._update_sms_button()

    def _set_busy(self, value):
        self._busy = value
        self.submit.setEnabled(not value)
        editing = not value and self._session is None
        self.mode.setEnabled(editing)
        self.username.setEnabled(editing)
        self.password.setEnabled(editing)
        self.consent.setEnabled(editing)
        self.accounts.setEnabled(not value)
        self._update_sms_button()

    def _update_sms_button(self):
        remaining = max(0, math.ceil(self._sms_deadline - time.monotonic()))
        self.sms_button.setText(f"{remaining}秒后重发" if remaining else "获取验证码")
        self.sms_button.setEnabled(not self._finished and not self._busy
                                   and self._session is None and remaining == 0)
        if remaining == 0:
            self._sms_timer.stop()

    @staticmethod
    def _error_text(result, action):
        code = getattr(result, "code", None)
        suffix = f"（错误码 {code}）" if type(code) is int else ""
        if code == 1035 and getattr(result, "kind", None) == "account_cancellation":
            return "账号存在注销申请，请在原应用确认是否撤销注销后再登录。"
        if getattr(result, "kind", None) in ("network", "http", "timeout"):
            return action + "请求未完成" + suffix + "，请稍后重试。"
        return action + "未完成" + suffix + "，请重试。"

    def _request_sms(self):
        if (self._finished or self._busy or self._session is not None
                or not self._is_sms() or self._send_sms is None
                or time.monotonic() < self._sms_deadline):
            return
        phone = self.username.text().strip()
        if not phone or not self.consent.isChecked():
            self.status.setText("请输入手机号，并阅读同意果盘协议后获取验证码。")
            return
        self._set_busy(True)
        self.status.setText("正在请求短信验证码…")
        self._send_sms(phone, self._sms_sent)

    def _sms_sent(self, result):
        if self._finished:
            return
        if isinstance(result, dict) and result.get("sent") is True:
            self._sms_deadline = time.monotonic() + 120
            self.password.clear()
            self._sms_timer.start()
            self.status.setText("验证码发送请求已成功，请查看手机并输入验证码。")
        else:
            self.status.setText(self._error_text(result, "验证码发送"))
        self._set_busy(False)

    def _submit(self):
        if self._finished or self._busy:
            return
        if self._session is not None:
            selected = self.accounts.currentData() or self._single_game_uin
            if not selected:
                self.status.setText("请选择本次登录的游戏账号。")
                return
            self._begin_selection(selected)
            return
        username, password = self.username.text().strip(), self.password.text()
        if not username or not password or not self.consent.isChecked():
            self.status.setText("请输入手机号和验证码，并同意果盘协议。" if self._is_sms()
                                else "请输入账号、密码，并阅读同意果盘协议。")
            return
        self._set_busy(True)
        self.password.clear()
        self.status.setText("正在向果盘验证登录…")
        authenticate = self._sms_authenticate if self._is_sms() else self._authenticate
        authenticate(username, password, self._received)

    def _received(self, result):
        if self._finished:
            return
        self._set_busy(False)
        if isinstance(result, Exception) or not isinstance(result, dict):
            self.status.setText(self._error_text(result, "果盘登录"))
            return
        accounts = result.get("accounts")
        if (not isinstance(accounts, list) or not accounts
                or any(not isinstance(a, dict) or not a.get("game_uin") for a in accounts)):
            self.status.setText("果盘未返回可用的游戏账号，请重试或检查原应用中的账号状态。")
            return
        self._session = result
        self._set_busy(False)
        self.submit.setText("验证游戏账号")
        if len(accounts) == 1:
            self._single_game_uin = accounts[0]["game_uin"]
            self._begin_selection(accounts[0]["game_uin"])
            return
        self.accounts.clear()
        self.accounts.addItem("请选择游戏账号", "")
        for index, account in enumerate(accounts, 1):
            name = account.get("name") or f"游戏账号 {index}"
            self.accounts.addItem(str(name), account["game_uin"])
        self.accounts.show()
        self.submit.setText("使用此账号")
        self.status.setText("此果盘账号包含多个游戏账号，请明确选择。")

    def _begin_selection(self, selected):
        self._set_busy(True)
        self.status.setText("正在验证所选游戏账号…")
        self._select_account(self._session, selected, self._selected)

    def _selected(self, result):
        if self._finished:
            return
        self._set_busy(False)
        if isinstance(result, Exception) or not isinstance(result, dict):
            self.status.setText("所选游戏账号验证未完成，请重试或在原应用中检查。")
            return
        self.result_value = result
        self.accept()

    def _closed(self, _result):
        if self._finished:
            return
        self._finished = True
        self._sms_timer.stop()
        self.password.clear()
        self.username.clear()
        self._session = None
        callback, self._callback = self._callback, None
        if callback is not None:
            callback(self.result_value)
