#!/usr/bin/env python3
# coding=UTF-8
"""纯 Python 二进制 AndroidManifest.xml (AXML) 解析器。

需要从 manifest 拿到：
  · 根节点 package 属性
  · 根节点 android:versionCode / versionName
  · application/meta-data 里各 android:name -> android:value
    （honor 的 appid/cpid、360 的 QHOPENSDK_*、小米的 miGameAppId）

AXML 与 resources.arsc 共用 chunk 结构，这里只实现 manifest 需要的部分。
"""
from __future__ import annotations

import struct

# chunk type
RES_STRING_POOL = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
RES_XML_RESOURCE_MAP = 0x0180

# 属性 dataType
TYPE_NULL = 0x00
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

ANDROID_NS = 0x01000000
ATTR_NAME = 0x01010003        # android:name
ATTR_VALUE = 0x01010024       # android:value
ATTR_RESOURCE = 0x01010025    # android:resource
ATTR_VERSION_CODE = 0x0101021B
ATTR_VERSION_NAME = 0x0101021C


def _read_string_pool(data: bytes, offset: int):
    """解析 RES_STRING_POOL，返回字符串列表。"""
    string_count, _style_count, flags = struct.unpack_from("<III", data, offset + 8)
    strings_start = struct.unpack_from("<I", data, offset + 20)[0]
    is_utf8 = bool(flags & (1 << 8))

    offsets = struct.unpack_from("<%dI" % string_count, data, offset + 28)
    base = offset + strings_start
    out = []
    for off in offsets:
        pos = base + off
        if is_utf8:
            # u16 字符数（可能 2 字节）；u8 字节数（可能 2 字节）
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
    return out


class Manifest:
    """解析结果。

    Attributes:
        package:       应用包名
        version_code:  android:versionCode（int 或 None）
        version_name:  android:versionName（str 或 None）
        meta_data:     {android:name: value}，value 为字符串或 ("ref", 0x...)
        attrs:         根节点全部属性
    """

    def __init__(self):
        self.package = ""
        self.version_code = None
        self.version_name = None
        self.meta_data = {}
        self.attrs = {}


def _attr_key(resource_map, name_idx: int, ns: int, strings):
    """把属性还原成可读键名。

    Android 平台属性（android:xxx）在 resource_map 里有 ID，用它识别；
    自定义属性用字符串池里的原始名字。
    """
    if ns == ANDROID_NS and name_idx < len(resource_map):
        aid = resource_map[name_idx]
        return {
            ATTR_NAME: "name",
            ATTR_VALUE: "value",
            ATTR_RESOURCE: "resource",
            ATTR_VERSION_CODE: "versionCode",
            ATTR_VERSION_NAME: "versionName",
            None: "",
        }.get(aid) or ("android:0x%08x" % aid)
    if name_idx < len(strings):
        return strings[name_idx]
    return "@%d" % name_idx


def parse_manifest(data: bytes) -> Manifest:
    """解析二进制 AndroidManifest.xml。"""
    result = Manifest()
    if len(data) < 8:
        return result

    _type, header_size, _size = struct.unpack_from("<HHI", data, 0)
    offset = header_size

    strings = []
    resource_map = []
    current_tag = None
    current_attrs = {}

    while offset + 8 <= len(data):
        ctype, cheader, csize = struct.unpack_from("<HHI", data, offset)
        if not csize:
            break

        if ctype == RES_STRING_POOL:
            strings = _read_string_pool(data, offset)
        elif ctype == RES_XML_RESOURCE_MAP:
            count = (csize - cheader) // 4
            resource_map = list(struct.unpack_from("<%dI" % count, data, offset + cheader))
        elif ctype == RES_XML_START_ELEMENT:
            name_idx = struct.unpack_from("<I", data, offset + 20)[0]
            attr_start = struct.unpack_from("<H", data, offset + 24)[0]
            attr_size = struct.unpack_from("<H", data, offset + 26)[0]
            attr_count = struct.unpack_from("<H", data, offset + 28)[0]
            current_tag = strings[name_idx] if name_idx < len(strings) else ""
            current_attrs = {}

            for i in range(attr_count):
                pos = offset + 16 + attr_start + i * attr_size
                ns, aname, raw, _vsize, _res0, dtype, dval = struct.unpack_from(
                    "<IIIHBBI", data, pos
                )
                key = _attr_key(resource_map, aname, ns, strings)

                if dtype == TYPE_STRING and raw != 0xFFFFFFFF:
                    value = strings[raw] if raw < len(strings) else ""
                elif dtype == TYPE_REFERENCE:
                    value = ("ref", dval)
                elif dtype == TYPE_INT_BOOLEAN:
                    value = bool(dval)
                elif dtype in (TYPE_INT_DEC, TYPE_INT_HEX):
                    value = dval
                elif dtype == TYPE_NULL:
                    value = None
                else:
                    value = dval
                current_attrs[key] = value

            # 根节点 = manifest
            if not result.package and "package" in current_attrs:
                result.package = str(current_attrs.get("package") or "")
                result.attrs = dict(current_attrs)
                vc = current_attrs.get("versionCode")
                vn = current_attrs.get("versionName")
                if isinstance(vc, int):
                    result.version_code = vc
                if isinstance(vn, str):
                    result.version_name = vn

            # meta-data 在 application 下
            if current_tag == "meta-data":
                name = current_attrs.get("name")
                value = current_attrs.get("value")
                res = current_attrs.get("resource")
                if isinstance(name, str) and name:
                    if value is not None:
                        result.meta_data[name] = value
                    elif res is not None:
                        result.meta_data[name] = res

        offset += csize

    return result


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "AndroidManifest.xml"
    with open(path, "rb") as f:
        m = parse_manifest(f.read())
    print("package      :", m.package)
    print("versionCode  :", m.version_code)
    print("versionName  :", m.version_name)
    print("meta-data    : %d 项" % len(m.meta_data))
    for k in sorted(m.meta_data):
        v = m.meta_data[k]
        print("  %-42s = %r" % (k, v))
