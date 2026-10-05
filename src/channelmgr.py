# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <https://www.gnu.org/licenses/>.
 """

import os
import copy
import json
import random
import time
import requests
from envmgr import genv
from logutil import setup_logger
from const import manual_login_channels
from channelHandler.channelUtils import cmp_game_id, getShortGameId
from channel_cache import packet_identity, credential_expires_at, exception_summary
from ssl_utils import should_verify_ssl


def legacy_record_source(data: dict) -> str:
    """按旧版实际落盘字段区分手动账号与扫码记录，不按渠道名猜测。"""
    if not isinstance(data, dict):
        raise ValueError('账号记录必须是JSON对象')
    login = data.get('login_info')
    if not isinstance(login, dict) or not isinstance(login.get('login_channel'), str) or not login['login_channel']:
        raise ValueError('账号记录缺少有效login_info.login_channel')
    if not isinstance(data.get('uuid', ''), str):
        raise ValueError('账号记录uuid必须是字符串')
    source = data.get("record_source")
    if source in ("manual", "scan"):
        return source
    if source is not None:
        raise ValueError("账号记录的来源标记无法识别")
    login_channel = data["login_info"]["login_channel"]
    credential_fields = {
        "xiaomi_app": "oAuthData", "huawei": "serviceToken",
        "nearme_vivo": "chosenAccount", "myapp": "session_json",
        "oppo": "loginResp", "bilibili_sdk": "loginResp",
        "honor_sdk": "unionToken", "uc_platform": "ucSession",
        "4399com": "loginResp", "360_assistant": "qt_cookie",
    }
    field = credential_fields.get(login_channel)
    # v5 的华为适配器保存 refreshToken；当前版本改为 serviceToken。
    if (field and field in data) or (login_channel == "huawei" and "refreshToken" in data):
        if login_channel == "myapp" and not data.get("uuid", "").removeprefix("idv-").startswith(("wx-", "qq-")):
            raise ValueError("旧应用宝记录无法区分微信和 QQ")
        return "manual"
    user = data.get("user_info", {})
    if isinstance(user, dict) and user.get("id") and user.get("token"):
        return "scan"
    raise ValueError("旧账号记录无法确定手动或扫码来源")


class channel:
    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        uuid: str = "",
    ) -> None:
        self.login_info = login_info
        self.record_source = "scan" if type(self) is channel else "manual"
        self.user_info = user_info
        self.ext_info = ext_info
        self.device_info = device_info
        self.exchange_data = {
            "device": device_info,
            "ext_info": ext_info,
            "user": user_info,
        }

        self.create_time = create_time
        self.last_login_time = last_login_time
        self.uuid = f"{login_info['login_channel']}-{login_info['code']}" if uuid == "" else uuid
        self.channel_name = (ext_info.get("src_app_channel2") or login_info["login_channel"]) if self.record_source == "scan" else login_info["login_channel"]
        self.game_id = ext_info.get("from_game_id", "")
        self.crossGames = type(self) is not channel
        self.unisdk_cache = {}
        self.expires_at = None
        if name == "":
            self.name = self.uuid
        else:
            self.name = name

    @classmethod
    def from_dict(cls, data: dict):
        restored = cls(
            login_info=data.get("login_info", {}),
            user_info=data.get("user_info", {}),
            ext_info=data.get("ext_info", {}),
            device_info=data.get("device_info", {}),
            create_time=data.get("create_time", int(time.time())),
            last_login_time=data.get("last_login_time", 0),
            name=data.get("name", ""),
            uuid=data.get("uuid", ""),
        )

        return restored.restore_common(data)

    def restore_common(self, data):
        """Restore shared state after either a scan or an adapter's from_dict."""
        for key in ('uuid', 'game_id', 'crossGames', 'token_issued_at'):
            if key in data:
                setattr(self, key, data[key])
        self.active_sdkuid = str(data.get('active_sdkuid') or '')
        self.unisdk_cache = {}
        self.expires_at = data.get('expires_at')
        if self.expires_at is not None:
            try:
                self.expires_at = int(self.expires_at)
            except (TypeError, ValueError, OverflowError):
                self.expires_at = int(time.time())
        saved = data.get('unisdk_cache')
        if self.record_source == 'manual' and isinstance(saved, dict):
            # Upgrade the old single packet only into its own actual SAUTH game.
            if 'extra_unisdk_data' in saved:
                try:
                    game = packet_identity(saved)[0]
                    saved = {game: {'packet': saved, 'expires_at': self.expires_at}}
                except ValueError as error:
                    self.report_login_failure(self.game_id, 'cache.restore.legacy',
                        '旧缓存身份无效，未恢复该缓存', error)
                    saved = {}
            for game, entry in saved.items():
                if not isinstance(entry, dict) or not isinstance(entry.get('packet'), dict):
                    self.report_login_failure(str(game), 'cache.restore', '缓存格式无效，未恢复该游戏缓存',
                        detail='field=unisdk_cache.packet: expected object')
                    continue
                packet = entry['packet']
                try:
                    identity = packet_identity(packet)
                    if identity[0] != game:
                        raise ValueError('field=SAUTH_JSON.gameid: differs from cache game')
                    if identity[1] != self.channel_name:
                        raise ValueError('field=SAUTH_JSON.login_channel: differs from Channel')
                    for field in ('user_id', 'token'):
                        if not packet.get(field):
                            raise ValueError(f'field=packet.{field}: missing value')
                    expiry = entry.get('expires_at')
                    expiry = int(expiry) if expiry is not None else None
                    self.unisdk_cache[game] = {'packet': packet, 'expires_at': expiry}
                except (ValueError, TypeError, OverflowError) as error:
                    self.report_login_failure(str(game), 'cache.restore', '缓存校验失败，未恢复该游戏缓存', error)
                    continue
            if self.unisdk_cache:
                self.expires_at = None
        return self

    def current_sdkuid(self):
        """Read the last actual selection/login result, independently of cache."""
        if (self.user_info.get('login_channel') or self.channel_name) == 'netease':
            return ''
        attrs = vars(self)
        if self.record_source == 'scan':
            try:
                return packet_identity({
                    'extra_unisdk_data': (self.user_info.get('pc_ext_info') or self.ext_info).get('extra_unisdk_data', ''),
                    'login_channel': self.channel_name,
                })[2]
            except ValueError:
                return ''
        name = self.channel_name
        if name in ('xiaomi_app', 'huawei'):
            return str(attrs.get('active_sdkuid') or '')
        if name == 'nearme_vivo':
            return str(getattr(attrs.get('activeAccount'), 'subOpenId', '') or attrs.get('chosenAccount') or '')
        if name == 'oppo':
            return str(((attrs.get('oppo_open_account') or {}).get('chosen_account') or {}).get('account_id') or '')
        if name == 'uc_platform':
            data = attrs.get('ucSession') or {}
            data = data if 'sid' in data else data.get('data') or {}
            return str(data.get('accountId') or data.get('ucid') or '')
        if name in ('myapp', 'myapp_qq'):
            session = attrs.get('session')
            return str((session.get('openid') if isinstance(session, dict) else getattr(session, 'openid', '')) or '')
        if name in ('honor', 'honor_sdk'):
            return str((attrs.get('unionToken') or {}).get('openId') or '')
        if name in ('bilibili_sdk', '4399com'):
            data = attrs.get('loginResp') or {}
            data = data.get('data' if name == 'bilibili_sdk' else 'result') or data
            return str(data.get('uid') or '')
        if name in ('qihoo', '360_assistant'):
            return str((attrs.get('userInfo') or {}).get('qid') or '')
        return str(attrs.get('active_sdkuid') or '')

    def sdk_identity(self, game_id):
        """None means NetEase, another scan game, or no packet acquired yet."""
        if (self.user_info.get('login_channel') or self.channel_name) == 'netease':
            return None
        if self.record_source == 'scan':
            ext = self.user_info.get('pc_ext_info') or self.ext_info
            if not isinstance(ext, dict):
                raise ValueError('field=pc_ext_info: expected object')
            packet = {'login_channel': self.channel_name,
                      'extra_unisdk_data': ext.get('extra_unisdk_data', '')}
        else:
            entry = self.unisdk_cache.get(getShortGameId(game_id))
            if entry is None:
                return None
            if not isinstance(entry, dict):
                raise ValueError('field=unisdk_cache.game: expected object')
            packet = entry.get('packet')
        identity = packet_identity(packet)
        if not cmp_game_id(identity[0], game_id):
            if self.record_source == 'scan':
                return None  # A healthy scan for another game is simply not a match.
            raise ValueError('field=SAUTH_JSON.gameid: differs from requested game')
        if identity[1] != self.channel_name:
            raise ValueError('field=SAUTH_JSON.login_channel: differs from Channel')
        return identity

    def report_login_failure(self, game_id, stage, message, error=None, *, detail='', notify=False, on_error=None):
        """Keep operation context and traceback locations, without payloads or locals."""
        logger = setup_logger()
        logger.debug('[channel] account={} game={} channel={} stage={} result={} {}',
                     self.uuid, getShortGameId(game_id), self.channel_name, stage, message, detail)
        if error is not None:
            logger.debug('{}', exception_summary(error))
        if on_error is not None:
            on_error(message)
        elif notify:
            import app_state
            app_state.toast(f'{self.name}（{getShortGameId(game_id)}）：{message}', duration=6000)

    def observe_sdkuid(self, sdkuid, game_id=None):
        """Observe an adapter result; only that game's cached selection changes."""
        sdkuid = str(sdkuid or '')
        if not sdkuid:
            return
        self.active_sdkuid = sdkuid
        if game_id is None and not self.crossGames:
            game_id = self.game_id
        if game_id:
            game = getShortGameId(game_id)
            entry = self.unisdk_cache.get(game)
            if entry:
                try:
                    if packet_identity(entry['packet'])[2] == sdkuid:
                        return
                except (KeyError, ValueError):
                    pass
                self.unisdk_cache.pop(game, None)

    def cache_unisdk(self, packet, expires_at=None):
        if self.record_source == 'scan':
            return packet
        game, login_channel, sdkuid = packet_identity(packet)
        if login_channel != self.channel_name:
            raise ValueError('field=SAUTH_JSON.login_channel: differs from Channel')
        for field in ('user_id', 'token'):
            if not packet.get(field):
                raise ValueError(f'field=packet.{field}: missing value')
        self.observe_sdkuid(sdkuid, game)
        self.unisdk_cache[game] = {'packet': copy.deepcopy(packet), 'expires_at': expires_at}
        self.expires_at = None
        return packet

    def cached_unisdk(self, game_id):
        game = getShortGameId(game_id)
        entry = self.unisdk_cache.get(game)
        if not entry:
            return None
        packet = entry['packet']
        try:
            if packet_identity(packet)[:2] != (game, self.channel_name):
                return None
        except ValueError:
            return None
        expiry = entry.get('expires_at')
        if expiry is not None and expiry <= time.time():
            return None
        return copy.deepcopy(packet)

    def is_login_expired(self, game_id):
        """Read existing expiry only; this does not refresh or validate a login."""
        if self.record_source == 'manual':
            entry = self.unisdk_cache.get(getShortGameId(game_id))
            deadlines = [entry.get('expires_at') if entry else self.expires_at]
        else:
            if not cmp_game_id(self.game_id, game_id):
                return False
            deadlines = [self.expires_at, credential_expires_at(self)]
        return any(value is not None and value <= time.time() for value in deadlines)

    def expire_unisdk(self, game_id=None):
        if self.record_source == 'scan':
            if game_id and not cmp_game_id(self.game_id, game_id):
                return False
            self.expires_at = int(time.time())
            return True
        games = [getShortGameId(game_id)] if game_id else list(self.unisdk_cache)
        changed = False
        for game in games:
            if game in self.unisdk_cache:
                self.unisdk_cache[game]['expires_at'] = int(time.time())
                changed = True
        if not changed:
            self.expires_at = int(time.time())  # No packet yet; retain the existing UI failure status.
        return True

    def request_unisdk(self, game_id='', on_complete=None, *, interactive=True, force=False):
        """Reuse one fresh game packet, otherwise run the unchanged adapter."""
        game_id = game_id or self.game_id
        if self.record_source == 'manual':
            genv.set('GLOB_LOGIN_UUID', self.uuid)
        # A cached last choice must not silently remember an OPPO choice that
        # the user deliberately left unfixed in the original login dialog.
        choose_again = interactive and self.record_source == 'manual' and self.channel_name == 'oppo' and not self.chosen_account_id
        packet = None if force or choose_again or self.record_source == 'scan' else self.cached_unisdk(game_id)
        if packet is not None:
            if on_complete is not None:
                on_complete(packet)
                return None
            return packet

        def deliver(result):
            if on_complete is not None:
                on_complete(result)
                return None
            if isinstance(result, Exception):
                raise result
            return result

        def ready(result):
            if result is None or result is False or isinstance(result, Exception):
                return deliver(result)
            try:
                identity = packet_identity(result)
                if not cmp_game_id(identity[0], game_id):
                    raise ValueError('field=SAUTH_JSON.gameid: differs from requested game')
                if self.record_source == 'manual':
                    self.cache_unisdk(result, credential_expires_at(self))
            except (TypeError, ValueError, AttributeError, OverflowError) as error:
                return deliver(error)
            return deliver(result)

        try:
            if on_complete is not None:
                return self.get_uniSdk_data(game_id, on_complete=ready, interactive=interactive)
            result = self.get_uniSdk_data(game_id, interactive=interactive)
        except Exception as error:
            return deliver(error)
        return ready(result)

    def get_uniSdk_data(self, game_id: str = "", on_complete=None, *, interactive=True):
        if "id" not in self.user_info or "token" not in self.user_info:
            raise ValueError("渠道登录信息不完整：缺少 user_id 或 token，请重新登录")
        result = {
            "user_id": self.user_info["id"],
            "token": self.user_info["token"],
            "login_channel": self.ext_info.get("src_app_channel2") or self.channel_name,
            "udid": self.ext_info.get("src_udid", ""),
            "app_channel": self.ext_info.get("src_app_channel", ""),
            "sdk_version": self.ext_info.get("src_jf_game_id", ""),
            "jf_game_id": self.ext_info.get("src_jf_game_id", ""),
            "pay_channel": self.ext_info.get("src_pay_channel", ""),
            "extra_data": "",
            "extra_unisdk_data": self.ext_info.get("extra_unisdk_data", ""),
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }

        if on_complete is not None:
            on_complete(result)
        return result

    def get_non_sensitive_data(self):
        return {
            "create_time": self.create_time,
            "last_login_time": self.last_login_time,
            "uuid": self.uuid,
            "name": self.name,
            "record_source": self.record_source,
        }

    def mark_manual_login_success(self, game_id=None):
        from prefetch_context import in_prefetch
        game = getShortGameId(game_id or self.game_id)
        self.observe_sdkuid(self.current_sdkuid(), game)
        if self.record_source != "manual" or in_prefetch():
            return
        # A real interactive login supplied new credentials, even for the same UID.
        self.unisdk_cache.pop(game, None)
        self.last_login_time = time.time()
        import app_state
        manager = app_state.channels_helper
        if manager is not None and any(record is self for record in manager.channels):
            manager.save_records()

    def before_save(self):
        pass
