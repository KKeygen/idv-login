# coding=UTF-8
import base64
import copy
import json
import os
import platform
import secrets
import socket
import string
import time
from typing import Any, Dict, List, Optional

import requests
from Crypto.Cipher import AES
from argon2.low_level import Type, hash_secret_raw

from envmgr import genv


class WebNote:
    BASE_URL = "https://api-webnote.txttool.cn/netcut/note"
    SALT_NOTE_ID = b"idv-login/cloud-sync/note-id/v2"
    SALT_NOTE_PASSWORD = b"idv-login/cloud-sync/note-password/v2"
    ARGON2_TIME_COST = 3
    ARGON2_MEMORY_COST_KIB = 262144
    ARGON2_PARALLELISM = 2

    def __init__(self, note_id: str, note_password: str, timeout: int = 15):
        self.note_id = note_id
        self.note_password = note_password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "accept-encoding": "gzip, deflate, br, zstd",
                "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                "cache-control": "no-cache",
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "dnt": "1",
                "origin": "https://webnote.cc",
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": "https://webnote.cc/",
                "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Microsoft Edge";v="144"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "cross-site",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0",
            }
        )

    @classmethod
    def from_master_key(cls, master_key: str, timeout: int = 15):
        note_id = cls.derive_note_id(master_key)
        note_password = cls.derive_note_password(master_key)
        return cls(note_id, note_password, timeout=timeout)

    @classmethod
    def _derive_hex(cls, master_key: str, salt: bytes, dklen: int = 32) -> str:
        material = hash_secret_raw(
            secret=master_key.encode("utf-8"),
            salt=salt,
            time_cost=cls.ARGON2_TIME_COST,
            memory_cost=cls.ARGON2_MEMORY_COST_KIB,
            parallelism=cls.ARGON2_PARALLELISM,
            hash_len=dklen,
            type=Type.ID,
        )
        return material.hex()

    @classmethod
    def derive_note_id(cls, master_key: str) -> str:
        return cls._derive_hex(master_key, cls.SALT_NOTE_ID, dklen=16)

    @classmethod
    def derive_note_password(cls, master_key: str) -> str:
        return cls._derive_hex(master_key, cls.SALT_NOTE_PASSWORD, dklen=32)

    def _post(self, path: str, form_data: Dict[str, Any], allowed_non_success_status: Optional[List[str]] = None):
        response = self.session.post(
            f"{self.BASE_URL}/{path}/",
            data=form_data,
            timeout=self.timeout,
        )

        response_json = None
        try:
            response_json = response.json()
        except Exception:
            response_json = None

        if isinstance(response_json, dict):
            status_value = response_json.get("status", None)
            if status_value is not None and str(status_value) != "1":
                if allowed_non_success_status and str(status_value) in [str(item) for item in allowed_non_success_status]:
                    return response_json
                error_message = response_json.get("error")
                if error_message:
                    raise ValueError(str(error_message))
                raise ValueError(f"webnote请求失败，status={status_value}")

        if not response.ok:
            if isinstance(response_json, dict) and response_json.get("error"):
                raise ValueError(str(response_json.get("error")))
            response.raise_for_status()

        if isinstance(response_json, dict):
            return response_json
        return {"raw": response.text}

    def get(self, allow_missing: bool = False):
        return self._post(
            "info",
            {
                "note_name": self.note_id,
                "note_pwd": self.note_password,
            },
            allowed_non_success_status=["2"] if allow_missing else None,
        )

    def new(self, note_content: str, expire_time: int = 259200):
        placeholder_content = json.dumps(
            [{"title": "sync_placeholder", "content": "pending"}],
            ensure_ascii=False,
        )

        # 第一次：不带密码创建占位符，获取 note_id/note_token
        created = self._post(
            "save",
            {
                "note_name": self.note_id,
                "note_id": "",
                "note_content": placeholder_content,
                "note_token": "",
                "expire_time": int(expire_time),
                "note_pwd": "",
            },
        )

        data = created.get("data", {}) if isinstance(created, dict) else {}
        note_internal_id = str(data.get("note_id", ""))
        note_token = str(data.get("note_token", ""))
        if not note_internal_id or not note_token:
            raise ValueError("创建云同步占位记录失败：未返回 note_id/note_token")

        # 第二次：带 token + 密码写入正式内容
        return self._post(
            "save",
            {
                "note_name": self.note_id,
                "note_id": note_internal_id,
                "note_content": note_content,
                "note_token": note_token,
                "expire_time": int(expire_time),
                "note_pwd": self.note_password,
            },
        )

    def update(
        self,
        note_internal_id: str,
        note_token: str,
        note_content: str,
        expire_time: int = 259200,
    ):
        return self._post(
            "save",
            {
                "note_name": self.note_id,
                "note_id": note_internal_id,
                "note_content": note_content,
                "note_token": note_token,
                "expire_time": int(expire_time),
                "note_pwd": self.note_password,
            },
        )

    def delete(self, note_internal_id: str, note_token: str):
        return self._post(
            "deleteNote",
            {
                "note_id": note_internal_id,
                "note_token": note_token,
                "note_name": self.note_id,
            },
        )


