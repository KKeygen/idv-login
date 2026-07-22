"""Verified, immutable assets used by the Fever-hosted MPay runtime."""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil


MPAY_DLL_SHA256 = "02d27ad37b1421e4b97e90b889c4f8a59a71279dd08e090b6f8bb1821fb3225c"
MPAY_SKIN_SHA256 = "b12333267bcc512bc2a74363c3e628a87f9f322797cca27806c07f94ac058c1d"


def install_asset_path(filename: str) -> str:
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "assets", filename)
    )


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_install_asset(filename: str, expected_sha256: str) -> str:
    path = install_asset_path(filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"缺少安装资源: {path}")
    actual_sha256 = file_sha256(path)
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise RuntimeError(
            f"安装资源校验失败: {filename}, sha256={actual_sha256}"
        )
    return path


def verified_asset_shadow(
    source_path: str,
    expected_sha256: str,
    runtime_dir: str,
    identity: str,
) -> str:
    """Return a verified byte-identical asset at an identity-specific path.

    MPay pins its own module during init.  Windows can therefore create fresh
    process-global SDK state only when each post-Release init generation loads
    the same verified image from a distinct path.  Existing shadows are
    hash-checked on every activation; a bad on-disk image is never loaded.
    """
    source_path = os.path.abspath(source_path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"缺少安装资源: {source_path}")
    source_hash = file_sha256(source_path)
    if not hmac.compare_digest(source_hash, expected_sha256):
        raise RuntimeError(
            f"安装资源校验失败: {os.path.basename(source_path)}, "
            f"sha256={source_hash}"
        )

    identity_hash = hashlib.sha256(str(identity).encode("utf-8")).hexdigest()[:16]
    version_dir = os.path.join(
        os.path.abspath(runtime_dir), expected_sha256[:16]
    )
    os.makedirs(version_dir, exist_ok=True)
    target_path = os.path.join(version_dir, f"mpay-{identity_hash}.dll")
    if not os.path.exists(target_path):
        temporary_path = f"{target_path}.{os.getpid()}.tmp"
        try:
            shutil.copyfile(source_path, temporary_path)
            copied_hash = file_sha256(temporary_path)
            if not hmac.compare_digest(copied_hash, expected_sha256):
                raise RuntimeError(
                    f"MPay 运行时副本校验失败: sha256={copied_hash}"
                )
            os.replace(temporary_path, target_path)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    target_hash = file_sha256(target_path)
    if not hmac.compare_digest(target_hash, expected_sha256):
        raise RuntimeError(
            f"MPay 运行时副本校验失败: path={target_path}, "
            f"sha256={target_hash}"
        )
    return target_path
