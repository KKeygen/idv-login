# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout,
)


class GuopanLoginDialog(QDialog):
    def __init__(self, authenticate, select_account, on_complete=None):
        super().__init__()
        self.setWindowTitle("果盘账号登录")
        self.setMinimumWidth(400)
        self._authenticate = authenticate
        self._select_account = select_account
        self._callback = on_complete
        self._finished = False
        self._busy = False
        self._session = None
        self._single_game_uin = ""
        self.result_value = None
        layout = QVBoxLayout(self)
        note = QLabel("使用果盘账号及密码。密码仅用于本次登录，不会保存。\n第三方发行商账号请使用其原有登录入口。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.password = QLineEdit()
        self.password.setMaxLength(256)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("账号", self.username)
        form.addRow("密码", self.password)
        layout.addLayout(form)
        self.consent = QCheckBox("我已阅读并同意果盘用户协议和隐私政策")
        layout.addWidget(self.consent)
        terms = QLabel('<a href="https://www.guopan.cn/zcxy.html">果盘用户协议</a> · '
                       '<a href="https://user.guopan.cn/conceal.html">果盘隐私政策</a>')
        terms.setOpenExternalLinks(True)
        layout.addWidget(terms)
        self.accounts = QComboBox()
        self.accounts.hide()
        layout.addWidget(self.accounts)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.submit = QPushButton("登录")
        buttons.addButton(self.submit, QDialogButtonBox.ButtonRole.AcceptRole)
        self.submit.clicked.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._closed)
        self.password.returnPressed.connect(self._submit)

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
            self.status.setText("请输入账号、密码，并阅读同意果盘协议。")
            return
        self._busy = True
        self.submit.setEnabled(False)
        self.password.clear()
        self.status.setText("正在向果盘验证登录…")
        self._authenticate(username, password, self._received)

    def _received(self, result):
        if self._finished:
            return
        self._busy = False
        self.submit.setEnabled(True)
        if isinstance(result, Exception) or not isinstance(result, dict):
            code = getattr(result, "code", None)
            suffix = f"（错误码 {code}）" if type(code) is int else ""
            self.status.setText("果盘登录未完成" + suffix + "。如需短信、实名或其他验证，请先在原应用处理。")
            return
        accounts = result.get("accounts")
        if (not isinstance(accounts, list) or not accounts
                or any(not isinstance(a, dict) or not a.get("game_uin") for a in accounts)):
            self.status.setText("当前账号状态暂不支持，请在原应用完成所需验证后重试。")
            return
        self._session = result
        self.username.setEnabled(False)
        self.password.setEnabled(False)
        self.consent.setEnabled(False)
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
        self._busy = True
        self.submit.setEnabled(False)
        self.status.setText("正在验证所选游戏账号…")
        self._select_account(self._session, selected, self._selected)

    def _selected(self, result):
        if self._finished:
            return
        self._busy = False
        self.submit.setEnabled(True)
        if isinstance(result, Exception) or not isinstance(result, dict):
            self.status.setText("所选游戏账号验证未完成，请重试或在原应用中检查。")
            return
        self.result_value = result
        self.accept()

    def _closed(self, _result):
        if self._finished:
            return
        self._finished = True
        self.password.clear()
        self._session = None
        callback, self._callback = self._callback, None
        if callback is not None:
            callback(self.result_value)
