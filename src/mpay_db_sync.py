"""Account lifecycle synchronization. All IO stays in this open-source host."""
from __future__ import annotations

import base64
from concurrent.futures import Future
from contextlib import contextmanager
import ctypes
import json
import os
import copy
import re
from pathlib import Path
import threading
import time

from channelHandler.channelUtils import getShortGameId, cmp_game_id
from channel_cache import credential_expires_at, packet_identity, exception_summary
from envmgr import genv
from mpay_wasm import MpayWasm, MAX_DB_BYTES

SQLITE_PENDING_BYTE = 0x40000000


def log_failure(logger, message: str, error: Exception):
    logger.debug('{}\n{}', message, exception_summary(error))


def machine_identifier() -> str:
    """Match MPay's primary disk serial, falling back to MachineGuid, via OS IO.

    MPay hashes this inside its User::Save token transform. The hash/transform
    belongs to Wasm; the host only fetches the OS identifier.
    """
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(r"\\.\PhysicalDrive0", 0, 3, None, 3, 0, None)
    if handle != wintypes.HANDLE(-1).value:
        try:
            query = (ctypes.c_uint32 * 3)(0, 0, 0)  # StorageDeviceProperty / PropertyStandardQuery
            output = ctypes.create_string_buffer(4096)
            count = wintypes.DWORD()
            if kernel.DeviceIoControl(handle, 0x2D1400, query, ctypes.sizeof(query), output,
                                      len(output), ctypes.byref(count), None):
                offset = int.from_bytes(output.raw[24:28], "little")
                if 0 < offset < count.value:
                    serial = output.raw[offset:count.value].split(b"\0", 1)[0].decode("ascii").strip()
                    if serial:
                        return serial
        finally:
            kernel.CloseHandle(handle)
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        return winreg.QueryValueEx(key, "MachineGuid")[0]


@contextmanager
def locked_database(path: Path):
    """Take SQLite's PENDING + RESERVED + SHARED range on the original inode.

    Native SQLite readers/writers cannot acquire their conflicting byte locks.
    Keep the same file rather than replacing an inode under a cached MPay handle.
    """
    with path.open("r+b", buffering=0) as file:
        file.seek(SQLITE_PENDING_BYTE)
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 512)
        try:
            file.seek(0)
            yield file
        finally:
            file.seek(SQLITE_PENDING_BYTE)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 512)


def credentials_from_packet(packet: dict, game_id: str) -> tuple[str, dict]:
    game, channel, sdkuid = packet_identity(packet)
    if not cmp_game_id(game, game_id) or channel != packet["login_channel"] or not sdkuid:
        raise ValueError("Refreshed credentials belong to a different game/account")
    if not packet.get("token"):
        raise ValueError("Refreshed credentials contain an empty token")
    return sdkuid, {
        "token": packet["token"],
        "pc_ext_info": {
            "extra_unisdk_data": packet["extra_unisdk_data"],
            "from_game_id": getShortGameId(game_id), "src_client_type": 1,
            "src_app_channel": packet["app_channel"], "src_pay_channel": packet["pay_channel"],
            "src_jf_game_id": packet["jf_game_id"], "src_sdk_version": packet["sdk_version"],
            "src_udid": packet["udid"], "is_remember": True,
        },
    }


def packet_from_credentials(channel, credentials):
    """Keep one captured game packet on its existing Channel, not a DB-write copy."""
    ext = credentials.get('pc_ext_info') or {}
    return {'token': credentials.get('token', ''),
            'login_channel': channel, 'udid': ext.get('src_udid', ''),
            'app_channel': ext.get('src_app_channel', channel),
            'pay_channel': ext.get('src_pay_channel', channel),
            'sdk_version': ext.get('src_sdk_version', ''),
            'jf_game_id': ext.get('from_game_id') or ext.get('src_jf_game_id', ''),
            'extra_data': '', 'extra_unisdk_data': ext.get('extra_unisdk_data', '{}'),
            'gv': '157', 'gvn': '1.5.80', 'cv': 'a1.5.0'}


