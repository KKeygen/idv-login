"""Open-source host for the restricted, separately built MPay Wasm module."""
from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

import wasmtime

OPERATIONS = {"rename_account": 4, "put_account": 5, "delete_account": 6, "list_accounts": 7, "native_head": 8}
MAX_DB_BYTES = 32 * 1024 * 1024


def _windows_cache_directory() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import ntsecuritycon as rights
        import pywintypes
        import win32con
        import win32file
        import win32security as security
        from win32com.shell import shell, shellcon
    except ImportError:
        return None

    administrators = "S-1-5-32-544"
    system = "S-1-5-18"
    trusted_installer = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    trusted = {administrators, system, trusted_installer}
    replace_path = (rights.DELETE | rights.FILE_DELETE_CHILD | rights.WRITE_DAC | rights.WRITE_OWNER |
                    rights.FILE_WRITE_ATTRIBUTES | rights.FILE_WRITE_EA | rights.GENERIC_ALL | rights.GENERIC_WRITE)
    write_cache = replace_path | rights.FILE_WRITE_DATA | rights.FILE_APPEND_DATA

    def protected(path, forbidden, *, include_inherited_children=False):
        info = path.lstat()
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        descriptor = security.GetFileSecurity(str(path), security.OWNER_SECURITY_INFORMATION |
                                               security.DACL_SECURITY_INFORMATION)
        owner = security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())
        acl = descriptor.GetSecurityDescriptorDacl()
        if owner not in trusted or acl is None:
            return False
        for i in range(acl.GetAceCount()):
            (kind, flags), mask, *details = acl.GetAce(i)
            if flags & win32con.INHERIT_ONLY_ACE and not include_inherited_children:
                continue
            if not mask & forbidden or kind == security.ACCESS_DENIED_ACE_TYPE:
                continue
            if kind != security.ACCESS_ALLOWED_ACE_TYPE or security.ConvertSidToStringSid(details[0]) not in trusted:
                return False
        return True

    try:
        program_data = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_COMMON_APPDATA, None, 0))
        directory = program_data / "idv-login-mpay-cache"
        if not program_data.is_absolute() or not all(protected(parent, replace_path) for parent in directory.parents):
            return None
        if not directory.exists():
            attrs = pywintypes.SECURITY_ATTRIBUTES()
            attrs.SECURITY_DESCRIPTOR = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", security.SDDL_REVISION_1)
            win32file.CreateDirectory(str(directory), attrs)
        if not protected(directory, write_cache, include_inherited_children=True):
            return None
        for name in ("compiled.cache", "config.json"):
            entry = directory / name
            try:
                if not protected(entry, write_cache) or not entry.is_file():
                    return None
            except FileNotFoundError:
                pass
        return directory
    except (OSError, pywintypes.error):
        return None


class MpayWasm:
    def __init__(self, path: Path):
        raw = path.read_bytes()
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        if manifest["abi"] != 6:
            raise ValueError("MPay Wasm ABI 6 is required; rebuild and promote the matching account primitive artifact")
        raw_hash = hashlib.sha256(raw).hexdigest()
        if raw_hash != manifest["sha256"]:
            raise ValueError("MPay Wasm artifact hash mismatch")
        config = wasmtime.Config()
        config.consume_fuel = True
        self.engine = wasmtime.Engine(config)
        directory = _windows_cache_directory()
        self.module = None
        if directory is not None:
            cache = directory / "compiled.cache"
            metadata = directory / "config.json"
            try:
                saved = json.loads(metadata.read_bytes())
                if isinstance(saved, dict) and saved.get("wasm_sha256") == raw_hash:
                    compiled = cache.read_bytes()
                    if hashlib.sha256(compiled).hexdigest() == saved.get("compiled_sha256"):
                        # Use exactly the verified bytes, never reopen the path for loading.
                        cached = wasmtime.Module.deserialize(self.engine, compiled)
                        self._check_interface(cached)
                        self.module = cached
            except (OSError, ValueError, wasmtime.WasmtimeError):
                pass
        rebuilt = self.module is None
        if rebuilt:
            self.module = wasmtime.Module(self.engine, raw)
            self._check_interface(self.module)
        if rebuilt and directory is not None:
            try:
                compiled = self.module.serialize()
                cache.write_bytes(compiled)
                metadata.write_text(json.dumps({
                    "wasm_sha256": raw_hash,
                    "compiled_sha256": hashlib.sha256(compiled).hexdigest(),
                }), encoding="utf-8")
            except (OSError, wasmtime.WasmtimeError):
                pass  # The validated module is already usable for this run.

    @staticmethod
    def _check_interface(module):
        if module.imports:
            raise ValueError("MPay Wasm must have zero imports (including WASI)")
        if {e.name for e in module.exports} != {
            "memory", "alloc", "release", "invoke", "result_ptr", "result_len"
        }:
            raise ValueError("MPay Wasm exposes an unexpected interface")

    def invoke(self, function: str, database: bytes, **arguments):
        operation = OPERATIONS[function]
        if len(database) > MAX_DB_BYTES:
            raise ValueError("MPay database exceeds the supported size")
        args = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        if len(args) > 1024 * 1024:
            raise ValueError("MPay operation arguments exceed the supported size")
        # Each call gets fresh linear memory; plaintext stays inside this instance.
        store = wasmtime.Store(self.engine)
        store.set_limits(memory_size=128 * 1024 * 1024, instances=1, memories=1, tables=1)
        # The hardened native core uses about 110,000 fuel/byte for reads and
        # 160,000 for writes. Fuel is an execution budget, not reserved memory.
        store.set_fuel(1_000_000_000 + len(database) * 2_000_000)
        instance = wasmtime.Instance(store, self.module, [])
        exports = instance.exports(store)
        memory = exports["memory"]
        db_ptr = exports["alloc"](store, len(database))
        arg_ptr = exports["alloc"](store, len(args))
        if not db_ptr or not arg_ptr:
            raise MemoryError("MPay Wasm input allocation failed")
        memory.write(store, database, db_ptr)
        memory.write(store, args, arg_ptr)
        status = exports["invoke"](store, operation, db_ptr, len(database), arg_ptr, len(args))
        if status:
            raise RuntimeError(f"MPay Wasm {function} failed: status={status}")
        result_ptr = exports["result_ptr"](store)
        result_len = exports["result_len"](store)
        if not result_ptr or result_len > MAX_DB_BYTES + (function == "put_account"):
            raise RuntimeError("MPay Wasm returned an invalid result")
        result = bytes(memory.read(store, result_ptr, result_ptr + result_len))
        if function == "put_account":
            if len(result) < 2 or result[0] not in (0, 1):
                raise RuntimeError("MPay Wasm returned an invalid put result")
            return bool(result[0]), result[1:]
        return result