class CloudSyncManager:
    EDGE_FILE_CANDIDATES = [
        "fakeDevice.json",
        "device.json",
        "huawei_device.json",
    ]

    DATA_TITLE = "sync_data"
    LOG_TITLE = "access_log"
    SALT_AES_KEY = b"idv-login/cloud-sync/aes-key/v2"
    ARGON2_TIME_COST = 3
    ARGON2_MEMORY_COST_KIB = 262144
    ARGON2_PARALLELISM = 2

    def __init__(self, logger=None):
        self.logger = logger

    def evaluate_master_key_strength(self, master_key: str) -> Dict[str, Any]:
        key = master_key or ""
        has_upper = any(ch.isupper() for ch in key)
        has_lower = any(ch.islower() for ch in key)
        has_digit = any(ch.isdigit() for ch in key)
        has_symbol = any(ch in string.punctuation for ch in key)
        char_classes = sum([has_upper, has_lower, has_digit, has_symbol])
        length = len(key)

        score = 0
        if length >= 12:
            score += 1
        if length >= 16:
            score += 1
        if char_classes >= 3:
            score += 1
        if char_classes == 4:
            score += 1

        strength = "弱"
        if score >= 4:
            strength = "强"
        elif score >= 3:
            strength = "中"

        valid = length >= 12 and char_classes >= 3

        return {
            "valid": valid,
            "length": length,
            "char_classes": char_classes,
            "has_upper": has_upper,
            "has_lower": has_lower,
            "has_digit": has_digit,
            "has_symbol": has_symbol,
            "score": score,
            "strength": strength,
        }

    def ensure_master_key_valid(self, master_key: str):
        result = self.evaluate_master_key_strength(master_key)
        if not result["valid"]:
            raise ValueError("主密钥强度不足：至少12位，且包含大小写字母、数字、符号中的任意3类")

    def generate_master_key(self, length: int = 16) -> str:
        if length < 16:
            length = 16
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
        while True:
            key = "".join(secrets.choice(alphabet) for _ in range(length))
            if self.evaluate_master_key_strength(key)["valid"]:
                return key

    def save_master_key_txt(self, master_key: str, output_path: str = "") -> str:
        self.ensure_master_key_valid(master_key)
        work_dir = genv.get("FP_WORKDIR", os.getcwd())
        if not output_path:
            ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            output_path = os.path.join(work_dir, f"idv-login-master-key-{ts}.txt")
        content = (
            "IDV Login 云同步主密钥\n"
            "请妥善保管，泄露将导致云端密文可被解密。\n\n"
            f"master_key={master_key}\n"
        )
        with open(output_path, "w", encoding="utf-8") as file:
            file.write(content)
        return output_path

    def _derive_aes_key(self, master_key: str) -> bytes:
        self.ensure_master_key_valid(master_key)
        return hash_secret_raw(
            secret=master_key.encode("utf-8"),
            salt=self.SALT_AES_KEY,
            time_cost=self.ARGON2_TIME_COST,
            memory_cost=self.ARGON2_MEMORY_COST_KIB,
            parallelism=self.ARGON2_PARALLELISM,
            hash_len=64,
            type=Type.ID,
        )

    def encrypt_text(self, plain_text: str, master_key: str) -> str:
        key = self._derive_aes_key(master_key)
        cipher = AES.new(key, AES.MODE_SIV)
        ciphertext, tag = cipher.encrypt_and_digest(plain_text.encode("utf-8"))
        payload = {
            "v": 2,
            "t": base64.b64encode(tag).decode("utf-8"),
            "c": base64.b64encode(ciphertext).decode("utf-8"),
        }
        return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("utf-8")

    def decrypt_text(self, encrypted_text: str, master_key: str) -> str:
        key = self._derive_aes_key(master_key)
        payload = json.loads(base64.b64decode(encrypted_text).decode("utf-8"))
        tag = base64.b64decode(payload["t"])
        ciphertext = base64.b64decode(payload["c"])
        cipher = AES.new(key, AES.MODE_SIV)
        plain = cipher.decrypt_and_verify(ciphertext, tag)
        return plain.decode("utf-8")

    def _load_channels_raw(self) -> List[dict]:
        channels_path = genv.get("FP_CHANNEL_RECORD", "")
        if not channels_path or not os.path.exists(channels_path):
            return []
        with open(channels_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []

    @staticmethod
    def _same_game(game_a: str, game_b: str) -> bool:
        if not game_a or not game_b:
            return game_a == game_b
        return game_a.split("-")[-1] == game_b.split("-")[-1]

    def _filter_channels(self, channels: List[dict], scope: dict) -> List[dict]:
        scope_type = scope.get("type", "all")
        if scope_type == "all":
            return channels
        if scope_type == "current_game":
            game_id = scope.get("game_id", "")
            return [item for item in channels if self._same_game(item.get("game_id", ""), game_id)]
        if scope_type == "selected":
            selected_uuids = set(scope.get("uuids", []))
            return [item for item in channels if item.get("uuid", "") in selected_uuids]
        return channels

    def _collect_edge_files(self, selected_channels: List[dict]) -> Dict[str, str]:
        work_dir = genv.get("FP_WORKDIR", "")
        edge_files: Dict[str, str] = {}

        required_files = set(["fakeDevice.json"])
        channel_names = {
            (item.get("login_info", {}) or {}).get("login_channel", "")
            for item in selected_channels
        }
        if "xiaomi_app" in channel_names or "nearme_vivo" in channel_names:
            required_files.add("device.json")
        if "huawei" in channel_names:
            required_files.add("huawei_device.json")

        for filename in required_files:
            file_path = os.path.join(work_dir, filename)
            if os.path.exists(file_path):
                with open(file_path, "rb") as file:
                    edge_files[filename] = base64.b64encode(file.read()).decode("utf-8")

        return edge_files

    def _device_meta(self) -> Dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "version": genv.get("VERSION", ""),
            "timestamp": int(time.time()),
        }

    @staticmethod
    def _recursive_find_first_key(obj: Any, key: str) -> Optional[Any]:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = CloudSyncManager._recursive_find_first_key(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = CloudSyncManager._recursive_find_first_key(item, key)
                if found is not None:
                    return found
        return None

    def _extract_note_meta(self, response_json: dict) -> Dict[str, str]:
        note_internal_id = self._recursive_find_first_key(response_json, "note_id")
        note_token = self._recursive_find_first_key(response_json, "note_token")
        return {
            "note_id": str(note_internal_id or ""),
            "note_token": str(note_token or ""),
        }

    def _extract_entries(self, response_json: dict) -> List[dict]:
        entries = self._recursive_find_first_key(response_json, "note_content")
        if entries is None:
            return []
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except Exception:
                return []
        if not isinstance(entries, list):
            return []
        normalized = []
        for item in entries:
            if isinstance(item, dict):
                normalized.append({
                    "title": str(item.get("title", "")),
                    "content": str(item.get("content", "")),
                })
        return normalized

    @staticmethod
    def _entries_to_dict(entries: List[dict]) -> Dict[str, str]:
        result = {}
        for item in entries:
            title = item.get("title", "")
            if title:
                result[title] = item.get("content", "")
        return result

    def _encode_entries(self, encrypted_sync: str, encrypted_log: str) -> str:
        payload = [
            {"title": self.DATA_TITLE, "content": encrypted_sync},
            {"title": self.LOG_TITLE, "content": encrypted_log},
        ]
        return json.dumps(payload, ensure_ascii=False)

    def _read_local_payload(self, scope: dict) -> Dict[str, Any]:
        channels = self._load_channels_raw()
        selected_channels = self._filter_channels(channels, scope)
        return {
            "version": 2,
            "scope": scope,
            "meta": {
                "exported_at": int(time.time()),
                "device": self._device_meta(),
            },
            "channels": selected_channels,
            "edge_files": self._collect_edge_files(selected_channels),
        }

    def _merge_channels(self, local_channels: List[dict], remote_channels: List[dict]) -> List[dict]:
        merged: Dict[str, dict] = {}
        for item in local_channels:
            uuid = item.get("uuid", "")
            if uuid:
                merged[uuid] = copy.deepcopy(item)
        for item in remote_channels:
            uuid = item.get("uuid", "")
            if not uuid:
                continue
            if uuid not in merged:
                merged[uuid] = copy.deepcopy(item)
            else:
                local_time = int((merged[uuid] or {}).get("last_login_time", 0) or 0)
                remote_time = int((item or {}).get("last_login_time", 0) or 0)
                if remote_time >= local_time:
                    merged[uuid] = copy.deepcopy(item)
        return list(merged.values())

    def _write_local_channels(self, channels: List[dict]):
        channels_path = genv.get("FP_CHANNEL_RECORD", "")
        if not channels_path:
            raise ValueError("渠道账号文件路径不存在")
        with open(channels_path, "w", encoding="utf-8") as file:
            json.dump(channels, file)

    def _write_edge_files(self, edge_files: Dict[str, str]):
        work_dir = genv.get("FP_WORKDIR", "")
        for filename, content_base64 in (edge_files or {}).items():
            if filename not in self.EDGE_FILE_CANDIDATES:
                continue
            file_path = os.path.join(work_dir, filename)
            with open(file_path, "wb") as file:
                file.write(base64.b64decode(content_base64))

    def _append_access_log(self, logs: List[dict], action: str):
        logs.append(
            {
                "action": action,
                "at": int(time.time()),
                "device": self._device_meta(),
            }
        )
        if len(logs) > 200:
            del logs[:-200]

    def _load_remote(self, web_note: WebNote, master_key: str, allow_missing: bool = False) -> Dict[str, Any]:
        response_json = web_note.get(allow_missing=allow_missing)
        meta = self._extract_note_meta(response_json)
        entries = self._extract_entries(response_json)
        content_map = self._entries_to_dict(entries)

        remote_payload = None
        access_logs = []

        encrypted_sync = content_map.get(self.DATA_TITLE, "")
        encrypted_log = content_map.get(self.LOG_TITLE, "")

        if encrypted_sync:
            plain_sync = self.decrypt_text(encrypted_sync, master_key)
            remote_payload = json.loads(plain_sync)
        if encrypted_log:
            plain_log = self.decrypt_text(encrypted_log, master_key)
            parsed_log = json.loads(plain_log)
            if isinstance(parsed_log, list):
                access_logs = parsed_log

        return {
            "raw": response_json,
            "meta": meta,
            "payload": remote_payload,
            "logs": access_logs,
            "raw_logs":response_json.get("data",{}).get("log_list",[]) if isinstance(response_json, dict) else [],
        }

    def push(self, master_key: str, scope: dict, expire_time: int = 259200) -> Dict[str, Any]:
        self.ensure_master_key_valid(master_key)
        web_note = WebNote.from_master_key(master_key)

        local_payload = self._read_local_payload(scope)
        remote_data = self._load_remote(web_note, master_key, allow_missing=True)
        logs = remote_data.get("logs", []) or []
        self._append_access_log(logs, "push")

        encrypted_sync = self.encrypt_text(json.dumps(local_payload, ensure_ascii=False), master_key)
        encrypted_log = self.encrypt_text(json.dumps(logs, ensure_ascii=False), master_key)
        encoded_entries = self._encode_entries(encrypted_sync, encrypted_log)

        meta = remote_data.get("meta", {})
        if meta.get("note_id") and meta.get("note_token"):
            save_resp = web_note.update(meta["note_id"], meta["note_token"], encoded_entries, expire_time)
        else:
            save_resp = web_note.new(encoded_entries, expire_time)
        return {
            "success": True,
            "action": "push",
            "note_name": web_note.note_id,
            "channels_count": len(local_payload.get("channels", [])),
            "edge_files_count": len(local_payload.get("edge_files", {})),
            "result": save_resp,
        }

    def pull(self, master_key: str) -> Dict[str, Any]:
        self.ensure_master_key_valid(master_key)
        web_note = WebNote.from_master_key(master_key)
        remote_data = self._load_remote(web_note, master_key)

        remote_payload = remote_data.get("payload")
        if not remote_payload:
            raise ValueError("云端没有可用同步数据")

        local_channels = self._load_channels_raw()
        remote_channels = remote_payload.get("channels", [])
        merged_channels = self._merge_channels(local_channels, remote_channels)
        self._write_local_channels(merged_channels)
        self._write_edge_files(remote_payload.get("edge_files", {}))

        logs = remote_data.get("logs", []) or []
        self._append_access_log(logs, "pull")

        encrypted_sync = self.encrypt_text(json.dumps(remote_payload, ensure_ascii=False), master_key)
        encrypted_log = self.encrypt_text(json.dumps(logs, ensure_ascii=False), master_key)
        encoded_entries = self._encode_entries(encrypted_sync, encrypted_log)

        meta = remote_data.get("meta", {})
        if meta.get("note_id") and meta.get("note_token"):
            web_note.update(meta["note_id"], meta["note_token"], encoded_entries)

        return {
            "success": True,
            "action": "pull",
            "note_name": web_note.note_id,
            "merged_channels_count": len(merged_channels),
            "remote_channels_count": len(remote_channels),
        }

    def delete_remote(self, master_key: str) -> Dict[str, Any]:
        self.ensure_master_key_valid(master_key)
        web_note = WebNote.from_master_key(master_key)

        remote_data = web_note.get()
        meta = self._extract_note_meta(remote_data)
        if not meta.get("note_id") or not meta.get("note_token"):
            raise ValueError("未找到可删除的云同步记录")
        delete_response = web_note.delete(meta["note_id"], meta["note_token"])
        return {
            "success": True,
            "action": "delete",
            "note_name": web_note.note_id,
            "result": delete_response,
        }

    def fetch_access_logs(self, master_key: str) -> List[dict]:
        self.ensure_master_key_valid(master_key)
        web_note = WebNote.from_master_key(master_key)
        remote_data = self._load_remote(web_note, master_key)
        return remote_data.get("logs", []) or []

    def probe_remote(self, master_key: str) -> Dict[str, Any]:
        self.ensure_master_key_valid(master_key)
        web_note = WebNote.from_master_key(master_key)
        remote_data = self._load_remote(web_note, master_key, allow_missing=True)

        raw = remote_data.get("raw", {}) if isinstance(remote_data, dict) else {}
        status_value = str((raw or {}).get("status", "")) if isinstance(raw, dict) else ""
        if status_value == "2":
            return {
                "success": True,
                "exists": False,
                "note_name": web_note.note_id,
                "remote_channels_count": 0,
                "message": str((raw or {}).get("error", "云端同步记录不存在")) if isinstance(raw, dict) else "云端同步记录不存在",
            }

        payload = remote_data.get("payload", None)
        remote_channels = payload.get("channels", []) if isinstance(payload, dict) else []
        meta = remote_data.get("meta", {}) if isinstance(remote_data, dict) else {}
        has_meta = bool(meta.get("note_id") and meta.get("note_token")) if isinstance(meta, dict) else False

        return {
            "success": True,
            "exists": bool(has_meta or payload),
            "note_name": web_note.note_id,
            "remote_channels_count": len(remote_channels) if isinstance(remote_channels, list) else 0,
        }