class MpayDBSync:
    MENUS = {'help': '【点我再点登录获取帮助】',
             'expired': '【点我再点击登录刷新过期记录】'}

    @staticmethod
    def _menu_key(game, channel, role):
        # A menu belongs to this channel, never to whichever account was first.
        return game, channel, f'idv-login:menu:{game}:{channel}:{role}'

    @classmethod
    def _menu_role(cls, key):
        return next((role for role in cls.MENUS if key == cls._menu_key(key[0], key[1], role)), None)

    def __init__(self, manager, artifact, game_helper=None):
        self.manager, self.logger = manager, manager.logger
        self.game_helper = game_helper
        self.wasm = MpayWasm(artifact)
        self.root = Path(os.environ.get('APPDATA', '')) / 'Netease' / 'Mpay'
        self.identity = machine_identifier() if os.name == 'nt' else ''
        self.lock = threading.RLock()
        self.stopping = False
        # Successful writes only: (path, SDK identity, created by this process).
        # No DB addresses, credentials, display-name copies or Channel bindings.
        self.projected = set()

    def paths(self, game):
        return sorted(self.root.glob(f'*-g-{getShortGameId(game)}-64-mpay.db'))

    def game_ids(self):
        return sorted({m.group(1) for p in self.root.glob('*-g-*-64-mpay.db')
                       if (m := re.match(r'.*-g-(.+)-64-mpay\.db$', p.name))})

    @staticmethod
    def _key(item):
        return item['game_id'], item['login_channel'], item['sdkuid']

    @staticmethod
    def _args(key):
        return dict(zip(('game_id', 'login_channel', 'sdkuid'), key))

    def operate(self, path, operation, **args):
        with self.lock, locked_database(path) as file:
            original = file.read(MAX_DB_BYTES + 1)
            if len(original) > MAX_DB_BYTES:
                raise ValueError('MPay database exceeds supported size')
            result = self.wasm.invoke(operation, original, **args)
            if operation == 'list_accounts':
                response = json.loads(result)
                for error in response['errors']:
                    self.logger.debug('[mpay-db] {} 渠道记录 #{}：{}；跳过此记录',
                                      path.name, error['record_index'], error['reason'])
                return response
            created = False
            if operation == 'put_account':
                created, result = result
            try:
                file.seek(0)
                file.write(result)
                file.truncate(len(result))
                file.flush()
                os.fsync(file.fileno())
            except OSError:
                file.seek(0)
                file.write(original)
                file.truncate(len(original))
                file.flush()
                os.fsync(file.fileno())
                raise
            return created

    def _remember_put(self, path, key, created):
        with self.lock:
            previous = {(p, k, c) for p, k, c in self.projected if p == path and k == key}
            self.projected.difference_update(previous)
            self.projected.add((path, key, created or any(c for _, _, c in previous)))

    def _delete(self, path, key):
        self.operate(path, 'delete_account', **self._args(key))
        self.projected = {(p, k, c) for p, k, c in self.projected if (p, k) != (path, key)}

    @staticmethod
    def _scan_packet(record):
        user = record.user_info
        if not user.get('token'):
            return None
        return packet_from_credentials(record.channel_name, {
            'token': user['token'], 'pc_ext_info': user.get('pc_ext_info') or record.ext_info})

    def _packet(self, record, game):
        if record.record_source == 'manual':
            return record.cached_unisdk(game)
        if self.is_expired(record.uuid, game):
            return None
        return self._scan_packet(record)

    @staticmethod
    def _target(record, game):
        return record.sdk_identity(game)

    def _record_error(self, record, game, error, *, stage='projection', message=None):
        record.report_login_failure(game, stage, message or '此账号本次同步已跳过', error)

    def _credentials(self, record, game, packet):
        if record.record_source == 'scan':
            return {'token': record.user_info['token'],
                    'pc_ext_info': copy.deepcopy(record.user_info.get('pc_ext_info') or record.ext_info)}
        return credentials_from_packet(packet, game)[1]

    def is_expired(self, uuid, game):
        record = self.manager.query_channel(uuid)
        return bool(record and record.is_login_expired(game))

    @staticmethod
    def _renewal_state(record, game):
        entry = record.unisdk_cache.get(getShortGameId(game))
        return id(entry), entry.get('expires_at') if entry else record.expires_at, record.current_sdkuid()

    def imported_uuids(self, game):
        identities = {key for path, key, _ in self.projected if path in self.paths(game)}
        result = set()
        for record in self.manager.channels:
            try:
                if self._target(record, game) in identities:
                    result.add(record.uuid)
            except ValueError as error:
                self._record_error(record, game, error, stage='status.identity')
                continue
        return result

    def native_account_count(self, game):
        try:
            count = 0
            for path in self.paths(game):
                response = self.operate(path, 'list_accounts')
                present = {self._key(item) for item in response['accounts']}
                added = {key for p, key, created in self.projected if p == path and created}
                menus = {key for key in present if self._menu_role(key)}
                count += response['total_count'] - len(present & (added | menus))
            return count
        except Exception as error:
            log_failure(self.logger, '[mpay-db] 原生账号数量暂不可用', error)
            return None

    def refresh_startup(self):
        from account_list_policy import AccountListPolicy
        self.reconcile_game_changes()
        selected = AccountListPolicy(self.manager).select()
        for game in self.game_ids():
            if game not in selected:
                self._clear_projection(game)
        for game, records in selected.items():
            try:
                self._prepare_selected({game: records})
                self._project_game(game, records)
            except Exception as error:
                log_failure(self.logger, f'[mpay-db] game={game} stage=refresh 本游戏更新失败，继续其余游戏', error)

    def _clear_projection(self, game):
        for path, key, created in tuple(self.projected):
            if key[0] != getShortGameId(game):
                continue
            try:
                if created and path.exists():
                    self._delete(path, key)
                else:
                    self.projected.discard((path, key, created))
            except Exception as error:
                log_failure(self.logger, f'[mpay-db] game={game} path={path.name} stage=projection.clear 临时账号清理失败，保留本次结果供退出重试', error)

    @staticmethod
    def _clone(record):
        record.before_save()
        data = {}
        for key, value in vars(record).items():
            try:
                data[key] = json.loads(json.dumps(value))
            except (TypeError, ValueError):
                pass
        clone = type(record).from_dict(data)
        clone.restore_common(data)
        for key in ('uuid', 'name', 'last_login_time', 'game_id', 'crossGames', 'record_source'):
            setattr(clone, key, getattr(record, key))
        return clone

    def _prepare_selected(self, selected):
        from prefetch_context import prefetch_scope
        jobs = []
        for game, records in selected.items():
            for record in records:
                try:
                    if self._packet(record, game):
                        continue
                    if record.record_source != 'manual':
                        continue
                    if record.channel_name == 'oppo':
                        accounts = ((record.oppo_open_account or {}).get('gamesdk_latest_role') or {}).get('accounts') or []
                        chosen = str(record.chosen_account_id or '')
                        if len(accounts) > 1 and not any(str(item.get('account_id') or '').strip() == chosen for item in accounts):
                            continue  # Known to need a user choice; do not start background renewal.
                    future, started = Future(), time.monotonic()
                    initial = self._renewal_state(record, game)
                    with prefetch_scope(started + 5):
                        clone = self._clone(record)
                except Exception as error:
                    self._record_error(record, game, error, stage='renewal.prepare')
                    continue
                def run(future=future, clone=clone, game=game, started=started):
                    try:
                        with prefetch_scope(started + 5):
                            packet = clone.request_unisdk(game, interactive=False)
                            future.set_result((clone, packet, time.monotonic()))
                    except BaseException as error:
                        future.set_exception(error)
                threading.Thread(target=run, daemon=True, name='mpay-renew').start()
                jobs.append((record, game, future, started, initial))
        for record, game, future, started, initial in jobs:
            try:
                clone, packet, finished = future.result(timeout=max(0, 5 - (time.monotonic() - started)))
                if finished > started + 5 or packet is None or packet is False:
                    continue  # Needs user action, cancellation or timeout: not a program error.
                with self.lock:
                    current = self._renewal_state(record, game)
                    if self.stopping or self.manager.query_channel(record.uuid) is not record or current != initial:
                        continue
                    preserved = {key: getattr(record, key) for key in ('uuid', 'name', 'last_login_time', 'game_id', 'crossGames', 'record_source')}
                    previous = record.__dict__.copy()
                    cache = dict(record.unisdk_cache)
                    renewed = clone.unisdk_cache.get(getShortGameId(game))
                    record.__dict__.update(clone.__dict__)
                    record.__dict__.update(preserved)
                    record.unisdk_cache = cache
                    if renewed:
                        record.unisdk_cache[getShortGameId(game)] = renewed
                    try:
                        with prefetch_scope(started + 5):
                            self.manager.save_records()
                    except Exception as error:
                        record.__dict__.clear()
                        record.__dict__.update(previous)
                        self._record_error(record, game, error, stage='renewal.save')
            except Exception as error:
                self._record_error(record, game, error, stage='renewal.credentials')

    def promote_account(self, record, game):
        """Move a successfully logged-in SDK account; Channel owns login state."""
        from account_list_policy import AccountListPolicy
        with self.lock:
            if self.stopping:
                return
            key = self._target(record, game)
            if key is None:
                return
            name = AccountListPolicy(self.manager).account_label(record)
            for path, projected, _ in tuple(self.projected):
                if projected != key:
                    continue
                try:
                    self.operate(path, 'put_account', **self._args(key), name=name, position='head')
                except Exception as error:
                    self._record_error(record, game, error, stage=f'login.promote path={path.name}',
                        message='登录成功，但游戏账号列表未能置顶；可关闭游戏后重试同步')

    def created(self, record, on_complete=None):
        def ready(packet):
            result = packet if packet is None or packet is False or isinstance(packet, Exception) else not self.stopping
            if on_complete:
                import app_state
                app_state.run_on_main_thread(lambda: on_complete(result))
            return result
        if self.stopping:
            return ready(False)
        if on_complete:
            record.request_unisdk(record.game_id, on_complete=ready, interactive=True)
            return None
        return ready(record.request_unisdk(record.game_id, interactive=True))

    @staticmethod
    def _placeholder(key):
        game, channel, sdkuid = key
        auth = {'gameid': game, 'login_channel': channel, 'sdkuid': sdkuid, 'sessionid': ''}
        extra = json.dumps({'SAUTH_JSON': base64.b64encode(json.dumps(auth).encode()).decode()})
        return {'token': '', 'pc_ext_info': {'extra_unisdk_data': extra, 'from_game_id': game}}

    @staticmethod
    def _create(name, channel):
        return {'login_channel': channel, 'login_type': 0,
                '__user_login_method': 17, '__user_platform_selected': '',
                '__user_platform_selected_second': '', '__user_platform_selected_third': '',
                '__user_typed_username': '', 'client_username': name,
                'display_username': name, 'nickname': '', 'avatar': '',
                'ext_access_token': '', 'need_mask': False, 'pc_ext_info': {},
                'related_login_status': 0, 'mask_related_mobile': ''}

    def refresh_before_game(self, game_id):
        from account_list_policy import AccountListPolicy
        game = getShortGameId(game_id)
        policy = AccountListPolicy(self.manager)
        self.reconcile_game_changes()
        selected = policy.select([game])
        if game not in selected:
            self._clear_projection(game)
            return
        self._prepare_selected(selected)
        self._project_game(game, selected[game])

    def _write_record(self, path, record, game, position):
        from account_list_policy import AccountListPolicy
        key = self._target(record, game)
        if key is None:
            return None
        packet = self._packet(record, game)
        name = AccountListPolicy(self.manager).account_label(record, expired=self.is_expired(record.uuid, game))
        if not packet:
            if any(p == path and identity == key for p, identity, _ in self.projected):
                self.operate(path, 'rename_account', **self._args(key), name=name)
            return None  # Preparation already reported this record; preserve existing credentials.
        created = self.operate(path, 'put_account', **self._args(key), name=name,
            position=position, machine_identifier=self.identity,
            credentials=self._credentials(record, game, packet), create=self._create(name, key[1]))
        self._remember_put(path, key, created)
        return key

    def refresh_record(self, record, game_id):
        """An explicit successful user refresh updates only an opted-in SDK account."""
        from account_list_policy import AccountListPolicy
        game = getShortGameId(game_id)
        key = self._target(record, game)
        if key is None:
            raise ValueError('field=unisdk_cache.packet: missing after successful refresh')
        selected = AccountListPolicy(self.manager).select([game]).get(game, [])
        allowed = False
        for candidate in selected:
            try:
                if candidate is record or self._target(candidate, game) == key:
                    allowed = True
                    break
            except ValueError as error:
                self._record_error(candidate, game, error, stage='refresh.selection')
                continue
        if not allowed:
            return
        failure = None
        latest = max(selected, key=lambda item: item.last_login_time, default=None)
        with self.lock:
            if self.stopping:
                return
            for path in self.paths(game):
                try:
                    if self._write_record(path, record, game,
                            'head' if record is latest and record.last_login_time else 'tail') is None:
                        raise ValueError('field=unisdk_cache.packet: unavailable after refresh')
                    if not any(self.is_expired(item.uuid, game) for item in selected):
                        for menu_path, menu_key, _ in tuple(self.projected):
                            if menu_path == path and menu_key[0] == game and self._menu_role(menu_key) == 'expired':
                                self._delete(path, menu_key)
                except Exception as error:
                    self._record_error(record, game, error, stage=f'refresh.database path={path.name}')
                    failure = failure or error
        if failure:
            raise RuntimeError('登录凭据已保存，但部分游戏账号库未刷新') from failure

    def _project_game(self, game_id, records):
        with self.lock:
            if self.stopping:
                return
            game = getShortGameId(game_id)
            latest = max(records, key=lambda r: r.last_login_time, default=None)
            for path in self.paths(game):
                desired, written = set(), set()
                for record in records:
                    try:
                        key = self._target(record, game)
                        if key is None:
                            continue  # This game has not acquired a packet; no identity to write.
                        if key in desired:
                            continue  # Renewal may reveal two saved entries for the same SDK account.
                        desired.add(key)
                        if self._write_record(path, record, game,
                                'head' if record is latest and record.last_login_time else 'tail') is not None:
                            written.add(key)
                    except Exception as error:
                        self._record_error(record, game, error,
                            stage=f'projection.write path={path.name}',
                            message='此账号未写入游戏账号库，请关闭游戏后重试；仍失败时反馈日志')
                menu_sources = written | {key for p, key, _ in self.projected
                                          if p == path and key in desired and not self._menu_role(key)}
                if menu_sources:
                    channel = min(key[1] for key in menu_sources)
                    roles = ['help'] + (['expired'] if any(self.is_expired(r.uuid, game) for r in records) else [])
                    for role in roles:
                        name = self.MENUS[role]
                        key = self._menu_key(game, channel, role)
                        desired.add(key)
                        try:
                            created = self.operate(path, 'put_account', **self._args(key), name=name,
                                position='tail', machine_identifier=self.identity,
                                create=self._create(name, channel), credentials=self._placeholder(key))
                            self._remember_put(path, key, created)
                            desired.add(key)
                        except Exception as error:
                            log_failure(self.logger, f'[mpay-db] game={game} channel={channel} role={role} path={path.name} stage=menu.write 提示菜单写入失败', error)
                for previous_path, key, created in tuple(self.projected):
                    if previous_path != path or key in desired:
                        continue
                    try:
                        if created:
                            self._delete(path, key)
                        else:
                            self.projected.discard((path, key, created))
                    except Exception as error:
                        log_failure(self.logger, f'[mpay-db] game={game} path={path.name} stage=projection.cleanup 临时旧账号清理失败，保留退出重试', error)

    def intercept_auth(self, data):
        if not isinstance(data, dict) or data.get('login_channel') == 'netease':
            return False
        game = getShortGameId(str(data.get('gameid', '')))
        key = (game, data.get('login_channel'), str(data.get('sdkuid', '')))
        if not any(projected == key for _, projected, _ in self.projected):
            return False
        role = self._menu_role(key)
        expired = False
        if role is None:
            for record in self.manager.channels:
                try:
                    if self._target(record, game) == key and self.is_expired(record.uuid, game):
                        expired = True
                        break
                except ValueError as error:
                    self._record_error(record, game, error, stage='menu.identity')
                    continue
        if role is None and not expired:
            return False
        import app_state
        if role == 'help':
            import webbrowser
            cloud = app_state.cloud_res
            url = cloud.get_by_game_id_and_key(game, 'account_help_url') if cloud else None
            app_state.run_on_main_thread(lambda: webbrowser.open(url or 'https://kkeygenn.feishu.cn/wiki/J0V4wbm3Bi5LOVkEN7wcvwSEn0e'))
        else:
            app_state.run_on_main_thread(lambda: app_state.ui_mgr.open_for_game(game, 'accounts') if app_state.ui_mgr else None)
        return True

    def reconcile_game_changes(self):
        missing = set()
        for path in {path for path, _, _ in self.projected}:
            if not path.exists():
                continue
            try:
                response = self.operate(path, 'list_accounts')
                rows = {self._key(item) for item in response['accounts']}
                blocked = {self._key(error) for error in response['errors']
                           if error['reason'] == 'duplicate_sdk_identity'}
                for _, key, _ in tuple(item for item in self.projected if item[0] == path):
                    if self._menu_role(key) or key in blocked:
                        continue
                    for record in self.manager.channels:
                        try:
                            if self._target(record, key[0]) != key:
                                continue
                        except ValueError as error:
                            self._record_error(record, key[0], error, stage='reconcile.identity')
                            continue
                        if key not in rows:
                            missing.add(record.uuid)
                            self.projected = {(p, k, c) for p, k, c in self.projected if (p, k) != (path, key)}
            except Exception as error:
                log_failure(self.logger, f'[mpay-db] path={path.name} stage=reconcile.read 无法检查游戏删除', error)
        for uuid in missing:
            if self.manager.query_channel(uuid) is not None:
                try:
                    self.manager.delete(uuid)
                except Exception as error:
                    log_failure(self.logger, f'[mpay-db] account={uuid} stage=reconcile.delete 单个账号删除同步失败，继续其余账号', error)

    def related_uuids(self, uuid):
        source = self.manager.query_channel(uuid)
        if source is None:
            return {uuid}
        games = {key[0] for _, key, _ in self.projected}
        related = {uuid}
        for game in games:
            try:
                key = self._target(source, game)
                if key is None:
                    continue
            except ValueError as error:
                self._record_error(source, game, error, stage='related.source')
                continue
            for record in self.manager.channels:
                try:
                    if self._target(record, game) == key:
                        related.add(record.uuid)
                except ValueError as error:
                    self._record_error(record, game, error, stage='related.identity')
                    continue
        return related

    def delete_record(self, uuid):
        record = self.manager.query_channel(uuid)
        failure = None
        with self.lock:
            for path, key, _ in tuple(self.projected):
                try:
                    if self._target(record, key[0]) != key:
                        continue
                except ValueError as error:
                    self._record_error(record, key[0], error, stage=f'delete.identity path={path.name}')
                    failure = failure or error
                    continue
                try:
                    if path.exists():
                        self._delete(path, key)
                    else:
                        self.projected = {(p, k, c) for p, k, c in self.projected if (p, k) != (path, key)}
                except Exception as error:
                    self._record_error(record, key[0], error, stage=f'delete.database path={path.name}')
                    failure = failure or error
        if failure:
            raise failure

    def rename_record(self, uuid, name):
        from account_list_policy import AccountListPolicy
        record = self.manager.query_channel(uuid)
        failure = None
        for path, key, _ in tuple(self.projected):
            try:
                if not path.exists() or self._target(record, key[0]) != key:
                    continue
                renamed = copy.copy(record)
                renamed.name = name
                label = AccountListPolicy(self.manager).account_label(renamed, expired=self.is_expired(uuid, key[0]))
                self.operate(path, 'rename_account', **self._args(key), name=label)
            except Exception as error:
                self._record_error(record, key[0], error, stage=f'rename.database path={path.name}')
                failure = failure or error
        if failure:
            raise RuntimeError('部分账号库改名失败，未报告同步成功') from failure

    def shutdown(self):
        with self.lock:
            self.stopping = True
            self.reconcile_game_changes()
            for game in {key[0] for _, key, _ in self.projected}:
                self._clear_projection(game)
