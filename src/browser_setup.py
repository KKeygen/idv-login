from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton
from native_ui.dialogs import dialog_layout, primary_button
from webview2_host import MINIMUM_RUNTIME_VERSION, RUNTIME_DOWNLOAD_URL


class BrowserSetupDialog(QDialog):
    def __init__(self, message, *, on_retry, on_cancel, on_clear_retry=None, parent=None):
        super().__init__(parent)
        self._clear_retry = False
        self.setWindowTitle('WebView2 登录组件准备')
        layout = dialog_layout(self, width=560)
        label = QLabel(str(message))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        note = QLabel('登录页面使用系统 Microsoft Edge WebView2 Runtime，不要求安装 Chrome。\n'
                      '缺失或版本过旧时，工具会自动下载并安装；失败后可以重试或从微软官网安装。\n'
                      f'最低 Runtime 版本：{MINIMUM_RUNTIME_VERSION}。\n工具使用微软共享 Runtime 和本工具的独立登录资料。')
        note.setWordWrap(True)
        layout.addWidget(note)
        links = QHBoxLayout()
        layout.addLayout(links)
        for title, url in [('微软 WebView2 官方获取', RUNTIME_DOWNLOAD_URL)]:
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, link=url: QDesktopServices.openUrl(QUrl(link)))
            links.addWidget(button)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch()
        layout.addLayout(buttons)
        retry = primary_button(QPushButton('重试')); retry.clicked.connect(self.accept)
        cancel = QPushButton('取消'); cancel.clicked.connect(self.reject)
        buttons.addWidget(retry)
        if on_clear_retry is not None:
            clear = QPushButton('清除独立登录状态并重试')
            def clear_retry():
                self._clear_retry = True
                self.accept()
            clear.clicked.connect(clear_retry)
            buttons.addWidget(clear)
        buttons.addWidget(cancel)
        self.finished.connect(lambda value: (on_clear_retry() if self._clear_retry else on_retry())
            if value == QDialog.DialogCode.Accepted else on_cancel())
        self.resize(self.minimumWidth(), self.sizeHint().height())
