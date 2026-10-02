"""Open-source host for the restricted, separately built MPay Wasm module."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import wasmtime

OPERATIONS = {"write_credentials": 1, "list_uids": 2, "delete_uid": 3, "rename_uid": 4, "project_accounts": 5, "cleanup_accounts": 6}
MAX_DB_BYTES = 32 * 1024 * 1024


class MpayWasm:
    def __init__(self, path: Path):
        raw = path.read_bytes()
        manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        if manifest["abi"] != 2 or hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
            raise ValueError("MPay Wasm artifact hash/ABI mismatch")
        config = wasmtime.Config()
        config.consume_fuel = True
        self.engine = wasmtime.Engine(config)
        # Validate raw Wasm. Never deserialize a downloaded native .cwasm image.
        self.module = wasmtime.Module(self.engine, raw)
        if self.module.imports:
            raise ValueError("MPay Wasm must have zero imports (including WASI)")
        if {e.name for e in self.module.exports} != {
            "memory", "alloc", "release", "invoke", "result_ptr", "result_len"
        }:
            raise ValueError("MPay Wasm exposes an unexpected interface")

    def invoke(self, function: str, database: bytes, **arguments) -> bytes:
        operation = OPERATIONS[function]
        if len(database) > MAX_DB_BYTES:
            raise ValueError("MPay database exceeds the supported size")
        args = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        if len(args) > 1024 * 1024:
            raise ValueError("MPay operation arguments exceed the supported size")
        # Each call gets fresh linear memory; plaintext stays inside this instance.
        store = wasmtime.Store(self.engine)
        store.set_limits(memory_size=128 * 1024 * 1024, instances=1, memories=1, tables=1)
        # Measured on the cloud-obfuscated codec: roughly 6,400 fuel/byte for
        # decrypt; writes also re-encrypt. Keep a bounded size-based allowance.
        store.set_fuel(100_000_000 + len(database) * 30_000)
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
        if not result_ptr or result_len > MAX_DB_BYTES:
            raise RuntimeError("MPay Wasm returned an invalid result")
        return bytes(memory.read(store, result_ptr, result_ptr + result_len))
