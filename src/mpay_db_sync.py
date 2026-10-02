"""Account lifecycle synchronization. All IO stays in this open-source host."""
from __future__ import annotations

import base64
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
import ctypes
import json
import os
import copy
import hashlib
import re
from pathlib import Path
import threading
import time
import traceback
from urllib.parse import unquote

from channelHandler.channelUtils import getShortGameId, cmp_game_id
from envmgr import genv
from mpay_wasm import MpayWasm, MAX_DB_BYTES
from secure_write import write_file_restricted

SQLITE_PENDING_BYTE = 0x40000000


def credential_expires_at(record):
    """Only consult local credential fields; never invoke a login accessor."""
    attrs = vars(record)
    channel = record.channel_name
    value = None
    if channel in ('myapp', 'myapp_qq'):
        session = attrs.get('session')
        session = session if isinstance(session, dict) else vars(session) if session else {}
        duration = session.get('atk_expire')
        if duration:
            value = attrs.get('token_issued_at', record.last_login_time) + int(duration)
    elif channel in ('honor', 'honor_sdk'):
        login = attrs.get('honorLogin')
        value = getattr(login, 'expiredTime', None)
    elif channel == 'bilibili_sdk':
        data = attrs.get('loginResp') or {}
        value = (data.get('data') or data).get('expires')
    elif channel == 'uc_platform':
        value = attrs.get('sid_expire_time')
    elif channel in ('qihoo', '360_assistant'):
        value = attrs.get('token_expire_time')
    try:
        return int(value) if value is not None and int(value) > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def log_failure(logger, message: str, error: Exception):
    # Loguru's diagnose=True dumps locals; renewal frames contain credentials.
    # Keep stack locations and exception type, without frame values or response bodies.
    logger.error("{} [{}]\n{}", message, type(error).__name__,
                 "".join(traceback.format_tb(error.__traceback__)))


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
    extra = json.loads(packet["extra_unisdk_data"])
    sauth = json.loads(base64.b64decode(unquote(extra["SAUTH_JSON"])))
    if not cmp_game_id(sauth["gameid"], game_id):
        raise ValueError("Refreshed credentials belong to a different game")
    if str(sauth["sdkuid"]) != str(packet["user_id"]) or sauth["login_channel"] != packet["login_channel"]:
        raise ValueError("Refreshed credentials have inconsistent channel/UID")
    if not packet["user_id"] or not packet["token"] or not sauth["sessionid"]:
        raise ValueError("Refreshed credentials contain an empty session")
    return str(packet["user_id"]), {
        "token": packet["token"],
        "pc_ext_info": {
            "extra_unisdk_data": packet["extra_unisdk_data"],
            "from_game_id": getShortGameId(game_id), "src_client_type": 1,
            "src_app_channel": packet["app_channel"], "src_pay_channel": packet["pay_channel"],
            "src_jf_game_id": packet["jf_game_id"], "src_sdk_version": packet["sdk_version"],
            "src_udid": packet["udid"], "is_remember": True,
        },
    }