class ChannelManager:
    def __init__(self):
        self.logger = setup_logger()
        try:
            genv.discard_cached('native_channel_accounts', 'mpay_db_helpers',
                                'mpay_db_bindings', 'mpay_db_writes')
        except (OSError, ValueError, TypeError) as error:
            self.logger.error('旧账号映射配置无法读取 [{}]；保留原配置文件', type(error).__name__)
        self.channels = []
        self.db_sync = None
        self._pending_login_channel = None  # 异步登录期间保持对 channel 的引用，防止 GC
        from channelHandler.miChannelHandler import miChannel
        from channelHandler.huaChannelHandler import huaweiChannel
        from channelHandler.vivoChannelHandler import vivoChannel
        from channelHandler.wechatChannelHandler import wechatChannel
        from channelHandler.oppoChannelHandler import oppoChannel
        from channelHandler.bilibiliChannelHandler import bilibiliChannel

        path = genv.get("FP_CHANNEL_RECORD")
        if genv.get('CHANNEL_RECORDS_LOAD_FAILED', False):
            return  # A failed migration must not create an empty replacement file.
        if os.path.exists(path):
            try:
                with open(path, "r", encoding='utf-8') as file:
                    data = json.load(file)
                if not isinstance(data, list):
                    raise ValueError('账号文件顶层必须是JSON数组')
            except (OSError, ValueError, UnicodeDecodeError) as error:
                genv.set('CHANNEL_RECORDS_LOAD_FAILED', True)
                self.logger.error('file={} stage=records.load 读取失败；保留原文件，禁止覆盖\n{}',
                                  os.path.basename(path), exception_summary(error))
                return
            for index, item in enumerate(data, 1):
                before = len(self.channels)
                try:
                    source = legacy_record_source(item)
                    if source == "scan":
                        self.channels.append(channel.from_dict(item))
                        continue
                    channel_name = item["login_info"]["login_channel"]
                    if channel_name == "xiaomi_app":

                        tmpChannel: miChannel = miChannel.from_dict(item)
                        # if tmpChannel.is_token_valid():
                        self.channels.append(tmpChannel)
                        # else:
                        #    self.logger.error(f"渠道服登录信息失效: {tmpChannel.name}")
                    elif channel_name == "huawei":
                        tmpChannel: huaweiChannel = huaweiChannel.from_dict(item)
                        # if tmpChannel.is_token_valid():
                        self.channels.append(tmpChannel)
                        # else:
                        #    self.logger.error(f"渠道服登录信息失效: {tmpChannel.name}")
                    elif channel_name =="nearme_vivo":
                        tmpChannel: vivoChannel = vivoChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "myapp" and item["uuid"].removeprefix("idv-").startswith("wx-"):
                        tmpChannel:wechatChannel=wechatChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "myapp" and item["uuid"].removeprefix("idv-").startswith("qq-"):
                        from channelHandler.qqChannelHandler import qqChannel
                        tmpChannel: qqChannel = qqChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "oppo" and item["uuid"].removeprefix("idv-").startswith("phone-"):
                        tmpChannel: oppoChannel = oppoChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "bilibili_sdk" and item["uuid"].removeprefix("idv-").startswith("bili-"):
                        tmpChannel: bilibiliChannel = bilibiliChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "honor_sdk" and item["uuid"].removeprefix("idv-").startswith("honor-"):
                        from channelHandler.honorChannelHandler import honorChannel
                        tmpChannel: honorChannel = honorChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "uc_platform" and item["uuid"].removeprefix("idv-").startswith("uc-"):
                        from channelHandler.ucChannelHandler import ucChannel
                        tmpChannel: ucChannel = ucChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "4399com" and item["uuid"].removeprefix("idv-").startswith("4399-"):
                        from channelHandler.m4399ChannelHandler import m4399Channel
                        tmpChannel: m4399Channel = m4399Channel.from_dict(item)
                        self.channels.append(tmpChannel)
                    elif channel_name == "360_assistant" and item["uuid"].removeprefix("idv-").startswith("360-"):
                        from channelHandler.qihooChannelHandler import qihooChannel
                        tmpChannel: qihooChannel = qihooChannel.from_dict(item)
                        self.channels.append(tmpChannel)
                    else:
                        self.logger.error(f"跳过第 {index} 条不支持的渠道账号记录：不支持的手动渠道账号类型")
                        continue
                    # 适配器构造完成后统一恢复公共缓存和旧记录标识。
                    self.channels[-1].restore_common(item)
                except Exception as error:
                    del self.channels[before:]
                    self.logger.error('file={} record_index={} stage=records.restore 加载失败；跳过此条，继续其余账号\n{}',
                                      os.path.basename(path), index, exception_summary(error))
        else:
            from secure_write import write_json_restricted
            write_json_restricted(path, [])

    def save_records(self):
        if genv.get('CHANNEL_RECORDS_LOAD_FAILED', False):
            raise ValueError('现有账号文件未能完整读取；保留原文件，修复后再保存')
        for i in self.channels:
            i.before_save()
        oldData = [channel.__dict__.copy() for channel in self.channels]
        data = oldData.copy()
        for channel_data in data:
            to_be_deleted = []
            for key in channel_data.keys():
                mini_data = {"data": channel_data[key]}
                try:
                    json.dumps(mini_data)
                except:
                    to_be_deleted.append(key)
            for key in to_be_deleted:
                del channel_data[key]
        from secure_write import write_json_restricted
        write_json_restricted(genv.get("FP_CHANNEL_RECORD"), data)
        self.logger.debug("渠道服登录信息已更新")
        callback = genv.get("CHANNELS_UPDATED_CALLBACK", None)
        if callable(callback):
            try:
                callback("channel_records_updated")
            except Exception as error:
                self.logger.debug('触发账号记录更新回调失败\n{}', exception_summary(error))

    def list_channels(self,game_id: str):
        return sorted(
            [channel.get_non_sensitive_data()  for channel in self.channels if game_id == "" or channel.crossGames or (cmp_game_id(channel.game_id, game_id))],
            key=lambda x: x["last_login_time"],
            reverse=True,
        )

    def _manual_channel_name(self, login_channel: str, game_id: str) -> str:
        """当前游戏支持该渠道手动登录时返回显示名，否则返回空串。"""
        from channelHandler.channelUtils import getShortGameId
        entries = []
        if game_id:
            try:
                from cloudRes import CloudRes
                entries = CloudRes().get_all_by_game_id(getShortGameId(game_id)) or []
            except Exception:
                entries = []
        if not entries:
            entries = manual_login_channels
        for item in entries:
            if not isinstance(item, dict):
                continue
            if (item.get("channel") or item.get("app_channel")) == login_channel:
                return item.get("name") or login_channel
        return ""

    def import_from_scan(self, login_info: dict, exchange_info: dict, game_id: str = ""):
        # Official accounts keep MPay's own remember/save behavior.
        if exchange_info["user"]["login_channel"].startswith("netease"):
            return False
        tmp_channel: channel = channel(
            login_info,
            exchange_info["user"],
            exchange_info["ext_info"] if "ext_info" in exchange_info.keys() else {},
            exchange_info["device"] if "device" in exchange_info.keys() else {},
            name=str(exchange_info["user"].get("client_username")
                     or exchange_info["user"].get("nickname")
                     or exchange_info["user"]["id"]),
        )
        from channelHandler.channelUtils import getShortGameId
        from account_list_policy import native_scan_channels
        tmp_channel.game_id = getShortGameId(game_id or tmp_channel.game_id)
        tmp_channel.crossGames = False
        tmp_channel.last_login_time = time.time()
        import app_state
        login_channel = tmp_channel.ext_info.get("src_app_channel2") or login_info["login_channel"]

        if login_channel in native_scan_channels(tmp_channel.game_id):
            app_state.toast("扫码登录由游戏原生保存，可在游戏账号列表中继续使用。", duration=5000)
            return False

        manual_name = self._manual_channel_name(login_channel, game_id)
        if manual_name:
            self.logger.info(f"正在扫码导入{manual_name}账号，保存时间较短，请前往渠道服管理界面手动登录以长期保存。")
            toast_text = f"您正在扫码导入{manual_name}账号，保存时间较短，请及时参看教程，前往【渠道服管理界面】操作"
        else:
            toast_text = "扫码结果已临时保存，时长约3天，可在游戏的下拉框中选择账号登录。"

        previous = self.channels[:]
        saved = False
        try:
            identity = tmp_channel.sdk_identity(tmp_channel.game_id)
            if identity is None:
                raise ValueError('field=SAUTH_JSON.gameid: differs from scan game')
            duplicates = []
            for record in self.channels:
                if record.record_source != 'scan':
                    continue
                try:
                    if record.sdk_identity(tmp_channel.game_id) == identity:
                        duplicates.append(record)
                except ValueError as error:
                    record.report_login_failure(tmp_channel.game_id, 'scan.previous.identity',
                        '旧扫码记录身份无效，跳过此条', error)
            if duplicates:
                latest = max(duplicates, key=lambda item: item.last_login_time)
                tmp_channel.name, tmp_channel.uuid = latest.name, latest.uuid
            self.channels[:] = [record for record in self.channels if record not in duplicates]
            self.channels.append(tmp_channel)
            self.save_records()
            saved = True
            from account_list_policy import AccountListPolicy
            AccountListPolicy(self).include_logged_in(tmp_channel)
        except Exception as error:
            if not saved:
                self.channels[:] = previous
            tmp_channel.report_login_failure(tmp_channel.game_id, 'scan.selection' if saved else 'scan.save',
                '扫码账号已保存，但游戏内切换选择未保存，请重新勾选' if saved else
                '扫码流程继续，但工具未能保存此账号，请检查账号配置后重新扫码', error, notify=True)
            return False
        app_state.toast(toast_text, duration=5000)
        return True

    def manual_import(self, channle_name: str, game_id: str, on_complete=None, login_method: str = "", on_error=None):
        tmpData = {
            "code": str(random.randint(100000, 999999)),
            "src_client_type": 1,
            "login_channel": channle_name,
            "src_client_country_code": "CN",
        }
        try:
            if channle_name == "xiaomi_app":
                from channelHandler.miChannelHandler import miChannel

                tmp_channel: miChannel = miChannel(tmpData,game_id=game_id)
            elif channle_name == "huawei":
                from channelHandler.huaChannelHandler import huaweiChannel

                tmp_channel: huaweiChannel = huaweiChannel(tmpData,game_id=game_id)
            elif channle_name == "nearme_vivo":
                from channelHandler.vivoChannelHandler import vivoChannel

                tmp_channel: vivoChannel = vivoChannel(tmpData,game_id=game_id)
            elif channle_name == "myapp":
                from channelHandler.wechatChannelHandler import wechatChannel
                tmp_channel: wechatChannel = wechatChannel(tmpData,game_id=game_id)
                tmp_channel.uuid=f"wx-{tmp_channel.uuid}"
            elif channle_name == "myapp_qq":
                from channelHandler.qqChannelHandler import qqChannel
                tmpData["login_channel"] = "myapp"
                tmp_channel: qqChannel = qqChannel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"qq-{tmp_channel.uuid}"
            elif channle_name == "oppo":
                from channelHandler.oppoChannelHandler import oppoChannel
                tmp_channel: oppoChannel = oppoChannel(tmpData, game_id=game_id)
                tmp_channel.uuid=f"phone-{tmp_channel.uuid}"
            elif channle_name == "bilibili_sdk":
                from channelHandler.bilibiliChannelHandler import bilibiliChannel
                tmp_channel: bilibiliChannel = bilibiliChannel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"bili-{tmp_channel.uuid}"
            elif channle_name == "honor_sdk":
                from channelHandler.honorChannelHandler import honorChannel
                tmp_channel: honorChannel = honorChannel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"honor-{tmp_channel.uuid}"
            elif channle_name == "uc_platform":
                from channelHandler.ucChannelHandler import ucChannel
                tmp_channel: ucChannel = ucChannel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"uc-{tmp_channel.uuid}"
            elif channle_name == "4399com":
                from channelHandler.m4399ChannelHandler import m4399Channel
                tmp_channel: m4399Channel = m4399Channel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"4399-{tmp_channel.uuid}"
            elif channle_name == "360_assistant":
                from channelHandler.qihooChannelHandler import qihooChannel
                tmp_channel: qihooChannel = qihooChannel(tmpData, game_id=game_id)
                tmp_channel.uuid = f"360-{tmp_channel.uuid}"
            else:
                self.logger.error('game={} channel={} stage=import.create 不支持的渠道', game_id, channle_name)
                if on_error:
                    on_error('此渠道暂不支持手动导入，请通过游戏原有登录入口登录；若此渠道出现在可选列表中，请反馈游戏名、渠道名和工具版本')
                if on_complete:
                    on_complete(False)
                return False
        except Exception as error:
            self.logger.error('game={} channel={} stage=import.create 创建渠道登录实例失败\n{}',
                              game_id, channle_name, exception_summary(error))
            if on_error:
                on_error('渠道登录入口未能启动，请刷新后重试；仍失败时反馈日志')
            if on_complete:
                on_complete(False)
            return False

        old_uuid = tmp_channel.uuid
        tmp_channel.uuid = "idv-" + old_uuid.removeprefix("idv-")
        if tmp_channel.name == old_uuid:
            tmp_channel.name = tmp_channel.uuid

        import_finished = False

        def _finish_import(result):
            nonlocal import_finished
            if import_finished:
                return result
            import_finished = True
            if isinstance(result, Exception):
                self._credential_failure(tmp_channel, game_id, 'import.credentials', result, on_error)
                result = False
            elif result is False:
                self._credential_failure(tmp_channel, game_id, 'import.credentials', None, on_error)
            if self._pending_login_channel is tmp_channel:
                self._pending_login_channel = None
            if result is True:
                tmp_channel.last_login_time = time.time()
                self.channels.append(tmp_channel)
                saved = False
                try:
                    self.save_records()
                    saved = True
                    from account_list_policy import AccountListPolicy
                    AccountListPolicy(self).include_logged_in(tmp_channel)
                except Exception as error:
                    if not saved:
                        self.channels.remove(tmp_channel)
                    tmp_channel.report_login_failure(game_id, 'import.selection' if saved else 'import.save',
                        '账号已保存，但游戏内切换选择未更新，请重新勾选保存' if saved else
                        '登录已完成，但账号未保存，请检查账号配置文件及目录权限后重试', error,
                        notify=True, on_error=on_error)
                    result = False
            elif result is None:
                self.logger.info(f"手动导入已取消: {tmp_channel.name}")
            else:
                self.logger.error(f"手动导入失败: {tmp_channel.name}")
                result = False
            if on_complete:
                on_complete(result)
            return result

        def _login_finished(result):
            if result is not True:
                return _finish_import(result)
            try:
                if not tmp_channel.is_token_valid():
                    result = False
                elif self.db_sync:
                    if on_complete:
                        self.db_sync.created(tmp_channel, on_complete=_finish_import)
                        return None
                    result = self.db_sync.created(tmp_channel)
            except Exception as error:
                result = error
            return _finish_import(result)

        if on_complete is not None:
            self._pending_login_channel = tmp_channel
            try:
                if channle_name == "myapp":
                    # 微信登录不使用浏览器，在后台线程运行避免阻塞主线程
                    import threading
                    import app_state
                    def _run_sync():
                        try:
                            self.logger.info("微信登录：开始 request_user_login")
                            success = tmp_channel.request_user_login()
                            self.logger.info(f"微信登录：request_user_login={success}")
                        except Exception as error:
                            success = error
                        # 必须在主线程收尾，因为后续可能涉及 Qt 操作
                        app_state.run_on_main_thread(lambda: _login_finished(success))
                    threading.Thread(target=_run_sync, daemon=True).start()
                elif channle_name == "bilibili_sdk":
                    # B站登录：QR 模式在后台线程阻塞轮询，Web 模式走 Qt 主线程
                    bili_method = login_method if login_method else "qr"
                    if bili_method == "qr":
                        import threading
                        import app_state
                        def _run_bili_qr():
                            try:
                                self.logger.info("B站登录：开始 QR 扫码登录")
                                success = tmp_channel.request_user_login(login_method="qr")
                                self.logger.info(f"B站登录：request_user_login={success}")
                            except Exception as error:
                                success = error
                            app_state.run_on_main_thread(lambda: _login_finished(success))
                        threading.Thread(target=_run_bili_qr, daemon=True).start()
                    else:
                        tmp_channel.request_user_login(
                            on_complete=_login_finished, login_method="web"
                        )
                elif channle_name == "huawei":
                    # 华为登录：QR 扫码在后台线程阻塞轮询，Web 浏览器走 Qt 主线程
                    hua_method = login_method if login_method else "qr"
                    if hua_method == "qr":
                        import threading
                        import app_state
                        def _run_huawei_qr():
                            try:
                                self.logger.info("华为登录：开始扫码登录")
                                success = tmp_channel.request_user_login(login_method="qr")
                                self.logger.info(f"华为登录：request_user_login={success}")
                            except Exception as error:
                                success = error
                            app_state.run_on_main_thread(lambda: _login_finished(success))
                        threading.Thread(target=_run_huawei_qr, daemon=True).start()
                    else:
                        tmp_channel.request_user_login(
                            on_complete=_login_finished, login_method="web"
                        )
                else:
                    tmp_channel.request_user_login(on_complete=_login_finished)
            except Exception as error:
                _finish_import(error)
            return

        try:
            return _login_finished(tmp_channel.request_user_login())
        except Exception as error:
            return _finish_import(error)

    def login(self, uuid: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                data = channel.login()
                self.save_records()
                return data
        return False

    def record_sauth(self, data, success):
        """Login state belongs to Channel, with or without the optional DB plugin."""
        if not isinstance(data, dict) or data.get('login_channel') == 'netease':
            return False
        from channelHandler.channelUtils import getShortGameId
        game = getShortGameId(str(data.get('gameid', '')))
        key = (game, data.get('login_channel'), str(data.get('sdkuid', '')))
        matches = []
        for record in self.channels:
            try:
                if record.sdk_identity(game) == key:
                    matches.append(record)
            except ValueError as error:
                record.report_login_failure(game, 'sauth.match', '已跳过身份字段无效的记录', error, detail=str(error))
                continue
        if not matches:
            return False
        if success:
            from account_list_policy import AccountListPolicy
            policy = AccountListPolicy(self)
            canonical = policy.canonical_uuid(matches[0], game)
            record = self.query_channel(canonical) if canonical else matches[0]
            record.last_login_time = time.time()
            try:
                policy.include_logged_in(record)
            except Exception as error:
                record.report_login_failure(game, 'sauth.selection',
                    '登录成功，但游戏内切换选择未保存', error)
        else:
            for record in matches:
                record.expire_unisdk(game)
        try:
            self.save_records()
        except Exception as error:
            matches[0].report_login_failure(game, 'sauth.save',
                '本次登录状态未能保存；游戏返回结果不变', error)
        if success and self.db_sync:
            self.db_sync.promote_account(record, game)
        return True

    def rename(self, uuid: str, new_name: str):
        if not self.query_channel(uuid):
            return False
        related = self.db_sync.related_uuids(uuid) if self.db_sync else {uuid}
        if self.db_sync:
            self.db_sync.rename_record(uuid, new_name)
        previous = [(record, record.name) for record in self.channels if record.uuid in related]
        try:
            for record, _ in previous:
                record.name = new_name
            self.save_records()
        except Exception:
            for record, name in previous:
                record.name = name
            raise
        return True

    def delete(self, uuid: str):
        """Delete by SDK identity; preserve the Channel for retry if saving fails."""
        if not self.query_channel(uuid):
            return False
        related = self.db_sync.related_uuids(uuid) if self.db_sync else {uuid}
        if self.db_sync:
            self.db_sync.delete_record(uuid)
        previous = self.channels[:]
        self.channels[:] = [record for record in self.channels if record.uuid not in related]
        try:
            self.save_records()
        except Exception:
            self.channels[:] = previous
            raise
        for key in related:
            self._cleanup_weblogin_data(key)
        return True

    def _cleanup_weblogin_data(self, uuid: str):
        """清理 weblogin 账号的 profile 和 cache 文件夹
        
        weblogin 账号（使用 WebBrowser 的渠道）：
        - phone-xxx (OPPO)
        - xiaomi_app-xxx (小米)
        - huawei-xxx (华为)
        - nearme_vivo-xxx (vivo)
        - honor-xxx (荣耀)
        - uc-xxx (九游)
        
        不包括微信 (wx-xxx)，微信使用扫码登录，不产生 profile。
        """
        import shutil
        
        # 判断是否是 weblogin 账号（使用 WebBrowser 的渠道）
        is_weblogin = (
            uuid.removeprefix("idv-").startswith("phone-") or
            uuid.removeprefix("idv-").startswith("xiaomi_app-") or
            uuid.removeprefix("idv-").startswith("huawei-") or
            uuid.removeprefix("idv-").startswith("nearme_vivo-") or
            uuid.removeprefix("idv-").startswith("honor-") or
            uuid.removeprefix("idv-").startswith("uc-")
        )
        
        if not is_weblogin:
            return
        
        # 删除 profile 文件夹
        profile_base = genv.get("GLOB_LOGIN_PROFILE_PATH")
        if profile_base:
            profile_path = os.path.join(profile_base, uuid)
            if os.path.exists(profile_path):
                try:
                    shutil.rmtree(profile_path)
                    self.logger.info(f"已删除 weblogin profile: {profile_path}")
                except Exception as e:
                    self.logger.warning(f"删除 profile 文件夹失败: {profile_path}, 错误: {e}")
        
        # 删除 cache 文件夹
        cache_base = genv.get("GLOB_LOGIN_CACHE_PATH")
        if cache_base:
            cache_path = os.path.join(cache_base, uuid)
            if os.path.exists(cache_path):
                try:
                    shutil.rmtree(cache_path)
                    self.logger.info(f"已删除 weblogin cache: {cache_path}")
                except Exception as e:
                    self.logger.warning(f"删除 cache 文件夹失败: {cache_path}, 错误: {e}")

    def cleanup_orphaned_weblogin_profiles(self):
        """v6.0.0 新增：清理孤立的 weblogin profile/cache 文件夹
        
        逻辑：
        1. 检查是否有 weblogin 账号，没有则跳过
        2. 遍历 profile/ 和 cache/ 文件夹，删除不在 channels 中的孤立文件夹
        
        由 main.py 在首次启动时调用。
        """
        # 检查是否有 weblogin 账号
        has_weblogin = any(
            ch.uuid.removeprefix("idv-").startswith("phone-") or
            ch.uuid.removeprefix("idv-").startswith("xiaomi_app-") or
            ch.uuid.removeprefix("idv-").startswith("huawei-") or
            ch.uuid.removeprefix("idv-").startswith("nearme_vivo-") or
            ch.uuid.removeprefix("idv-").startswith("honor-") or
            ch.uuid.removeprefix("idv-").startswith("uc-")
            for ch in self.channels
        )
        
        if not has_weblogin:
            return
        
        # 收集所有合法的 uuid
        valid_uuids = {ch.uuid for ch in self.channels}
        
        import shutil
        
        # 清理孤立的 profile 文件夹
        profile_base = genv.get("GLOB_LOGIN_PROFILE_PATH")
        if profile_base and os.path.exists(profile_base):
            try:
                for folder_name in os.listdir(profile_base):
                    folder_path = os.path.join(profile_base, folder_name)
                    if not os.path.isdir(folder_path):
                        continue
                    
                    # 只清理 weblogin 类型的文件夹
                    is_weblogin_folder = (
                        folder_name.removeprefix("idv-").startswith("phone-") or
                        folder_name.removeprefix("idv-").startswith("xiaomi_app-") or
                        folder_name.removeprefix("idv-").startswith("huawei-") or
                        folder_name.removeprefix("idv-").startswith("nearme_vivo-") or
                        folder_name.removeprefix("idv-").startswith("honor-") or
                        folder_name.removeprefix("idv-").startswith("uc-")
                    )
                    
                    if is_weblogin_folder and folder_name not in valid_uuids:
                        try:
                            shutil.rmtree(folder_path)
                            self.logger.info(f"清理孤立 profile: {folder_path}")
                        except Exception as e:
                            self.logger.warning(f"清理孤立 profile 失败: {folder_path}, 错误: {e}")
            except Exception as e:
                self.logger.warning(f"扫描 profile 文件夹失败: {e}")
        
        # 清理孤立的 cache 文件夹
        cache_base = genv.get("GLOB_LOGIN_CACHE_PATH")
        if cache_base and os.path.exists(cache_base):
            try:
                for folder_name in os.listdir(cache_base):
                    folder_path = os.path.join(cache_base, folder_name)
                    if not os.path.isdir(folder_path):
                        continue
                    
                    # 只清理 weblogin 类型的文件夹
                    is_weblogin_folder = (
                        folder_name.removeprefix("idv-").startswith("phone-") or
                        folder_name.removeprefix("idv-").startswith("xiaomi_app-") or
                        folder_name.removeprefix("idv-").startswith("huawei-") or
                        folder_name.removeprefix("idv-").startswith("nearme_vivo-") or
                        folder_name.removeprefix("idv-").startswith("honor-") or
                        folder_name.removeprefix("idv-").startswith("uc-")
                    )
                    
                    if is_weblogin_folder and folder_name not in valid_uuids:
                        try:
                            shutil.rmtree(folder_path)
                            self.logger.info(f"清理孤立 cache: {folder_path}")
                        except Exception as e:
                            self.logger.warning(f"清理孤立 cache 失败: {folder_path}, 错误: {e}")
            except Exception as e:
                self.logger.warning(f"扫描 cache 文件夹失败: {e}")
        
        self.logger.info("孤立 weblogin profile/cache 清理完成")

    def query_channel(self, uuid: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                return channel
        return None

    @staticmethod
    def _credential_failure(record, game_id, stage, error, on_error=None):
        if isinstance(error, (TimeoutError, requests.Timeout)):
            message = '获取登录数据超时，请检查网络后重试'
        elif isinstance(error, requests.RequestException):
            message = '渠道请求未完成，请检查网络后重试；仍失败时反馈日志'
        elif isinstance(error, OSError):
            message = '登录数据未能读取或保存，请检查账号配置文件及目录权限后重试'
        elif isinstance(error, ValueError):
            message = '渠道登录数据校验失败，请重新登录；仍失败时反馈日志'
        elif error is None:
            message = '渠道未完成登录，请重试；仍失败时反馈日志'
        else:
            message = '获取登录数据失败，请重试；仍失败时反馈日志'
        record.report_login_failure(game_id, stage, message, error, notify=True, on_error=on_error)

    def simulate_confirm(self, channel: channel, scanner_uuid: str, game_id: str, on_complete=None, on_error=None):
        def _do_confirm(channel_data):
            result = False
            try:
                if isinstance(channel_data, Exception):
                    self._credential_failure(channel, game_id, 'switch.credentials', channel_data, on_error)
                elif channel_data is None:
                    result = None
                elif channel_data is False:
                    self._credential_failure(channel, game_id, 'switch.credentials', None, on_error)
                else:
                    body_data = dict(channel_data, uuid=scanner_uuid, game_id=game_id)
                    body = "&".join([f"{k}={v}" for k, v in body_data.items()])
                    response = requests.post(
                        "https://service.mkey.163.com/mpay/api/qrcode/confirm_login", data=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        verify=should_verify_ssl())
                    response.raise_for_status()
                    result = response.json()
            except Exception as error:
                channel.report_login_failure(game_id, 'switch.confirm',
                    '游戏确认登录失败，请重新扫码；仍失败时反馈日志', error, notify=True, on_error=on_error)
            if result is None or result is False:
                genv.set("CHANNEL_ACCOUNT_SELECTED", "")
            if on_complete:
                on_complete(result)
            return result

        if on_complete is not None:
            channel.request_unisdk(game_id, on_complete=_do_confirm)
            return None

        try:
            channel_data = channel.request_unisdk(game_id)
        except Exception as error:
            channel_data = error
        return _do_confirm(channel_data)

    def simulate_scan(self, uuid: str | channel, scanner_uuid: str, game_id: str, on_complete=None, on_error=None):
        # A hosted MPay callback can supply credentials without a tool record.
        records = self.channels if isinstance(uuid, str) else (uuid,)
        for channel in records:
            if channel is uuid or channel.uuid == uuid:
                data = {
                    "uuid": scanner_uuid,
                    "login_channel": channel.channel_name,
                    "app_channel": channel.channel_name,
                    "pay_channel": channel.channel_name,
                    "game_id": game_id,
                    "gv": "157",
                    "gvn": "1.5.80",
                    "cv": "a1.5.0",
                }
                try:
                    if scanner_uuid=="Kinich":
                        def _ready(channel_data):
                            if isinstance(channel_data, Exception):
                                self._credential_failure(channel, game_id, 'refresh.credentials', channel_data, on_error)
                                result = False
                            else:
                                result = channel_data if channel_data is None or channel_data is False else True
                                if result is False:
                                    self._credential_failure(channel, game_id, 'refresh.credentials', None, on_error)
                            if result:
                                saved = False
                                try:
                                    channel.last_login_time = time.time()
                                    self.save_records()
                                    saved = True
                                    if self.db_sync:
                                        self.db_sync.refresh_record(channel, game_id)
                                except Exception as error:
                                    channel.report_login_failure(game_id, 'refresh.database' if saved else 'refresh.save',
                                        '登录已刷新，但游戏账号库未全部更新，请关闭游戏后重试' if saved else
                                        '登录已刷新，但账号未保存，请检查账号配置文件及目录权限后重试', error, notify=True, on_error=on_error)
                                    result = False
                            if not result:
                                genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                            if on_complete:
                                on_complete(result)
                            return result

                        if on_complete is not None and channel.record_source == "manual":
                            channel.request_unisdk(game_id, on_complete=_ready, force=True)
                            return None
                        try:
                            result = channel.request_unisdk(game_id, force=True)
                        except Exception as error:
                            result = error
                        return _ready(result)
                    r = requests.get(
                        "https://service.mkey.163.com/mpay/api/qrcode/scan",
                        params=data,
                        verify=should_verify_ssl()
                    )
                    
                    r.raise_for_status()
                    resp=r.json()
                    if resp.get("code",-1)==1424:
                        data["game_id"]=resp["game"]["id"]
                        self.logger.info(f"发烧平台游戏id:{data['game_id']}")
                        r=requests.get(
                            "https://service.mkey.163.com/mpay/api/qrcode/scan",
                            params=data,
                            verify=should_verify_ssl()
                        )
                    if r.status_code == 200:
                        return self.simulate_confirm(channel, scanner_uuid, data["game_id"], on_complete=on_complete, on_error=on_error)
                    else:
                        r.raise_for_status()
                except Exception as error:
                    channel.report_login_failure(game_id, 'switch.scan',
                        '游戏扫码请求失败，请重新扫码；仍失败时反馈日志', error, notify=True, on_error=on_error)
                    genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                    if on_complete:
                        on_complete(False)
                    return False
        genv.set("CHANNEL_ACCOUNT_SELECTED", "")
        if on_complete:
            on_complete(None)
        return None
