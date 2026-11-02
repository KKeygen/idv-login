# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlencode

from PyQt6.QtCore import QEvent, QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QFileDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from native_uimgr import (UIManager as NativeUIManager, configure_application_icon,
    configure_windows_app_identity, register_url_scheme, _minimize_console_window)
from native_ui.chrome import _WindowsFramelessWindow, _fit_frameless_launcher_size
from webview2_host import WebView2Host, WebView2Error, runtime_status, MINIMUM_RUNTIME_VERSION, RUNTIME_DOWNLOAD_URL
from fever_webview2_bridge import (BASE_HEADERS, ENTRY, ORIGIN, cloud_transport_script,
    RequestRejected, authorize, json_response, navigation_allowed, trusted_document)


def _log_failure(logger, operation):
    import traceback
    error = sys.exception()
    code = getattr(error, 'hresult', None)
    label = type(error).__name__
    if isinstance(code, int):
        label += f' HRESULT=0x{code & 0xffffffff:08X}'
    frames = '\n'.join(f'  {Path(frame.f_code.co_filename).name}:{line} in {frame.f_code.co_name}'
                       for frame, line in traceback.walk_tb(error.__traceback__))
    logger.exception('%s failed\n%s', operation, frames,
                     exc_info=(RuntimeError, RuntimeError(label), None))


class _Surface(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self._owner = window
        self.host = None
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet('background: #10141b;')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.host and self.host._ready:
            self._owner._call_host('resize', self.width(), self.height())

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self.host and self.host._ready:
            reason = ('next' if event.reason() == Qt.FocusReason.TabFocusReason else
                      'previous' if event.reason() == Qt.FocusReason.BacktabFocusReason else 'programmatic')
            self._owner._call_host('focus', reason=reason)


class _ApiJobs(QObject):
    completed = pyqtSignal(int, object)

    def __init__(self, game_helper, logger, parent):
        super().__init__(parent)
        if sys.platform == 'win32':
            import pythoncom  # noqa: F401
        self.game_helper, self.logger = game_helper, logger
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix='fever-ui')
        self.capacity = threading.BoundedSemaphore(48)
        self.closed = False
        QApplication.instance().aboutToQuit.connect(self.close)

    def submit(self, rid, kind, path, query, body, method):
        if self.closed or not self.capacity.acquire(blocking=False):
            self.completed.emit(rid, json_response(503, {'error': '请求过多，请稍后重试'}))
            return

        def run():
            com = None
            try:
                if sys.platform == 'win32':
                    import pythoncom
                    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
                    com = pythoncom
                from local_handler import LocalRequestHandler
                result = LocalRequestHandler(game_helper=self.game_helper, logger=self.logger).handle_simple(path, method, query, body)
            except Exception:
                _log_failure(self.logger, 'Fever API request')
                result = json_response(500, {'success': False, 'error': '工具未能完成此请求，请重试'})
            finally:
                if com is not None:
                    com.CoUninitialize()
                self.capacity.release()
            try:
                self.completed.emit(rid, result)
            except RuntimeError:
                pass
        try:
            self.pool.submit(run)
        except RuntimeError:
            self.capacity.release()
            self.completed.emit(rid, json_response(503, {'error': '界面正在关闭'}))

    def close(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)


class _RuntimePreparation(QObject):
    progress = pyqtSignal(str)
    completed = pyqtSignal(object)

    def start(self):
        def prepare():
            from webview2_setup import ensure_runtime
            try:
                ensure_runtime(self.progress.emit)
            except Exception as error:
                self.completed.emit(error)
            else:
                self.completed.emit(None)
        threading.Thread(target=prepare, name='IDV WebView2 setup').start()


