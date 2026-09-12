#!/usr/bin/env python3
# coding=UTF-8
"""纯 Python resources.arsc 解析器。

用途：把 AndroidManifest 里的资源引用（如 honor 的 `@ref` 形式 appid/cpid）
解析成实际值。只实现「按资源 ID 取字符串/整数」这一条路径。

资源 ID 结构：0xPPTTEEEE
    PP = package id
    TT = type id
    EEEE = entry id
"""
from __future__ import annotations

import struct

RES_STRING_POOL = 0x0001
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201

# Res_value dataType
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11

NO_ENTRY = 0xFFFFFFFF
FLAG_COMPLEX = 0x0001


def _read_string_pool(data: bytes, offset: int):
    """解析字符串池，返回 (strings, chunk_size)。"""
    if offset + 28 > len(data):
        return [], 0
    _ctype, _hsize, csize = struct.unpack_from("<HHI", data, offset)
    string_count = struct.unpack_from("<I", data, offset + 8)[0]
    flags = struct.unpack_from("<I", data, offset + 16)[0]
    strings_start = struct.unpack_from("<I", data, offset + 20)[0]
    is_utf8 = bool(flags & (1 << 8))

    if offset + 28 + string_count * 4 > len(data):
        return [], csize

    offsets = struct.unpack_from("<%dI" % string_count, data, offset + 28)
    base = offset + strings_start
    out = []
    for off in offsets:
        pos = base + off
        try:
            if is_utf8:
                pos += 2 if data[pos] & 0x80 else 1
                blen = data[pos]
                if blen & 0x80:
                    blen = ((blen & 0x7F) << 8) | data[pos + 1]
                    pos += 2
                else:
                    pos += 1
                out.append(data[pos:pos + blen].decode("utf-8", "replace"))
            else:
                length = struct.unpack_from("<H", data, pos)[0]
                if length & 0x8000:
                    length = ((length & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos + 2)[0]
                    pos += 4
                else:
                    pos += 2
                out.append(data[pos:pos + length * 2].decode("utf-16-le", "replace"))
        except Exception:
            out.append("")
    return out, csize


class ResourceTable:
    """resources.arsc 的最小实现：按资源 ID 取单值。"""

    def __init__(self, data: bytes):
        self.data = data
        self.global_strings = []
        # (pkg_id, type_id) -> {"entry_count", "entries_start", "offsets", "config_size", "base"}
        self._types = {}
        self._parse()

    def _parse(self):
        data = self.data
        if len(data) < 12:
            return

        # 文件头之后紧接着全局字符串池
        _ctype, _hsize, csize = struct.unpack_from("<HHI", data, 0)
        strings, pool_size = _read_string_pool(data, 12)
        self.global_strings = strings
        pos = 12 + pool_size if pool_size else 12 + csize

        while pos + 8 <= len(data):
            ctype, hsize, chunk_size = struct.unpack_from("<HHI", data, pos)
            if not chunk_size:
                break

            if ctype == RES_TABLE_PACKAGE:
                self._parse_package(pos, hsize, chunk_size)

            pos += chunk_size

    def _parse_package(self, base: int, header_size: int, chunk_size: int):
        data = self.data
        pkg_id = struct.unpack_from("<I", data, base + 8)[0]

        pos = base + header_size
        end = base + chunk_size

        # Package 内部还有各类型的字符串池，先跳过（用全局池即可）
        while pos + 8 <= end:
            ctype, hsize, csize = struct.unpack_from("<HHI", data, pos)
            if not csize:
                break

            if ctype == RES_TABLE_TYPE:
                self._parse_type(pkg_id, pos, hsize, csize)
            elif ctype == RES_STRING_POOL:
                pass  # 类型名的池，取 entry 时不需要

            pos += csize

    def _parse_type(self, pkg_id: int, base: int, header_size: int, chunk_size: int):
        data = self.data
        try:
            type_id = struct.unpack_from("<B", data, base + 8)[0]
            entry_count = struct.unpack_from("<I", data, base + 12)[0]
            entries_start = struct.unpack_from("<I", data, base + 16)[0]
        except struct.error:
            return

        offsets_base = base + header_size
        if offsets_base + entry_count * 4 > len(data):
            return

        offsets = struct.unpack_from("<%dI" % entry_count, data, offsets_base)
        # 多配置时保留首个（通常是默认配置）
        self._types.setdefault((pkg_id, type_id), {
            "entry_count": entry_count,
            "entries_start": base + entries_start,
            "offsets": offsets,
        })

    def resolve(self, res_id: int):
        """按资源 ID 取实际值；取不到返回 None。"""
        pkg_id = (res_id >> 24) & 0xFF
        type_id = (res_id >> 16) & 0xFF
        entry_id = res_id & 0xFFFF

        info = self._types.get((pkg_id, type_id))
        if not info or entry_id >= info["entry_count"]:
            return None

        offset = info["offsets"][entry_id]
        if offset == NO_ENTRY:
            return None

        pos = info["entries_start"] + offset
        data = self.data
        if pos + 8 > len(data):
            return None

        entry_size, entry_flags = struct.unpack_from("<HH", data, pos)
        if entry_flags & FLAG_COMPLEX:
            return None  # 复杂项（如 style/array）不支持

        value_pos = pos + entry_size
        if value_pos + 8 > len(data):
            return None

        v_type = data[value_pos + 3]
        v_data = struct.unpack_from("<I", data, value_pos + 4)[0]

        if v_type == TYPE_STRING:
            if v_data < len(self.global_strings):
                return self.global_strings[v_data]
            return None
        if v_type in (TYPE_INT_DEC, TYPE_INT_HEX):
            return v_data
        return None


if __name__ == "__main__":
    import sys

    with open(sys.argv[1], "rb") as f:
        table = ResourceTable(f.read())
    print("全局字符串数:", len(table.global_strings))
    print("类型块数:", len(table._types))
    if len(sys.argv) > 2:
        rid = int(sys.argv[2], 16)
        print("0x%08x ->" % rid, repr(table.resolve(rid)))
