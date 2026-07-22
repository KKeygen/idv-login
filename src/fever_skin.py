"""Build the hosted MPay skin from the verified runtime skin."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
import tempfile
import zipfile

from fever_assets import file_sha256


_CACHE_LAYOUT_VERSION = "zip-v6"
_REQUIRED_SKIN_VERSION = "41"

_WINDOW_MARKER = '<Window size="360,480"'
_SWITCH_MARKER = (
    '<TabLayout name="switch" width="360" height="480" mouse="false">'
)
_QRCODE_MARKER = (
    '<VerticalLayout name="qrcodetab" pagename="qrcode_login" mouse="false">'
)


def _channel_accounts_link() -> str:
    text = (
        "<a idvlogin://fever-channel-accounts/>"
        "<c 5><u>我要登录已保存的渠道服账号</u></c></a>"
    )
    return (
        '                <Text name="idvlogin_channel_accounts" float="true" '
        'pos="4,500,356,544" width="352" height="44" align="center" '
        'valign="center" showhtml="true" font="10" text="'
        + text
        + '" />\n'
    )


def _element_at(layout: str, marker: str) -> str:
    """Return one balanced element from the pinned MPay layout.

    MPay's layout language permits HTML-like markup inside attribute values,
    so a general XML parser cannot read the file.  The QR page itself has no
    such attributes and can be balanced safely without rewriting the archive.
    """
    start = layout.find(marker)
    if start < 0:
        raise ValueError("无法识别基础 skin 的二维码布局")
    depth = 0
    token_re = re.compile(r"<!--.*?-->|<[^>]+>", re.DOTALL)
    for match in token_re.finditer(layout, start):
        token = match.group(0)
        if token.startswith("<!--") or token.startswith("<?"):
            continue
        if token.startswith("</"):
            depth -= 1
            if depth == 0:
                return layout[start : match.end()]
        elif not token.endswith("/>"):
            depth += 1
    raise ValueError("基础 skin 的二维码布局没有闭合")


def _append_to_element(element: str, child: str) -> str:
    closing = element.rfind("</VerticalLayout>")
    if closing < 0:
        raise ValueError("基础 skin 的二维码布局没有闭合")
    return element[:closing] + child + element[closing:]


def _patch_single_column(layout: str, qrcode_page: str) -> str:
    required_markers = (
        _WINDOW_MARKER,
        _SWITCH_MARKER,
        'name="_dialog_size_for_not_recentuser"',
    )
    if any(marker not in layout for marker in required_markers):
        raise ValueError("无法识别基础 skin 的单栏登录布局")

    layout = layout.replace(
        qrcode_page,
        _append_to_element(qrcode_page, _channel_accounts_link()),
        1,
    )
    layout = layout.replace(_WINDOW_MARKER, '<Window size="360,560"', 1)
    layout = layout.replace(
        _SWITCH_MARKER,
        '<TabLayout name="switch" width="360" height="560" mouse="false">',
        1,
    )
    layout = layout.replace(
        '<Control name="versionbtn" float="true" pos="0,450"',
        '<Control name="versionbtn" float="true" pos="0,530"',
        1,
    )
    layout = layout.replace(
        '<Label name="versionlabel" float="true" pos="15,455"',
        '<Label name="versionlabel" float="true" pos="15,535"',
        1,
    )
    layout = layout.replace(
        'name="_dialog_size_for_not_recentuser" float="true" padding="0,0,360,480"',
        'name="_dialog_size_for_not_recentuser" float="true" padding="0,0,360,560"',
        1,
    )
    return layout


def _patch_login_layout(layout: str) -> str:
    if 'name="idvlogin_channel_accounts"' in layout:
        raise ValueError("基础 skin 已包含 idv-login 渠道账号入口")
    qrcode_page = _element_at(layout, _QRCODE_MARKER)
    return _patch_single_column(layout, qrcode_page)


def _manifest_entry(digest, name: str, data: bytes) -> None:
    encoded_name = name.encode("utf-8")
    digest.update(struct.pack("<I", len(encoded_name)))
    digest.update(encoded_name)
    digest.update(struct.pack("<Q", len(data)))
    digest.update(data)


def _archive_manifest(path: str) -> str:
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            _manifest_entry(digest, info.filename, archive.read(info.filename))
    return digest.hexdigest()


def prepare_mpay_skin(
    base_skin_zip: str,
    cache_root: str,
    expected_base_sha256: str,
) -> str:
    """Return a cached hosted-login ZIP for MPay ``SetResPath``.

    The ZIP resource mode validates ``version.txt`` before accepting the skin.
    Only ``layout/login.xml`` is changed; every other entry and its ZIP metadata
    are copied from the verified base archive.
    """
    base_skin_zip = os.path.abspath(base_skin_zip)
    if not zipfile.is_zipfile(base_skin_zip):
        raise FileNotFoundError(f"未找到有效的 mpay 基础皮肤: {base_skin_zip}")
    actual_base_sha256 = file_sha256(base_skin_zip)
    if not hmac.compare_digest(actual_base_sha256, expected_base_sha256):
        raise RuntimeError(
            f"mpay 基础皮肤校验失败: sha256={actual_base_sha256}"
        )
    fingerprint = actual_base_sha256[:16]
    with zipfile.ZipFile(base_skin_zip) as archive:
        try:
            original_bytes = archive.read("layout/login.xml")
            version_bytes = archive.read("version.txt")
        except KeyError as error:
            raise ValueError(f"mpay 基础皮肤缺少文件: {error.args[0]}") from error
    version = version_bytes.decode("ascii").strip()
    if version != _REQUIRED_SKIN_VERSION:
        raise ValueError(
            f"mpay skin 版本必须为 {_REQUIRED_SKIN_VERSION}，当前为 {version}"
        )
    cache_root = os.path.abspath(cache_root)
    target_dir = os.path.join(cache_root, fingerprint, _CACHE_LAYOUT_VERSION)

    original = original_bytes.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in original else "\n"
    normalized = original.replace("\r\n", "\n")
    patched = _patch_login_layout(normalized).replace("\n", newline)
    patched_bytes = patched.encode("utf-8")

    expected_manifest_digest = hashlib.sha256()
    with zipfile.ZipFile(base_skin_zip) as archive:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "layout/login.xml":
                data = patched_bytes
            _manifest_entry(expected_manifest_digest, info.filename, data)
    expected_manifest = expected_manifest_digest.hexdigest()

    os.makedirs(target_dir, exist_ok=True)
    target_zip = os.path.join(target_dir, f"hosted-{expected_manifest[:16]}.zip")
    if os.path.isfile(target_zip):
        try:
            if hmac.compare_digest(
                _archive_manifest(target_zip), expected_manifest
            ):
                return target_zip
        except (OSError, zipfile.BadZipFile, KeyError):
            pass
        os.remove(target_zip)

    fd, temp_zip = tempfile.mkstemp(
        prefix=".hosted-", suffix=".zip", dir=target_dir
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(base_skin_zip) as source_archive, zipfile.ZipFile(
            temp_zip, "w"
        ) as target_archive:
            for info in source_archive.infolist():
                data = source_archive.read(info.filename)
                if info.filename == "layout/login.xml":
                    data = patched_bytes
                target_archive.writestr(info, data)
        os.replace(temp_zip, target_zip)
        if not hmac.compare_digest(_archive_manifest(target_zip), expected_manifest):
            os.remove(target_zip)
            raise RuntimeError("生成的 mpay skin 内容校验失败")
    finally:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
    return target_zip
