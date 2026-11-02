# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import threading
import uuid

import app_state


def cancel_web_login(task_id):
    if not task_id:
        return
    sync = getattr(getattr(app_state, 'channels_helper', None), 'db_sync', None)
    attempt = getattr(sync, '_relogin_tasks', {}).get(task_id)
    if attempt is not None:
        attempt.cancel()


class ExpiredChannelLogin:
    def __init__(self, sync, record, game, on_finished):
        self.sync, self.record, self.game = sync, record, game
        self.on_finished = on_finished
        self.task_id = uuid.uuid4().hex
        self.finished = False
        self.started = False
        self.login_received = False
        self.packet_received = False
        self.cancel_event = threading.Event()
        self.previous_windows = {}
        self.qrcode_state = {'status': 'loading', 'qrcode_base64': ''}
        self.status = 'waiting'
        self.success = False
        self.cancelled = False
        self.message = '账号登录已过期，正在准备重新登录…'

    def public_state(self):
        return {'task_id': self.task_id, 'game_id': self.game,
                'uuid': self.record.uuid, 'channel': self.record.channel_name,
                'name': self.record.name, 'status': self.status,
                'success': self.success, 'cancelled': self.cancelled,
                'message': self.message, 'qr': hasattr(self.record, 'wechatLogin')}

    def _available(self):
        return not self.sync.stopping and self.sync.manager.query_channel(self.record.uuid) is self.record

    def start(self):
        if not self._available():
            self._finish(None, notify=False)
            return
        if self.record.record_source != 'manual' or not callable(
                getattr(self.record, 'request_user_login', None)):
            app_state.toast('此扫码账号登录已过期，请重新扫码登录；不会自动转换或替换账号。', duration=7000)
            self._finish(None, notify=False)
            return
        app_state.toast('账号登录已过期，请完成渠道重新登录；完成后需重启游戏。', duration=7000)
        try:
            if app_state.ui_mgr is None:
                raise RuntimeError('账号管理页面不可用')
            app_state.ui_mgr.open_for_game(self.game, 'accounts', relogin_task=self.task_id)
        except Exception as error:
            self._finish(error)

    def _login_windows(self):
        windows = {}
        for owner in (self.record, *vars(self.record).values()):
            values = getattr(owner, '__dict__', {})
            for field in ('_active_browser', '_active_dialog', '_login_dialog'):
                window = values.get(field)
                if window is not None and callable(getattr(window, 'close', None)):
                    windows[id(window)] = window
        return windows

    def begin(self):
        if self.finished or self.started or self.cancel_event.is_set():
            return
        if not self._available():
            self._finish(None, notify=False)
            return
        self.previous_windows = self._login_windows()
        self.started = True
        self.status = 'pending'
        self.message = '请完成此账号的渠道登录。'
        options = {}
        if self.record.channel_name in ('huawei', 'bilibili_sdk'):
            options['login_method'] = 'web'
        elif self.record.channel_name in ('oppo', 'nearme_vivo'):
            options['game_id'] = self.game
        elif self.record.channel_name == 'lenovo_open':
            options['force'] = True
        if self.record.channel_name == 'baidu' or hasattr(self.record, 'wechatLogin'):
            options['cancel_event'] = self.cancel_event
        if hasattr(self.record, 'wechatLogin'):
            options['on_qrcode'] = self._qrcode
        try:
            self.record.request_user_login(
                on_complete=lambda result: app_state.run_on_main_thread(
                    lambda: self._logged_in(result)), **options)
        except Exception as error:
            self._finish(error)

    def _qrcode(self, data):
        if not self.finished and not self.cancel_event.is_set():
            self.qrcode_state = dict(data)

    def cancel(self):
        if self.finished:
            return
        windows = self._login_windows() if self.started else {}
        self.cancel_event.set()
        self._finish(None)
        for key, window in windows.items():
            if key not in self.previous_windows:
                try:
                    cancel = getattr(window, 'cancel_login', None)
                    (cancel if callable(cancel) else window.close)()
                except RuntimeError:
                    pass

    def _logged_in(self, result):
        if self.finished or self.login_received or self.cancel_event.is_set():
            return
        self.login_received = True
        if result is not True:
            self._finish(result)
            return
        if not self._available():
            self._finish(False)
            return
        self.message = '渠道登录完成，正在更新游戏登录凭据…'
        try:
            self.record.request_unisdk(
                self.game, force=True, interactive=self.record.channel_name in ('oppo', 'huawei'),
                cancel_event=self.cancel_event,
                on_complete=lambda packet: app_state.run_on_main_thread(
                    lambda: self._packet_ready(packet)))
        except Exception as error:
            self._finish(error)

    def _packet_ready(self, packet):
        if self.finished or self.packet_received or self.cancel_event.is_set():
            return
        self.packet_received = True
        if not isinstance(packet, dict):
            self._finish(packet if packet is None or isinstance(packet, Exception) else False)
            return
        if not self._available():
            self._finish(False)
            return
        try:
            self.sync.manager.save_records()
            self.sync.refresh_record(self.record, self.game)
        except Exception as error:
            self._finish(error)
            return
        self._finish(True)

    def _finish(self, result, *, notify=True):
        if self.finished:
            return
        self.finished = True
        self.cancel_event.set()
        self.status = 'done'
        self.success = result is True
        self.cancelled = result is None
        if result is True:
            self.message = '渠道重新登录已完成，登录凭据已保存。请重启游戏后再登录。'
        elif result is None:
            self.message = '已取消渠道重新登录，账号尚未恢复。可再次点击游戏登录重试。'
        else:
            self.message = '渠道重新登录未完成或凭据未能更新，请重试；尚不能使用旧登录继续游戏。'
        self.on_finished()
        if not notify:
            return
        if result is True or result is None:
            app_state.toast(self.message, duration=10000 if result is True else 6000)
        else:
            self.record.report_login_failure(self.game, 'sauth.relogin', self.message,
                result if isinstance(result, Exception) else None, notify=True)
