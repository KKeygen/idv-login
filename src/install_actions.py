import logging
import ntpath
import os
import shutil
import stat


_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_WINDOWS_DEVICES.update(
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
)


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _action_path(install_root, path):
    if not isinstance(path, str) or not path:
        raise ValueError("缺少相对路径")
    path = path.replace("\\", "/")
    if path.startswith("//") or ntpath.splitdrive(path)[0]:
        raise ValueError("不允许盘符或 UNC 路径")
    if path.startswith("/"):
        path = path[1:]
    parts = path.split("/")
    for part in parts:
        if (
            not part or part in (".", "..") or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_DEVICES
        ):
            raise ValueError("不安全或不明确的相对路径")
    root = os.path.realpath(os.path.abspath(install_root))
    if not os.path.isdir(root) or os.path.dirname(root) == root:
        raise ValueError("安装根目录无效或为磁盘根目录")
    target = os.path.abspath(os.path.join(root, *parts))
    if target == root or os.path.commonpath((root, target)) != root:
        raise ValueError("路径超出安装根目录")
    current = root
    for part in parts[:-1]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if _is_link(info):
            raise ValueError("路径包含符号链接或重解析点")
    return target


def apply_post_update_actions(install_root, install_script, logger=None):
    logger = logger or logging.getLogger(__name__)
    report = {"deleted": 0, "missing": 0, "skipped": 0, "warnings": []}

    def warn(message):
        report["skipped"] += 1
        report["warnings"].append(message)
        logger.warning(f"更新收尾: {message}")

    if not install_script:
        return report
    if not isinstance(install_script, dict):
        warn("install_script 格式无效，已跳过")
        return report
    actions = install_script.get("update_actions", [])
    if not isinstance(actions, list):
        warn("update_actions 格式无效，已跳过")
        return report
    for action in actions:
        if not isinstance(action, dict):
            warn("update_actions 项格式无效，已跳过")
            continue
        if action.get("scene") != 2:
            report["skipped"] += 1
            continue
        if action.get("type") != 1:
            warn("未支持的更新收尾操作类型，已跳过")
            continue
        relative_path = action.get("path")
        try:
            target = _action_path(install_root, relative_path)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                report["missing"] += 1
                continue
            if _is_link(info):
                os.unlink(target)
            elif stat.S_ISDIR(info.st_mode):
                if os.path.ismount(target):
                    raise ValueError("目标为挂载点")
                shutil.rmtree(target)
            elif stat.S_ISREG(info.st_mode):
                os.remove(target)
            else:
                raise ValueError("目标不是普通文件或目录")
            report["deleted"] += 1
            logger.info(f"更新收尾已删除: {relative_path}")
        except (OSError, ValueError, TypeError) as exc:
            warn(f"跳过 {relative_path!r}: {exc}")
    return report
