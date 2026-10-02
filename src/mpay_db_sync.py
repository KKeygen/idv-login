"""Account lifecycle synchronization. All IO stays in this open-source host."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import threading
import time
import traceback
from urllib.parse import unquote

from channelHandler.channelUtils import getShortGameId, cmp_game_id
from envmgr import genv
from mpay_wasm import MpayWasm, MAX_DB_BYTES
from secure_write import write_file_restricted

INVALID_SUFFIX = "（已失效，在网页重新登录）"
SQLITE_PENDING_BYTE = 0x40000000


def credential_expires_at(record):
    """Read existing channel expiry rules; absent rules mean no deadline."""
    value = None
    if record.channel_name == "myapp":
        session = getattr(record, "session", None)
        if session is not None and session.atk_expire:
            value = record.last_login_time + int(session.atk_expire)
    elif record.channel_name == "honor_sdk":
        value = record.honorLogin.expiredTime
    elif record.channel_name == "bilibili_sdk":
        value = (record._get_login_data() or {}).get("expires")
    elif record.channel_name == "uc_platform":
        value = record.sid_expire_time
    elif record.channel_name == "360_assistant":
        value = record.token_expire_time
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
    def __init__(self, manager, artifact: Path):
        self.manager = manager
        self.logger = manager.logger
        self.wasm = MpayWasm(artifact)
        self.root = Path(os.environ["APPDATA"]) / "Netease" / "Mpay"
        self.identity = machine_identifier()
        self.lock = threading.RLock()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mpay-renew")
        self.stopping = False
        # UUID -> game -> MPay UID. SDK UID and MPay UID are not assumed equal
        # when a real exchange_token response provides the mapping.
        self.bindings = genv.get("mpay_db_bindings", {})
        self.writes = genv.get("mpay_db_writes", {})

    def paths(self, game_id: str) -> list[Path]:
        return sorted(self.root.glob(f"*-g-{getShortGameId(game_id)}-64-mpay.db"))

    def save_bindings(self):
        genv.set("mpay_db_bindings", self.bindings, True)
        genv.set("mpay_db_writes", self.writes, True)

    def operate(self, path: Path, operation: str, **args):
        with self.lock, locked_database(path) as file:
            original = file.read(MAX_DB_BYTES + 1)
            if len(original) > MAX_DB_BYTES:
                raise ValueError("MPay database exceeds the supported size")
            result = self.wasm.invoke(operation, original, **args)
            if operation == "list_uids":
                return set(json.loads(result))
            # Preserve only an encrypted backup. No plaintext database IO.
            write_file_restricted(str(path) + ".idv-login.bak", original)
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

    def remember_exchange(self, record, game_id: str, user: dict):
        """Observe the successful MPay UID; let MPay persist its own response."""
        with self.lock:
            self.bindings.setdefault(record.uuid, {})[getShortGameId(game_id)] = str(user["id"])
            self.save_bindings()

    def reconcile_game_deletions(self):
        """Only remembered, previously synchronized records can be deleted here."""
        with self.lock:
            game_uids = {}
            for uuid, games in list(self.bindings.items()):
                for game_id, uid in list(games.items()):
                    if game_id not in game_uids:
                        paths = self.paths(game_id)
                        game_uids[game_id] = (set().union(
                            *(self.operate(p, "list_uids") for p in paths)
                        ) if paths else None)
                    uids = game_uids[game_id]
                    if uids is None:
                        # Missing DB is not proof the user removed this account.
                        continue
                    if uid not in uids:
                        self.logger.info("[mpay-db] 游戏已删除关联记录，同步删除工具账号")
                        self.manager.delete(uuid)
                        break

    def delete_record(self, uuid: str):
        with self.lock:
            for game_id, uid in self.bindings.get(uuid, {}).items():
                for path in self.paths(game_id):
                    self.operate(path, "delete_uid", uid=uid)
            self.bindings.pop(uuid, None)
            self.writes.pop(uuid, None)
            self.save_bindings()

    def rename_record(self, uuid: str, name: str):
        with self.lock:
            for game_id, uid in self.bindings.get(uuid, {}).items():
                for path in self.paths(game_id):
                    if uid in self.operate(path, "list_uids"):
                        self.operate(path, "rename_uid", uid=uid, name=name)

    def write_packet(self, record, game_id: str, sdkuid: str, credentials: dict, *, new=False):
        with self.lock:
            if self.stopping:
                return False
            if not new and self.manager.query_channel(record.uuid) is not record:
                return False  # The user deleted/replaced it during a network renewal.
            paths = self.paths(game_id)
            if not paths:
                raise FileNotFoundError("请先启动该游戏一次，初始化 MPay 数据库")
            uid = self.bindings.get(record.uuid, {}).get(getShortGameId(game_id), sdkuid)
            create = {"id": uid, "login_channel": record.channel_name, "login_type": 0,
                "__user_login_method": 17, "__user_platform_selected": "",
                "__user_platform_selected_second": "", "__user_platform_selected_third": "",
                "__user_typed_username": "", "client_username": record.name,
                "display_username": record.name, "nickname": "", "avatar": "",
                "ext_access_token": "", "need_mask": False, "pc_ext_info": {},
                "related_login_status": 0, "mask_related_mobile": ""}
            for path in paths:
                self.operate(path, "write_credentials", uid=uid, credentials=credentials,
                             create=create, machine_identifier=self.identity)
            self.bindings.setdefault(record.uuid, {})[getShortGameId(game_id)] = uid
            self.writes.setdefault(record.uuid, {})[getShortGameId(game_id)] = {
                "sdkuid": sdkuid,
                "written_at": int(time.time()),
                "expires_at": credential_expires_at(record),
            }
            self.save_bindings()
            self.logger.info("[mpay-db] 凭证已写入游戏账号库")
            return True

    def packet_credentials(self, record, game_id: str, packet):
        if packet is None or packet is False:
            return packet
        if not isinstance(packet, dict):
            raise ValueError("渠道没有返回有效续期凭证")
        if packet.get("login_channel") != record.channel_name:
            raise ValueError("渠道凭证与账号渠道不一致")
        return credentials_from_packet(packet, game_id)

    def prepare_credentials(self, record, game_id: str):
        packet = record.get_uniSdk_data(game_id, interactive=False)
        return self.packet_credentials(record, game_id, packet)

    def renew(self, record, game_id: str):
        if record.record_source != "manual":
            return
        written = self.writes.get(record.uuid, {}).get(getShortGameId(game_id), {})
        if written.get("written_at") and (
            written.get("expires_at") is None or written["expires_at"] > time.time()
        ):
            return
        name = record.name
        try:
            prepared = self.prepare_credentials(record, game_id)
            if prepared is None or prepared is False:
                raise ValueError("渠道续期需要用户重新登录")
        except Exception as error:
            record.name = name
            log_failure(self.logger, "[mpay-db] 渠道续期失败", error)
            self.mark_expired(record)
            return
        with self.lock:
            if self.manager.query_channel(record.uuid) is not record:
                return
            record.name = name
            try:
                if self.write_packet(record, game_id, *prepared):
                    self.manager.save_records()
            except Exception as error:
                log_failure(self.logger, "[mpay-db] 数据库同步失败，账号未删除", error)

    def mark_expired(self, record):
        with self.lock:
            if self.manager.query_channel(record.uuid) is not record:
                return
            name = record.name if record.name.endswith(INVALID_SUFFIX) else record.name + INVALID_SUFFIX
            record.name = name
            self.manager.save_records()
            self.rename_record(record.uuid, name)
            self.logger.warning("[mpay-db] 续期失败，关联账号已标记为失效")

    def mark_sauth_expired(self, data):
        """A failed check expires the matching account/channel/game write."""
        if not isinstance(data, dict) or not data.get("gameid") or not data.get("sdkuid"):
            return
        game_id = getShortGameId(str(data["gameid"]))
        with self.lock:
            for record in self.manager.channels:
                written = self.writes.get(record.uuid, {}).get(game_id)
                if (written and record.channel_name == data.get("login_channel")
                        and str(written["sdkuid"]) == str(data["sdkuid"])):
                    written["expires_at"] = int(time.time())
            self.save_bindings()

    def refresh_startup(self):
        # Check first: renewing first would resurrect a game-side deletion.
        self.reconcile_game_deletions()
        for record in list(self.manager.channels):
            if record.record_source != "manual":
                continue
            games = {getShortGameId(game) for game in
                     (record.game_id, *self.bindings.get(record.uuid, {}))}
            for game_id in games:
                try:
                    self.renew(record, game_id)
                except (OSError, RuntimeError) as error:
                    log_failure(self.logger, "[mpay-db] 数据库同步失败，账号未删除", error)

    def created(self, record, on_complete=None):
        """Complete the interactive packet first, then write on the single worker."""
        def packet_ready(packet):
            try:
                prepared = self.packet_credentials(record, record.game_id, packet)
                if self.stopping:
                    result = False
                elif prepared is None or prepared is False:
                    result = prepared
                else:
                    future = self.worker.submit(
                        self.write_packet, record, record.game_id, *prepared, new=True
                    )
                    if on_complete:
                        import app_state
                        def written(done):
                            try:
                                result = done.result()
                            except Exception as error:
                                log_failure(self.logger, "[mpay-db] 新账号同步失败", error)
                                result = False
                            app_state.run_on_main_thread(lambda: on_complete(result))
                        future.add_done_callback(written)
                        return None
                    return future.result()
            except Exception as error:
                log_failure(self.logger, "[mpay-db] 新账号同步失败", error)
                result = False
            if on_complete:
                on_complete(result)
            return result

        if self.stopping:
            if on_complete:
                on_complete(False)
            return False
        if on_complete:
            record.get_uniSdk_data(
                record.game_id, on_complete=packet_ready, interactive=True
            )
            return None
        return packet_ready(record.get_uniSdk_data(record.game_id, interactive=True))

    def shutdown(self):
        self.stopping = True
        self.worker.shutdown(wait=True)
        self.reconcile_game_deletions()
