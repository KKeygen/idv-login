"""SAUTH error details and non-blocking, opt-in Qt login recovery."""
import base64


CONFIRM_TIMEOUT_MS = 10000
_dialogs = {}


def failure_details(http_status, payload, *, local=False):
    """Display server error fields, with optional Base64 UTF-8 decoding."""
    payload = payload if isinstance(payload, dict) else {}
    lines = ['登录已过期' if local else '游戏登录失败']
    for label, value in (('HTTP', http_status), ('code', payload.get('code')),
                         ('subcode', payload.get('subcode'))):
        if type(value) in (int, str):
            lines.append(f'{label}: {value}')
    for field in ('msg', 'message', 'reason', 'status'):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            decoded = base64.b64decode(value, validate=True).decode('utf-8')
            if decoded and all(c.isprintable() or c in '\n\r\t' for c in decoded):
                value = decoded
        except (ValueError, UnicodeError):
            pass
        if value and value not in lines:
            lines.append(value)
    return '\n'.join(lines)


def show_message(key, text, on_confirm=None, *, timeout_ms=30000):
    """Keep all Qt work on the UI thread; close/timeout never means consent."""
    import app_state

    def create():
        from PyQt6.QtCore import Qt, QTimer
        from PyQt6.QtWidgets import QApplication
        from native_ui.dialogs import MessageBox
        if QApplication.instance() is None or key in _dialogs:
            return
        dialog = MessageBox()
        dialog.setWindowTitle('游戏登录')
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        if on_confirm is not None:
            dialog.setStandardButtons(MessageBox.StandardButton.Yes | MessageBox.StandardButton.No)
            dialog.button(MessageBox.StandardButton.Yes).setText('确认')
            dialog.button(MessageBox.StandardButton.No).setText('否')
            dialog.setDefaultButton(MessageBox.StandardButton.No)
            dialog.setEscapeButton(MessageBox.StandardButton.No)
        else:
            dialog.setStandardButtons(MessageBox.StandardButton.Ok)
            dialog.button(MessageBox.StandardButton.Ok).setText('确定')
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: dialog.done(int(MessageBox.StandardButton.No)))

        def finished(result):
            timer.stop()
            _dialogs.pop(key, None)
            dialog.deleteLater()
            if result == MessageBox.StandardButton.Yes and on_confirm is not None:
                on_confirm()

        dialog.finished.connect(finished)
        _dialogs[key] = dialog
        timer.start(timeout_ms)
        dialog.show()

    app_state.run_on_main_thread(create)


def confirm_relogin(key, details, on_confirm, *, note=''):
    text = f'{details}\n\n登录过期，是否重新登录？'
    if note:
        text += '\n' + note
    text += f'\n{CONFIRM_TIMEOUT_MS // 1000} 秒内未确认将自动关闭，不会重新登录。'
    show_message(('confirm', key), text, on_confirm, timeout_ms=CONFIRM_TIMEOUT_MS)


def relogin_succeeded():
    show_message('relogin-success', '重新登录成功，请你重新打开游戏', timeout_ms=15000)


def after_relogin_notice(callback):
    """On the UI thread, defer existing auto-quit until the notice is dismissed."""
    dialog = _dialogs.get('relogin-success')
    if dialog is None:
        return False
    if not dialog.property('autoQuitPending'):
        dialog.setProperty('autoQuitPending', True)
        dialog.finished.connect(lambda _: callback())
    return True