class MpayDBSync:

    def __init__(self, manager, artifact):
        self.manager, self.logger = manager, manager.logger
        self.wasm = MpayWasm(artifact)
        self.root = Path(os.environ.get('APPDATA', '')) / 'Netease' / 'Mpay'
        self.identity = machine_identifier() if os.name == 'nt' else ''
        self.lock = threading.RLock()
        self.stopping = False
        self.bindings = genv.get('mpay_db_bindings', {})
        self.writes = genv.get('mpay_db_writes', {})
        self.baselines, self.active, self.candidates = {}, {}, {}
        self.pending, self.shells, self.signatures = {}, {}, {}
        self.helpers = genv.get("mpay_db_helpers", {})
        self.attempted = {}
        self.projected_aliases = {}
        self.exchange_pending = {}

    def paths(self, game):
        return sorted(self.root.glob(f'*-g-{getShortGameId(game)}-64-mpay.db'))

    def game_ids(self):
        return sorted({m.group(1) for p in self.root.glob('*-g-*-64-mpay.db')
                       if (m := re.match(r'.*-g-(.+)-64-mpay\.db$', p.name))})

    def save_bindings(self):
        genv.set('mpay_db_bindings', self.bindings, True)
        genv.set('mpay_db_writes', self.writes, True)

    def operate(self, path, operation, **args):
        with self.lock, locked_database(path) as file:
            original = file.read(MAX_DB_BYTES + 1)
            if len(original) > MAX_DB_BYTES:
                raise ValueError('MPay database exceeds supported size')
            result = self.wasm.invoke(operation, original, **args)
            if operation == 'list_uids':
                return set(json.loads(result))
            write_file_restricted(str(path) + '.idv-login.bak', original)
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

    def _snapshot(self, paths):
        for path in paths:
            if path not in self.baselines:
                self.baselines[path] = self.operate(path, 'list_uids')
                self.active[path], self.candidates[path] = {}, set()

    def _cache(self, record, game, sdkuid, credentials, expiry=None):
        self.writes.setdefault(record.uuid, {})[game] = {
            'sdkuid': str(sdkuid), 'credentials': copy.deepcopy(credentials),
            'written_at': int(time.time()), 'expires_at': expiry, 'expired': False}
        self.pending[(game, record.channel_name, str(sdkuid))] = record.uuid
        self.save_bindings()

    def _scan_credentials(self, record):
        user = record.user_info
        if not user.get('id') or not user.get('token'):
            return None
        expiry = user.get('expires', user.get('expire'))
        try:
            expiry = int(expiry) if expiry is not None else None
        except (ValueError, TypeError):
            expiry = None
        return str(user['id']), {'token': user['token'], 'pc_ext_info': copy.deepcopy(user.get('pc_ext_info') or record.ext_info)}, expiry

    def remember_exchange(self, record, game_id, user):
        game = getShortGameId(game_id)
        with self.lock:
            self.bindings.setdefault(record.uuid, {})[game] = str(user['id'])
            sdkuid = str(user['id'])
            complete = False
            credentials = {'token': user.get('token', ''), 'pc_ext_info': copy.deepcopy(user.get('pc_ext_info') or record.ext_info)}
            try:
                extra = credentials['pc_ext_info'].get('extra_unisdk_data', '{}')
                extra = json.loads(extra) if isinstance(extra, str) else extra
                auth = json.loads(base64.b64decode(unquote(extra['SAUTH_JSON'])))
                sdkuid = str(auth['sdkuid'])
                complete = bool(auth.get('sessionid'))
            except (KeyError, ValueError, TypeError):
                pass
            if credentials['token'] and (record.record_source != 'manual' or complete):
                scan = self._scan_credentials(record)
                prior = self.writes.get(record.uuid, {}).get(game, {})
                expiry = prior.get('expires_at') if record.record_source == 'manual' else scan[2] if scan else None
                self._cache(record, game, sdkuid, credentials, expiry)
            self.exchange_pending[(game, record.channel_name)] = record.uuid
            self.pending[(game, record.channel_name, sdkuid)] = record.uuid
            self.save_bindings()

    def is_expired(self, uuid, game):
        return bool(self.writes.get(uuid, {}).get(getShortGameId(game), {}).get('expired'))

    def imported_uuids(self, game):
        game = getShortGameId(game)
        with self.lock:
            included = {uuid for path in self.paths(game) for uuid in self.active.get(path, {})}
            return included | {alias for alias, winner in self.projected_aliases.get(game, {}).items() if winner in included}

    def packet_credentials(self, record, game, packet):
        if packet is None or packet is False:
            return packet
        if not isinstance(packet, dict) or packet.get('login_channel') != record.channel_name:
            raise ValueError('渠道没有返回一致的续期凭证')
        return credentials_from_packet(packet, game)

    def prepare_credentials(self, record, game):
        return self.packet_credentials(record, game, record.get_uniSdk_data(game, interactive=False))

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
        for key in ('uuid', 'name', 'last_login_time', 'game_id', 'crossGames', 'record_source'):
            setattr(clone, key, getattr(record, key))
        return clone

    @staticmethod
    def _fresh(written):
        return bool(written.get('credentials')) and not written.get('expired') and (written.get('expires_at') is None or written['expires_at'] > time.time())

    def refresh_startup(self):
        from account_list_policy import AccountListPolicy
        selected = AccountListPolicy(self.manager).select()
        with self.lock:
            self._snapshot([p for game in self.game_ids() for p in self.paths(game)])
        self._prepare_selected(selected)
        for game in selected:
            self._project_game(game)

    def _prepare_selected(self, selected):
        with self.lock:
            self._snapshot([p for game in selected for p in self.paths(game)])
        jobs = []
        for game, records in selected.items():
            for record in records:
                written = self.writes.get(record.uuid, {}).get(game, {})
                if self._fresh(written):
                    continue
                cache_version = (written.get('written_at'), written.get('expires_at'), written.get('expired'))
                if self.attempted.get((record.uuid, game)) == cache_version:
                    continue
                self.attempted[(record.uuid, game)] = cache_version
                if record.record_source != 'manual':
                    scan = self._scan_credentials(record)
                    if scan and not written.get('expired') and (scan[2] is None or scan[2] > time.time()):
                        with self.lock:
                            self._cache(record, game, *scan[:2], expiry=scan[2])
                    else:
                        self.mark_expired(record, game)
                    current = self.writes.get(record.uuid, {}).get(game, {})
                    self.attempted[(record.uuid, game)] = (current.get('written_at'), current.get('expires_at'), current.get('expired'))
                    continue
                future, started = Future(), time.monotonic()
                try:
                    clone = self._clone(record)
                except Exception:
                    self.mark_expired(record, game)
                    current = self.writes.get(record.uuid, {}).get(game, {})
                    self.attempted[(record.uuid, game)] = (current.get('written_at'), current.get('expires_at'), current.get('expired'))
                    continue
                def run(future=future, clone=clone, game=game, started=started):
                    try:
                        from prefetch_context import prefetch_scope
                        with prefetch_scope(started + 5):
                            prepared = self.prepare_credentials(clone, game)
                            future.set_result((clone, prepared, credential_expires_at(clone), time.monotonic()))
                    except BaseException as error:
                        future.set_exception(error)
                threading.Thread(target=run, daemon=True, name='mpay-renew').start()
                jobs.append((record, game, future, started))
        for record, game, future, started in jobs:
            try:
                clone, prepared, expiry, finished = future.result(timeout=max(0, 5 - (time.monotonic() - started)))
                if finished > started + 5 or not prepared:
                    raise TimeoutError()
                with self.lock:
                    if self.stopping or self.manager.query_channel(record.uuid) is not record:
                        continue
                    preserved = {key: getattr(record, key) for key in ('uuid', 'name', 'last_login_time', 'game_id', 'crossGames', 'record_source')}
                    record.__dict__.update(clone.__dict__)
                    record.__dict__.update(preserved)
                    self._cache(record, game, *prepared, expiry=expiry)
                    self.manager.save_records()
            except Exception:
                self.mark_expired(record, game)
                current = self.writes.get(record.uuid, {}).get(game, {})
                self.attempted[(record.uuid, game)] = (current.get('written_at'), current.get('expires_at'), current.get('expired'))

    def mark_expired(self, record, game=None):
        with self.lock:
            if self.manager.query_channel(record.uuid) is not record:
                return
            game = getShortGameId(game or record.game_id)
            self.writes.setdefault(record.uuid, {}).setdefault(game, {}).update(expires_at=int(time.time()), expired=True)
            self.save_bindings()

    def record_sauth(self, data, success):
        if not isinstance(data, dict):
            return False
        game = getShortGameId(str(data.get('gameid', '')))
        key = (game, data.get('login_channel'), str(data.get('sdkuid', '')))
        with self.lock:
            uuid = self.pending.get(key)
            if uuid is None:
                uuid = next((r.uuid for r in self.manager.channels if r.channel_name == key[1] and str(self.writes.get(r.uuid, {}).get(game, {}).get('sdkuid', '')) == key[2]), None)
            if uuid is None:
                uuid = self.exchange_pending.get((game, key[1]))
                if uuid and key[2]:
                    self.writes.setdefault(uuid, {}).setdefault(game, {})['sdkuid'] = key[2]
                    self.pending[key] = uuid
                    self.save_bindings()
            record = self.manager.query_channel(uuid) if uuid else None
            if record is None:
                return False
            self.exchange_pending.pop((game, key[1]), None)
            if success:
                record.last_login_time = int(time.time())
                self.manager.save_records()
                self.pending.pop(key, None)
            else:
                # The game invalidates the cached package; only a failed silent
                # renewal changes the UI's explicit "已过期" state.
                self.writes.setdefault(record.uuid, {}).setdefault(game, {})['expires_at'] = int(time.time())
                self.attempted.pop((record.uuid, game), None)
                self.save_bindings()
            return True

    def mark_sauth_expired(self, data):
        return self.record_sauth(data, False)

    def created(self, record, on_complete=None):
        def ready(packet):
            try:
                prepared = self.packet_credentials(record, record.game_id, packet)
                result = prepared if prepared is None or prepared is False else not self.stopping
                if result:
                    with self.lock:
                        self._cache(record, getShortGameId(record.game_id), *prepared, expiry=credential_expires_at(record))
            except Exception as error:
                log_failure(self.logger, '[mpay-db] 新账号凭证准备失败', error)
                result = False
            if on_complete:
                import app_state
                app_state.run_on_main_thread(lambda: on_complete(result))
            return result
        if self.stopping:
            return ready(False)
        if on_complete:
            record.get_uniSdk_data(record.game_id, on_complete=ready, interactive=True)
            return None
        return ready(record.get_uniSdk_data(record.game_id, interactive=True))

    def _placeholder(self, game, uid, record, credentials=None):
        credentials = copy.deepcopy(credentials or {})
        credentials.setdefault('token', record.user_info.get('token', ''))
        ext = credentials.setdefault('pc_ext_info', copy.deepcopy(record.ext_info))
        try:
            extra = ext.get('extra_unisdk_data', '{}')
            extra = json.loads(extra) if isinstance(extra, str) else copy.deepcopy(extra)
            auth = json.loads(base64.b64decode(unquote(extra['SAUTH_JSON'])))
            auth['sdkuid'] = uid
            extra['SAUTH_JSON'] = base64.b64encode(json.dumps(auth).encode()).decode()
            ext['extra_unisdk_data'] = json.dumps(extra)
        except (KeyError, ValueError, TypeError):
            pass
        return credentials

    def _helper_uid(self, game, role, source, occupied):
        helpers = self.helpers.setdefault(game, {})
        existing = helpers.get(role)
        if existing and existing['uid'] not in occupied:
            return existing['uid']
        salt = 0
        while True:
            digest = hashlib.sha256(f'{game}:{role}:{source}:{salt}'.encode()).hexdigest()
            if source.isdigit():
                uid = str(int(digest, 16) % (10 ** len(source))).zfill(len(source))
            elif re.fullmatch('[0-9a-fA-F]+', source):
                uid = (digest * ((len(source) // len(digest)) + 1))[:len(source)]
            else:
                uid = digest[:32]
            if uid not in occupied:
                helpers[role] = {'uid': uid}
                genv.set('mpay_db_helpers', self.helpers, True)
                return uid
            salt += 1

    @staticmethod
    def _create(uid, name, channel_name):
        return {'id': uid, 'login_channel': channel_name, 'login_type': 0,
                '__user_login_method': 17, '__user_platform_selected': '',
                '__user_platform_selected_second': '', '__user_platform_selected_third': '',
                '__user_typed_username': '', 'client_username': name,
                'display_username': name, 'nickname': '', 'avatar': '',
                'ext_access_token': '', 'need_mask': False, 'pc_ext_info': {},
                'related_login_status': 0, 'mask_related_mobile': ''}

    def refresh_before_game(self, game_id):
        from account_list_policy import AccountListPolicy
        selected = AccountListPolicy(self.manager).select()
        game = getShortGameId(game_id)
        self._prepare_selected({game: selected.get(game, [])})
        self._project_game(game)

    def _project_game(self, game_id):
        from account_list_policy import AccountListPolicy
        game = getShortGameId(game_id)
        policy = AccountListPolicy(self.manager)
        records = policy.select().get(game, [])
        game_records = policy._candidates(game)
        self.projected_aliases[game] = dict(policy.aliases.get(game, {}))
        accounts, active, shells = [], {}, set()
        for record in records:
            written = self.writes.get(record.uuid, {}).get(game, {})
            uid = str(self.bindings.get(record.uuid, {}).get(game) or written.get('sdkuid') or record.user_info.get('id') or hashlib.sha256(record.uuid.encode()).hexdigest()[:32])
            fresh = self._fresh(written)
            credentials = written['credentials'] if fresh else self._placeholder(game, uid, record, written.get("credentials"))
            if not fresh:
                shells.add(uid)
            name = policy.account_label(record, expired=self.is_expired(record.uuid, game))
            accounts.append({'uid': uid, 'name': name, 'credentials': credentials, 'create': self._create(uid, name, record.channel_name)})
            active[record.uuid] = uid
        roles = [('help', '【点我再点登录获取帮助】')]
        if any(self.is_expired(r.uuid, game) for r in game_records):
            roles.append(('expired', '【点我再点击登录刷新过期记录】'))
        paths = self.paths(game)
        with self.lock:
            self._snapshot(paths)
        occupied = set(active.values())
        helper_known = {item['uid'] for item in self.helpers.get(game, {}).values()}
        occupied.update(uid for path in paths for uid in self.baselines[path] if uid not in helper_known)
        if game_records:
            source = records[0] if records else game_records[0]
            written = self.writes.get(source.uuid, {}).get(game, {})
            template = accounts[0] if accounts else {
                'uid': str(self.bindings.get(source.uuid, {}).get(game) or written.get('sdkuid')
                           or source.user_info.get('id') or hashlib.sha256(source.uuid.encode()).hexdigest()[:32]),
                'credentials': written.get('credentials') or self._placeholder(game, '', source),
            }
            for role, name in roles:
                uid = self._helper_uid(game, role, template['uid'], occupied)
                occupied.add(uid)
                self.helpers[game][role].update(channel=source.channel_name, game=game)
                accounts.append({'uid': uid, 'name': name,
                                 'credentials': self._placeholder(game, uid, source, template['credentials']),
                                 'create': self._create(uid, name, source.channel_name), 'append': True})
                shells.add(uid)
            genv.set('mpay_db_helpers', self.helpers, True)
        signature = json.dumps(accounts, sort_keys=True, ensure_ascii=False)
        with self.lock:
            if self.stopping:
                return
            paths = self.paths(game)
            self._snapshot(paths)
            for path in paths:
                if self.signatures.get(path) == signature:
                    continue
                stale = set(self.active[path].values()) - set(active.values())
                if stale:
                    self.operate(path, 'cleanup_accounts', uids=list(stale), baseline_uids=list(self.baselines[path]))
                self.operate(path, 'project_accounts', accounts=accounts, machine_identifier=self.identity)
                self.active[path] = dict(active)
                self.candidates[path].update(a['uid'] for a in accounts)
                self.signatures[path] = signature
            self.shells[game] = shells

    def write_packet(self, record, game_id, sdkuid, credentials, *, new=False):
        with self.lock:
            self._cache(record, getShortGameId(game_id), sdkuid, credentials, credential_expires_at(record))
        return True

    def intercept_mpay_login(self, game_id, uid):
        game, uid = getShortGameId(game_id), str(uid)
        role = next((role for role, item in self.helpers.get(game, {}).items() if item['uid'] == uid), None)
        if role is None and uid not in self.shells.get(game, set()):
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

    def intercept_auth(self, data):
        return isinstance(data, dict) and self.intercept_mpay_login(str(data.get('gameid', '')), data.get('sdkuid', ''))

    def reconcile_game_deletions(self):
        missing = set()
        with self.lock:
            for path, active in self.active.items():
                if path.exists():
                    uids = self.operate(path, 'list_uids')
                    missing.update(uuid for uuid, uid in active.items() if uid not in uids)
        missing.update(alias for aliases in self.projected_aliases.values() for alias, canonical in aliases.items() if canonical in missing)
        for uuid in missing:
            if self.manager.query_channel(uuid) is not None:
                self.manager.delete(uuid)

    def delete_record(self, uuid):
        with self.lock:
            for game, uid in self.bindings.get(uuid, {}).items():
                for path in self.paths(game):
                    self.operate(path, 'delete_uid', uid=uid)
            for path, active in self.active.items():
                uid = active.pop(uuid, None)
                if uid and path.exists():
                    self.operate(path, 'delete_uid', uid=uid)
            self.bindings.pop(uuid, None)
            self.writes.pop(uuid, None)
            self.pending = {key: value for key, value in self.pending.items() if value != uuid}
            self.attempted = {key: value for key, value in self.attempted.items() if key[0] != uuid}
            self.signatures.clear()
            self.save_bindings()

    def rename_record(self, uuid, name):
        from account_list_policy import AccountListPolicy
        record = self.manager.query_channel(uuid)
        if record is None:
            return
        policy = AccountListPolicy(self.manager)
        renamed = copy.copy(record)
        renamed.name = name
        with self.lock:
            targets = {(path, game): uid for game, uid in self.bindings.get(uuid, {}).items()
                       for path in self.paths(game)}
            for path, active in self.active.items():
                uid = active.get(uuid)
                if uid and path.exists():
                    game = next((g for g in self.game_ids() if path in self.paths(g)), record.game_id)
                    targets[path, game] = uid
            for (path, game), uid in targets.items():
                if uid in self.operate(path, 'list_uids'):
                    self.operate(path, 'rename_uid', uid=uid, name=policy.account_label(renamed, expired=self.is_expired(uuid, game)))
            self.signatures.clear()

    def shutdown(self):
        self.stopping = True
        self.reconcile_game_deletions()
        with self.lock:
            for path, candidates in self.candidates.items():
                if path.exists():
                    self.operate(path, 'cleanup_accounts', uids=list(candidates), baseline_uids=list(self.baselines[path]))
            self.active.clear()
