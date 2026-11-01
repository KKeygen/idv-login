# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import base64

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout


class QrDialog(QDialog):
    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.setWindowTitle('扫码登录')
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setStyleSheet(
            'QDialog { background: #25272c; }'
            'QLabel { color: #f4f5f7; font-size: 14px; }'
            'QPushButton { color: #f4f5f7; background: #41444b; padding: 8px 24px; }')
        self.resize(360, 180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setFixedSize(280, 280)
        self.image.setStyleSheet('background: #ffffff; border-radius: 8px; padding: 8px;')
        self.image.hide()
        layout.addWidget(self.image, 0, Qt.AlignmentFlag.AlignHCenter)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel('正在获取二维码…')
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setMinimumHeight(48)
        layout.addWidget(self.status)
        self.cancel_button = QPushButton('取消登录')
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignHCenter)

    def update_state(self, data):
        status = str(data.get('status') or 'idle')
        hints = {'myapp': '请使用微信扫描二维码登录',
                 'bilibili_sdk': '请使用哔哩哔哩扫描二维码登录',
                 'huawei': '请使用手机浏览器扫描二维码登录'}
        labels = {'idle': '等待开始登录…', 'loading': '正在获取二维码…',
                  'ready': hints.get(self.channel, '请使用对应客户端扫码'),
                  'scanned': '扫码成功，正在校验…', 'verified': '校验成功，正在导入…',
                  'expired': '二维码已过期', 'failed': '扫码失败',
                  'retrying': '连接失败，正在重试…', 'cancelled': '扫码已取消'}
        self.status.setText(labels.get(status, '正在处理…'))
        encoded = data.get('qrcode_base64') or ''
        if encoded and len(encoded) < 8 * 1024 * 1024:
            try:
                pixmap = QPixmap()
                if pixmap.loadFromData(base64.b64decode(encoded, validate=True)):
                    self.image.setPixmap(pixmap.scaled(
                        264, 264, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    self.image.show()
                    self.progress.hide()
                    return
            except (ValueError, TypeError):
                pass
        self.image.clear()
        self.image.hide()
        self.progress.setVisible(status in {'loading', 'scanned', 'verified', 'retrying'})
