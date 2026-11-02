# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import threading

import app_state


class ExpiredChannelLogin:
    def __init__(self, sync, record, game, on_finished):
        self.sync, self.record, self.game = sync, record, game
        self.on_finished = on_finished
        self.finished = False
        self.login_received = False
        self.packet_received = False
        self.cancel_event = threading.Event()
        self.qr_dialog = None

    def start(self):
        if self.sync.stopping or self.sync.manager.query_channel(self.record.uuid) is not self.record:
            self._finish(None, notify=False)
            return
        if self.record.record_source != 'manual' or not callable(
                getattr(self.record, 'request_user_login', None)):
            app_state.toast('此扫码账号登录已过期，请重新扫码登录；不会自动转换或替换账号。', duration=7000)
            self._finish(None, notify=False)
            return
        app_state.toast('账号登录已过期，请完成渠道重新登录；完成后需重启游戏。', duration=7000)
        options = {}
        if self.record.channel_name in ('huawei', 'bilibili_sdk'):
            options['login_method'] = 'web'
        elif self.record.channel_name in ('oppo', 'nearme_vivo'):
            options['game_id'] = self.game
        elif self.record.channel_name == 'lenovo_open':
            options['force'] = True
        try:
            if hasattr(self.record, 'wechatLogin'):
                from native_ui.qr_login import QrDialog
                self.qr_dialog = QrDialog('myapp')
                self.qr_dialog.rejected.connect(self.cancel)
                options.update(cancel_event=self.cancel_event, on_qrcode=self._qrcode)
                self.qr_dialog.open()
            self.record.request_user_login(
                on_complete=lambda result: app_state.run_on_main_thread(
                    lambda: self._logged_in(result)), **options)
        except Exception as error:
            self._finish(error)

    def _qrcode(self, state):
        if not self.finished and self.qr_dialog is not None:
            self.qr_dialog.update_state(state)

    def cancel(self):
        self.cancel_event.set()
        self._finish(None)

    def _logged_in(self, result):
        if self.finished or self.login_received:
            return
        self.login_received = True
        if result is not True:
            self._finish(result)
            return
        if self.sync.stopping or self.sync.manager.query_channel(self.record.uuid) is not self.record:
            self._finish(False)
            return
        try:
            self.record.request_unisdk(
                self.game, force=True, interactive=self.record.channel_name in ('oppo', 'huawei'),
                on_complete=lambda packet: app_state.run_on_main_thread(
                    lambda: self._packet_ready(packet)))
        except Exception as error:
            self._finish(error)

    def _packet_ready(self, packet):
        if self.finished or self.packet_received:
            return
        self.packet_received = True
        if not isinstance(packet, dict):
            self._finish(packet if packet is None or isinstance(packet, Exception) else False)
            return
        if self.sync.stopping or self.sync.manager.query_channel(self.record.uuid) is not self.record:
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
        if self.qr_dialog is not None:
            self.qr_dialog.accept()
            self.qr_dialog = None
        self.on_finished()
        if not notify:
            return
        if result is True:
            app_state.toast('渠道重新登录已完成，登录凭据已保存。请重启游戏后再登录。', duration=10000)
        elif result is None:
            app_state.toast('已取消渠道重新登录，账号尚未恢复。可再次点击游戏登录重试。', duration=6000)
        else:
            self.record.report_login_failure(
                self.game, 'sauth.relogin',
                '渠道重新登录未完成或凭据未能更新，请重试；尚不能使用旧登录继续游戏。',
                result if isinstance(result, Exception) else None, notify=True)