class FeverWindow(_WindowsFramelessWindow):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.host = None
        self.lease = None
        self.jobs = None
        self._document_url = ''
        self._navigation_id = None
        self._pending_url = ENTRY
        self._closing_host = False
        self._dialogs = set()
        self._failure = None
        self._preparation = None
        self._setup_status = None
        self._ready = False
        self.setWindowTitle('IDV Login · Fever WebUI')
        self.setWindowIcon(QApplication.instance().windowIcon())
        self.setMinimumSize(760, 540)
        self._layout.setContentsMargins(7, 7, 7, 7)
        self.surface = _Surface(self)
        self.set_content_widget(self.surface)
        self._window_controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._window_controls.winId()
        self._window_controls.raise_()
        screen = self.screen() or QApplication.primaryScreen()
        rect = screen.availableGeometry()
        self.resize(*_fit_frameless_launcher_size(rect.width(), rect.height()))

    def start(self):
        from webview2_setup import needs_install
        if self.host is not None or self._preparation is not None:
            return
        status = runtime_status()
        if not status['available']:
            if not needs_install(status):
                self._show_failure(status.get('error') or 'WebView2 运行时检测失败，请检查安装和系统策略。')
                return
            self._setup_status = QLabel('正在准备 Microsoft Edge WebView2 运行库…', self)
            self._setup_status.setWordWrap(True)
            self._setup_status.setTextFormat(Qt.TextFormat.PlainText)
            self._layout.addWidget(self._setup_status)
            self.surface.hide()
            self._preparation = _RuntimePreparation(self)
            self._preparation.progress.connect(self._setup_status.setText, Qt.ConnectionType.QueuedConnection)
            self._preparation.completed.connect(self._runtime_prepared, Qt.ConnectionType.QueuedConnection)
            self._preparation.start()
            return
        self._start_host()

    @pyqtSlot(object)
    def _runtime_prepared(self, error):
        self._preparation.deleteLater()
        self._preparation = None
        self._setup_status.deleteLater()
        self._setup_status = None
        if error is not None:
            from webview2_setup import RuntimeSetupError
            self._show_failure(str(error) if isinstance(error, RuntimeSetupError)
                               else '准备 WebView2 运行库失败，请重试。')
            return
        status = runtime_status()
        if not status['available']:
            self._show_failure('WebView2 安装后仍无法加载，请检查运行库和系统策略后重试。')
            return
        self._start_host()

    def _start_host(self):
        from browser_profile import ProfileLease
        from envmgr import genv
        try:
            self.surface.show()
            base = Path(genv.get('FP_WORKDIR') or os.getcwd())
            self.lease = ProfileLease(base / 'webui_webview2_profile', persistent=True)
            profile = self.lease.prepare()
            self.jobs = _ApiJobs(self.manager.game_helper, self.manager.ui_logger, self)
            self.jobs.completed.connect(self._finish_request, Qt.ConnectionType.QueuedConnection)
            self.host = WebView2Host(int(self.surface.winId()), str(profile),
                on_event=self._event, navigation_policy=navigation_allowed,
                scripts=[cloud_transport_script()])
            self.surface.host = self.host
            self.host.add_resource_filter(ENTRY, allow_document=True)
            self.host.add_resource_filter(ORIGIN + '/_idv-login/*')
            self.host.start()
        except Exception:
            _log_failure(self.manager.ui_logger, 'Fever WebView2 initialization')
            if self.host:
                self._close_host()
            elif self.lease:
                self.lease.close()
                self.lease = None
            if not self.host and self.jobs:
                self.jobs.close()
            self._show_failure('WebView2 初始化失败，请检查运行时和数据目录权限。')

    def open(self, game_id='', initial_view='', relogin_task=''):
        from channel_relogin import cancel_web_login
        previous = getattr(self, '_relogin_task', '')
        if previous != relogin_task:
            cancel_web_login(previous)
        self._relogin_task = relogin_task
        query = {}
        if relogin_task:
            query['relogin'] = relogin_task
        if game_id:
            query['game_id'] = game_id
        if initial_view:
            query['view'] = initial_view
        self._pending_url = ENTRY + ('?' + urlencode(query) if query else '')
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_(); self.activateWindow()
        if self.host and self._ready and not self._closing_host:
            self._document_url = ''
            self._navigation_id = None
            if self._call_host('visible', True):
                self._call_host('navigate', 1, self._pending_url)
        self._window_controls.raise_()

    def _close_host(self):
        if not self.host or self._closing_host:
            return
        self._closing_host = True
        try:
            self.host.close()
        except Exception:
            _log_failure(self.manager.ui_logger, 'WebView2 close')

    def _host_failed(self, message='WebView2 运行失败，请关闭窗口后重试。'):
        self._ready = False
        self._document_url = ''
        self._navigation_id = None
        self.surface.host = None
        self._close_host()
        self._show_failure(message)

    def _call_host(self, method, *args, **kwargs):
        if not self.host or self._closing_host:
            return False
        try:
            getattr(self.host, method)(*args, **kwargs)
            return True
        except Exception:
            _log_failure(self.manager.ui_logger, 'WebView2 ' + method)
            self._host_failed()
            return False

    def _event(self, name, data):
        if name == 'ready':
            if self._closing_host:
                return
            self._ready = True
            if not self._call_host('resize', self.surface.width(), self.surface.height()):
                return
            if not self._call_host('visible', self.isVisible()):
                return
            if self._call_host('navigate', 1, self._pending_url):
                self._window_controls.raise_()
        elif name == 'navigation' and data.get('page_id') == 1 and not data.get('is_frame'):
            if data.get('allowed') and data.get('kind') not in ('popup', 'external'):
                self._document_url = ''
                self._navigation_id = data.get('navigation_id')
        elif name == 'url' and data.get('page_id') == 1:
            if self._navigation_id is None or data.get('navigation_id') != self._navigation_id:
                return
            self._document_url = data.get('url', '') if trusted_document(data.get('url', '')) else ''
        elif name == 'resource_request':
            self._request(data)
        elif name == 'loaded' and data.get('page_id') == 1:
            if self._navigation_id is None or data.get('navigation_id') != self._navigation_id:
                return
            if not data.get('success') and data.get('web_error') != 14:
                self._host_failed('Fever 页面未能载入，请关闭窗口后重试。')
        elif name == 'focus_requested':
            controls = self._window_controls
            target = controls._minimize_button if data.get('reason') == 'next' else controls._close_button
            target.setFocus(Qt.FocusReason.TabFocusReason)
        elif name == 'page':
            if data.get('page_id', 1) != 1:
                self._call_host('close_page', data['page_id'])
        elif name == 'console':
            if data.get('page_id') == 1 and not data.get('is_frame') and trusted_document(data.get('source', '')):
                level = data.get('level') if data.get('level') in ('warn', 'error') else 'debug'
                self.manager.ui_logger.debug('Fever WebUI console event: %s', level)
        elif name == 'error':
            self._host_failed()
        elif name == 'closed':
            self._ready = False
            self._document_url = ''
            self._navigation_id = None
            self.surface.host = None
            if data.get('safe_to_release') is not True:
                self._closing_host = True
                self._show_failure('WebView2 未确认资料释放，将保留独立资料锁。请退出后重试。')
                return
            if self.lease:
                self.lease.close(); self.lease = None
            if self.jobs:
                self.jobs.close()
            self.host = None
            self._closing_host = False

    def _request(self, data):
        rid = data['request_id']
        try:
            kind, path, query, body = authorize(data, self._document_url)
            if kind == 'document':
                self.jobs.submit(rid, kind, '/_idv-login/index', query, None, 'GET')
            elif kind == 'api' and path.startswith('/_idv-login/webview2/'):
                self._native_action(rid, path, body, data.get('method'))
            else:
                self.jobs.submit(rid, kind, path, query, body, data.get('method'))
        except RequestRejected as error:
            self._finish_request(rid, json_response(error.status, {'error': str(error)}))
        except Exception:
            _log_failure(self.manager.ui_logger, 'Fever WebUI response')
            self._finish_request(rid, json_response(500, {'error': '界面请求失败，请重试'}))

    @pyqtSlot(int, object)
    def _finish_request(self, rid, result):
        if not self.host or self._closing_host:
            return
        status, headers, body = result
        headers = {**BASE_HEADERS, **headers}
        for key in list(headers):
            if key.lower().startswith('access-control-') or key.lower() in ('set-cookie', 'location'):
                del headers[key]
        try:
            self.host.respond(rid, status, HTTPStatus(status).phrase, headers, body)
        except (WebView2Error, ValueError):
            pass

    def _native_action(self, rid, path, body, method):
        if path not in ('/_idv-login/webview2/copy-text', '/_idv-login/webview2/save-text'):
            raise RequestRejected(404, 'Not found')
        if method != 'POST' or not isinstance(body, dict):
            raise RequestRejected(405, 'POST required')
        text = body.get('text')
        if not isinstance(text, str) or len(text.encode('utf-8')) > 4096:
            raise RequestRejected(400, 'Invalid text')
        if path.endswith('/copy-text'):
            QApplication.clipboard().setText(text)
            self._finish_request(rid, json_response(200, {'success': True}))
            return
        from nativebridge import NativeTaskRegistry
        task_id = NativeTaskRegistry.create('webui-save-text')
        NativeTaskRegistry.update(task_id, reconciled=True)
        self._finish_request(rid, json_response(202, {'success': True, 'status': 'pending', 'task_id': task_id}))
        filename = body.get('filename', 'idv-cloud-master-key.txt')
        if not isinstance(filename, str) or not __import__('re').fullmatch(r'idv-cloud-master-key(?:-\d{4}-\d{2}-\d{2})?\.txt', filename):
            filename = 'idv-cloud-master-key.txt'
        dialog = QFileDialog(self, '保存主密钥文本', filename, '文本文件 (*.txt)')
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix('txt')
        self._dialogs.add(dialog)
        finished = [False]

        def finish(accepted):
            if finished[0]:
                return
            finished[0] = True
            success = False
            try:
                if accepted and dialog.selectedFiles():
                    from secure_write import _atomic_write
                    _atomic_write(dialog.selectedFiles()[0], text.encode('utf-8'))
                    success = True
                NativeTaskRegistry.finish(task_id, success=success, cancelled=not accepted)
            except Exception:
                _log_failure(self.manager.ui_logger, 'Fever text export')
                NativeTaskRegistry.finish(task_id, success=False, error='保存失败，请检查目标目录权限')
            finally:
                self._dialogs.discard(dialog)
                dialog.deleteLater()
        dialog.accepted.connect(lambda: finish(True))
        dialog.rejected.connect(lambda: finish(False))
        dialog.open()

    def _show_failure(self, message):
        if self._failure is None:
            self._failure = QWidget(self)
            layout = QVBoxLayout(self._failure)
            text = QLabel(self._failure); text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.PlainText)
            self._failure_text = text
            layout.addWidget(text)
            runtime_link = QPushButton('WebView2 官方运行时页面', self._failure)
            runtime_link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(RUNTIME_DOWNLOAD_URL)))
            layout.addWidget(runtime_link)
            close = QPushButton('关闭', self._failure)
            close.clicked.connect(self.close)
            layout.addWidget(close)
            self._layout.addWidget(self._failure)
        self._failure_text.setText(message)
        if self.host and self._ready:
            self._call_host('visible', False)
        self.surface.hide()
        self._failure.show()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            inset = 0 if self.isMaximized() else 7
            self._layout.setContentsMargins(inset, inset, inset, inset)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.host and self._ready:
            self._call_host('notify_position_changed')

    def closeEvent(self, event):
        from channel_relogin import cancel_web_login
        cancel_web_login(getattr(self, '_relogin_task', ''))
        for dialog in tuple(self._dialogs):
            dialog.reject()
        if self.host and self._ready:
            self._call_host('visible', False)
        self.hide()
        event.ignore()


class UIManager(NativeUIManager):
    def _do_open_for_game(self, game_id='', initial_view='', relogin_task=''):
        if self._window is None:
            self._window = FeverWindow(self)
            self._window.open(game_id, initial_view, relogin_task)
            self._window.start()
        elif self._window.host is None and self._window._failure is not None:
            old = self._window
            self._window = None
            old.deleteLater()
            self._do_open_for_game(game_id, initial_view, relogin_task)
            return
        else:
            self._window.open(game_id, initial_view, relogin_task)
        self._force_foreground()
        _minimize_console_window()
