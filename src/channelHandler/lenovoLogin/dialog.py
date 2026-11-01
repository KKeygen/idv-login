# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import threading
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QPushButton, QVBoxLayout
from .protocol import LoginError

_workers = set()


class LoginWorker(QObject):
    completed = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, client, account, password):
        super().__init__()
        self.client, self.account, self.password = client, account, password

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            result = self.client.password_login(self.account, self.password)
        except LoginError as error:
            result = error
        except Exception:
            result = LoginError("联想登录失败，请稍后重试")
        finally:
            self.password = ""
            self.account = ""
            self.client.http.close()
        self.completed.emit(result)
        self.finished.emit()


class LenovoLoginDialog(QDialog):
    def __init__(self, client_factory):
        super().__init__()
        self.client_factory = client_factory
        self.login_result = None
        self._busy = False
        self._closed = False
        self.setWindowTitle("联想账号登录")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        note = QLabel("请输入联想手机号或邮箱；密码不保存。\n激活、验证码或实名认证请先在官方游戏客户端完成。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.account = QLineEdit()
        self.account.setPlaceholderText("联想手机号或邮箱")
        self.password = QLineEdit()
        self.password.setPlaceholderText("密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.submit = QPushButton("登录")
        cancel = QPushButton("取消")
        for widget in (self.account, self.password, self.status, self.submit, cancel):
            layout.addWidget(widget)
        self.submit.clicked.connect(self._submit)
        self.password.returnPressed.connect(self._submit)
        cancel.clicked.connect(self.reject)
        self.finished.connect(self._finish)

    def _submit(self):
        if self._busy or self._closed:
            return
        account, password = self.account.text().strip(), self.password.text()
        if not account or not password:
            self.status.setText("请输入账号和密码")
            return
        self.password.clear()
        self._busy = True
        self.submit.setEnabled(False)
        self.status.setText("正在校验联想账号及服务端登录限制…")
        try:
            worker = LoginWorker(self.client_factory(), account, password)
        except LoginError as error:
            self._busy = False
            self.submit.setEnabled(True)
            self.status.setText(str(error))
            return
        _workers.add(worker)
        worker.completed.connect(self._completed)
        worker.finished.connect(lambda: _workers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _completed(self, result):
        if self._closed:
            return
        self._busy = False
        self.submit.setEnabled(True)
        if isinstance(result, LoginError):
            self.status.setText(str(result))
            return
        if not isinstance(result, dict):
            self.status.setText("联想登录失败")
            return
        self.login_result = result
        self.accept()

    def _finish(self, _code):
        self._closed = True
        self.password.clear()
        self.account.clear()


def show_login(client_factory, on_complete=None):
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise LoginError("请从主界面的联想账号入口发起登录")
    dialog = LenovoLoginDialog(client_factory)
    if on_complete is None:
        dialog.exec()
        result = dialog.login_result
        dialog.deleteLater()
        return result
    def finished(_code):
        on_complete(dialog.login_result)
        dialog.deleteLater()
    dialog.finished.connect(finished)
    dialog.open()
    return dialog
