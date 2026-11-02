# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QInputDialog, QLabel, QListView, QMessageBox, QVBoxLayout,
)


DIALOG_MARGIN = 24
DIALOG_SPACING = 16

DIALOG_STYLE = '''
QDialog { background: #2c2c2c; }
QWidget { color: #f0f0f0; font-size: 14px; }
QLabel { background: transparent; }
QLabel[role="muted"] { color: #b0b0b0; }
QLabel[role="status"] { color: #ff99a4; }
QLabel[role="heading"] { font-size: 18px; font-weight: 600; }
QPushButton, QToolButton {
    min-height: 34px; padding: 0 12px; border: 1px solid #3e3e3e;
    border-radius: 8px; background: #393939; color: #e0e0e0;
}
QPushButton { min-width: 62px; }
QPushButton:hover, QToolButton:hover { background: #424242; }
QPushButton:pressed, QToolButton:pressed { background: #343434; }
QPushButton:focus, QToolButton:focus { border-color: #f0f0f0; }
QPushButton[role="primary"] { background: #cbad8e; color: #201a15; font-weight: 600; }
QPushButton[role="primary"]:hover { background: #d2b89e; }
QPushButton[role="primary"]:pressed { background: #bfa182; }
QPushButton:disabled, QToolButton:disabled { background: #353535; color: #7d7d7d; }
QLineEdit, QComboBox {
    min-height: 34px; padding: 0 11px; border: 1px solid #3e3e3e;
    border-radius: 8px; background: #353535; selection-background-color: #cbad8e;
    selection-color: #201a15;
}
QLineEdit:hover, QComboBox:hover { background: #393939; }
QLineEdit:focus, QComboBox:focus { border-color: #cbad8e; }
QLineEdit:disabled, QComboBox:disabled { color: #7d7d7d; background: #303030; }
QComboBox { padding-right: 32px; }
QComboBox[paintedArrow="true"]::drop-down { width: 30px; border: 0; }
QComboBox[paintedArrow="true"]::down-arrow { image: none; }
QComboBox QAbstractItemView, QListWidget {
    background: #303030; border: 1px solid #454545; border-radius: 8px;
    selection-background-color: #51483e; selection-color: #ffffff; outline: 0;
}
QComboBox QAbstractItemView::item, QListWidget::item { min-height: 28px; padding: 4px 8px; }
QCheckBox, QRadioButton { spacing: 8px; min-height: 24px; }
QCheckBox:disabled, QRadioButton:disabled { color: #7d7d7d; }
QScrollArea, QScrollArea > QWidget > QWidget { border: 0; background: transparent; }
QScrollBar:vertical { background: #303030; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: #303030; height: 10px; margin: 2px; }
QScrollBar::handle { background: #646464; border-radius: 3px; min-width: 24px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { border: 0; border-radius: 3px; background: #414141; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: #cbad8e; border-radius: 3px; }
QToolTip { background: #303030; color: #f0f0f0; border: 1px solid #555555; padding: 6px; }
'''


def _configure_surface(dialog):
    font = QFont(dialog.font())
    font.setFamilies(['Segoe UI Variable', 'Segoe UI', 'PingFang SC',
                      'Microsoft YaHei', 'Noto Sans CJK SC', 'sans-serif'])
    font.setPixelSize(14)
    dialog.setFont(font)
    palette = dialog.palette()
    for role, value in ((QPalette.ColorRole.Window, '#2c2c2c'),
                        (QPalette.ColorRole.WindowText, '#f0f0f0'),
                        (QPalette.ColorRole.Base, '#353535'),
                        (QPalette.ColorRole.Text, '#f0f0f0'),
                        (QPalette.ColorRole.Button, '#393939'),
                        (QPalette.ColorRole.ButtonText, '#f0f0f0'),
                        (QPalette.ColorRole.Highlight, '#cbad8e'),
                        (QPalette.ColorRole.HighlightedText, '#201a15'),
                        (QPalette.ColorRole.Link, '#cbad8e'),
                        (QPalette.ColorRole.PlaceholderText, '#a0a0a0')):
        palette.setColor(role, QColor(value))
    dialog.setPalette(palette)
    dialog.setStyleSheet(DIALOG_STYLE)


def configure_dialog(dialog, width=460):
    _configure_surface(dialog)
    available = dialog.screen().availableGeometry().width()
    dialog.setMinimumWidth(min(width, max(280, available - 40)))
    layout = dialog.layout()
    if layout is not None:
        layout.setContentsMargins(DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN)
        layout.setSpacing(DIALOG_SPACING)
    for buttons in dialog.findChildren(QDialogButtonBox):
        buttons.layout().setSpacing(8)


def dialog_layout(dialog, width=460):
    configure_dialog(dialog, width)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN, DIALOG_MARGIN)
    layout.setSpacing(DIALOG_SPACING)
    return layout


def configure_login_window(window, frameless=False):
    _configure_surface(window)
    window.setObjectName('nativeLoginWindow')
    window.setStyleSheet(DIALOG_STYLE + 'QWidget#nativeLoginWindow { background: #2c2c2c; }')
    margin = 0 if frameless else 16
    window.layout.setContentsMargins(margin, margin, margin, margin)
    window.layout.setSpacing(12)
    window.toolBarLayout.setSpacing(8)
    if window.clear_cookie_button is not None:
        window.clear_cookie_button.setMaximumWidth(104)


def fit_login_window(window, width, height):
    available = window.screen().availableGeometry()
    window.resize(min(width, max(280, available.width() - 40)),
                  min(height, max(280, available.height() - 40)))


def configure_close_button(button):
    button.setFixedSize(32, 32)
    button.setStyleSheet(
        'QPushButton { min-width: 0; min-height: 0; padding: 0; color: #f0f0f0; '
        'background: #393939; border: 1px solid transparent; border-radius: 8px; font-size: 14px; }'
        'QPushButton:hover { background: #c42b1c; }'
        'QPushButton:pressed { background: #a82318; }'
        'QPushButton:focus { border-color: #f0f0f0; }')


def configure_account_list(widget, count):
    widget.setWordWrap(True)
    widget.setTextElideMode(Qt.TextElideMode.ElideNone)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setResizeMode(QListView.ResizeMode.Adjust)
    widget.setFixedHeight(min(280, max(108, count * 52)))


def primary_button(button):
    button.setProperty('role', 'primary')
    button.setMinimumWidth(88)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


class DialogComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('paintedArrow', True)
        self.setView(QListView(self))

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#e0e0e0' if self.isEnabled() else '#7d7d7d'), 1.4))
        x, y = self.width() - 17, self.height() / 2
        painter.drawPolyline(QPolygonF([QPointF(x - 4, y - 2),
                                        QPointF(x, y + 2), QPointF(x + 4, y - 2)]))


class StatusLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.hide()

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))

    def clear(self):
        self.setText('')


class MessageBox(QMessageBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configure_dialog(self)

    @staticmethod
    def _show(icon, parent, title, text, buttons, default_button):
        dialog = MessageBox(icon, title, text, buttons, parent)
        if default_button != QMessageBox.StandardButton.NoButton:
            dialog.setDefaultButton(default_button)
        result = QMessageBox.StandardButton(dialog.exec())
        dialog.deleteLater()
        return result

    @staticmethod
    def question(parent, title, text,
                 buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                 defaultButton=QMessageBox.StandardButton.NoButton):
        return MessageBox._show(QMessageBox.Icon.Question, parent, title, text, buttons, defaultButton)

    @staticmethod
    def information(parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                    defaultButton=QMessageBox.StandardButton.NoButton):
        return MessageBox._show(QMessageBox.Icon.Information, parent, title, text, buttons, defaultButton)

    @staticmethod
    def warning(parent, title, text, buttons=QMessageBox.StandardButton.Ok,
                defaultButton=QMessageBox.StandardButton.NoButton):
        return MessageBox._show(QMessageBox.Icon.Warning, parent, title, text, buttons, defaultButton)


def get_text(parent, title, label, *, text=''):
    dialog = QInputDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setLabelText(label)
    dialog.setTextValue(text)
    dialog.setOkButtonText('确认')
    dialog.setCancelButtonText('取消')
    configure_dialog(dialog)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    value = dialog.textValue() if accepted else ''
    dialog.deleteLater()
    return value, accepted
