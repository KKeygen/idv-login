# coding=UTF-8
"""从游戏 APK 提取网易渠道配置，用于更新 assets/cloudRes.json。

设计要点：
  · 用 remotezip 按需读取，不下载整个 APK（渠道包常达 2GB）
  · 纯 Python 解析 AndroidManifest.xml / resources.arsc，无需 jadx
  · 已支持渠道：xiaomi_app / huawei / myapp / honor_sdk / uc_platform /
               4399com / nearme_vivo / oppo / 360_assistant

用法：
    python tools/unpack.py <apk_url_or_path>
    # 不传参数时读取环境变量 APK_URL / APK_PATH

依赖：requests、remotezip（仅 CI/本地调试用，不是运行时依赖）
    pip install requests remotezip
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time

import requests

from axml import parse_manifest
from arsc import ResourceTable

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

CLOUDRES_API = "https://api.github.com/repos/KKeygen/idv-login/contents/assets/cloudRes.json"

# 支持自动提交到 cloudRes 的渠道
AUTO_UPDATE_CHANNELS = [
    "xiaomi_app", "huawei", "myapp", "honor_sdk", "uc_platform",
    "4399com", "nearme_vivo", "oppo", "360_assistant",
]


# ──────────────────────────────────────────────────────────────
# 渠道数据解密
# ──────────────────────────────────────────────────────────────
def validate(data, key):
    """解开 {channel}_data 里被轮转加密的字段。

    密钥表来自 UNISDK_SERVER_KEY：base64 解码后 124 字节，
    前 62 字节与后 62 字节构成映射。
    注意 UNISDK_SERVER_KEY / APP_CHANNEL 本身是明文，直接返回。
    """
    s_key = data["UNISDK_SERVER_KEY"]
    try:
        val = data[key]
        if key in ("UNISDK_SERVER_KEY", "APP_CHANNEL"):
            return val

        decode = base64.b64decode(s_key)
        if len(decode) != 124:
            logging.error("UNISDK_SERVER_KEY 长度异常: %d <> 124" % len(decode))
            return val

        first, second = decode[:62], decode[62:124]
        hash_map = {}
        for i in range(62):
            hash_map[(first[i] - 76) + second[i]] = second[i]

        char_array = list(val)
        for i, ch in enumerate(char_array):
            b = ord(ch)
            if b in hash_map:
                char_array[i] = chr(hash_map[b])
        return "".join(char_array)
    except Exception:
        logging.exception("解密字段 %s 失败" % key)
        return val


def load_channel_data(zf, name):
    """读取并解析 assets/{name}_data。文件可能是 base64 或明文 JSON。"""
    try:
        raw = zf.read("assets/%s_data" % name)
    except KeyError:
        return None

    text = raw.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(base64.b64decode(text).decode("utf-8"))
    except Exception:
        logging.warning("assets/%s_data 既不是明文 JSON 也不是 base64" % name)
        return None


# ──────────────────────────────────────────────────────────────
# 辅助读取
# ──────────────────────────────────────────────────────────────
def read_text(zf, path):
    """读取 zip 内的文本文件，不存在返回 None。"""
    try:
        return zf.read(path).decode("utf-8", "replace")
    except KeyError:
        return None


def parse_channel_infos(zf, channel_name):
    """从 assets/channel_infos_data 取该渠道的 sdk 版本。

    形如 {"main_channel": {"360_assistant_5": {"channel_id": "360_assistant",
                                               "version": "V2.4.0_816"}}}
    """
    text = read_text(zf, "assets/channel_infos_data")
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ""

    main = data.get("main_channel")
    # 老格式：channel_infos_data 直接是 {"channel":..., "version": "V..."}
    if not isinstance(main, dict):
        version = data.get("version")
        return str(version).lstrip("V") if version else ""

    for value in main.values():
        if isinstance(value, dict) and value.get("channel_id") == channel_name:
            version = value.get("version")
            if version:
                return str(version).lstrip("V")
    return ""


def parse_readme_version(zf, channel_name):
    """从 ReadMe_UniSDK.txt 取渠道 SDK 版本（形如 "360_assistant_5  2.4.0_816"）。"""
    text = read_text(zf, "ReadMe_UniSDK.txt")
    if not text:
        return ""
    for line in text.splitlines():
        m = re.match(r"%s_\d+\s+(\S+)" % re.escape(channel_name), line.strip())
        if m:
            return m.group(1)
    return ""


def meta_value(manifest, name, res_table=None):
    """取 manifest meta-data 的值，若是资源引用则解析。"""
    value = manifest.meta_data.get(name)
    if value is None:
        return ""
    if isinstance(value, tuple) and value and value[0] == "ref":
        resolved = res_table.resolve(value[1]) if res_table is not None else None
        return str(resolved) if resolved is not None else ""
    return str(value)


def attr_value(manifest, name, res_table=None):
    """取 manifest 根节点属性，若是资源引用则解析。"""
    value = manifest.attrs.get(name)
    if value is None:
        return ""
    if isinstance(value, tuple) and value and value[0] == "ref":
        resolved = res_table.resolve(value[1]) if res_table is not None else None
        return str(resolved) if resolved is not None else ""
    return str(value)


# ──────────────────────────────────────────────────────────────
# 渠道判定
# ──────────────────────────────────────────────────────────────
def detect_channel(package_name, names):
    """判定渠道，返回 (app_channel, data_file_channel)。

    data_file_channel 用于定位 assets/{name}_data；
    None 表示该渠道无 _data 文件（vivo/oppo 只需包名）。

    策略：
      1. vivo/oppo 没有 _data，只能按包名判定
      2. 其余渠道优先按 {channel}_data 文件名判定（网易生成的，最权威）
      3. 都没有时退回包名特征
    """
    data_files = {
        n.split("/")[1][:-5]
        for n in names
        if n.startswith("assets/") and n.endswith("_data")
    }

    # 1. vivo / oppo：无 _data
    if "nearme_vivo" in package_name or package_name.endswith(".vivo"):
        return "nearme_vivo", None
    if "nearme.gamecenter" in package_name or ".nearme." in package_name:
        # OPPO 渠道包名形如 com.netease.dwrg.nearme.gamecenter
        return "oppo", None

    # 2. 按 {channel}_data 文件判定
    for known in ("360_assistant", "4399com", "honor_sdk", "uc_platform",
                  "huawei", "myapp", "xiaomi_app"):
        if known in data_files:
            return known, known

    # 3. 退回包名特征
    if "qihoo" in package_name:
        return "360_assistant", "360_assistant"
    if "m4399" in package_name:
        return "4399com", "4399com"
    if "hihonor" in package_name or ".honor" in package_name:
        return "honor_sdk", "honor_sdk"
    if "aligames" in package_name or package_name.endswith(".uc"):
        return "uc_platform", "uc_platform"
    if "huawei" in package_name:
        return "huawei", "huawei"
    if "com.tencent" in package_name:
        return "myapp", "myapp"
    if "xiaomi" in package_name or package_name.endswith(".mi"):
        return "xiaomi_app", "xiaomi_app"

    return None, None


# ──────────────────────────────────────────────────────────────
# 各渠道参数提取
# ──────────────────────────────────────────────────────────────
def build_channel_data(zf, manifest, res_table, app_channel, my_data, package_name):
    """按渠道提取专属参数。"""
    logging.info("开始提取 [%s] 专属参数" % app_channel)

    if app_channel == "xiaomi_app":
        value = meta_value(manifest, "miGameAppId", res_table)
        if not value:
            logging.error("未找到 miGameAppId")
            return None
        logging.info("miGameAppId = %s" % value)
        return value

    if app_channel in ("nearme_vivo", "oppo"):
        # 只需渠道包名
        logging.info("%s 使用渠道包名: %s" % (app_channel, package_name))
        return package_name

    if app_channel == "huawei":
        text = read_text(zf, "assets/agconnect-services.json")
        if not text:
            logging.error("未找到 assets/agconnect-services.json")
            return None
        return json.loads(text).get("client")

    if app_channel == "myapp":
        text = read_text(zf, "assets/ysdkconf.ini")
        if not text:
            logging.error("未找到 assets/ysdkconf.ini")
            return None
        out = {"wx_appid": "", "channel": ""}
        for line in text.splitlines():
            line = line.strip()
            if "WX_APP_ID" in line:
                out["wx_appid"] = line.split("=")[-1]
            elif "OFFER_ID" in line or "QQ_APP_ID" in line:
                out["channel"] = line.split("=")[-1]
        logging.info("ysdkconf: wx_appid=%s channel=%s" % (out["wx_appid"], out["channel"]))
        return out

    if app_channel == "honor_sdk":
        out = {
            "app_id": meta_value(manifest, "com.hihonor.iap.sdk.appid", res_table),
            "cp_id": meta_value(manifest, "com.hihonor.iap.sdk.cpid", res_table),
            "package_name": package_name,
        }
        sdk_ver = parse_readme_version(zf, "honor_sdk")
        if sdk_ver:
            out["sdk_ver"] = sdk_ver
        logging.info("honor: app_id=%s cp_id=%s sdk_ver=%s"
                     % (out["app_id"], out["cp_id"], out.get("sdk_ver")))
        return out

    if app_channel == "uc_platform":
        app_id = validate(my_data, "APPID") if my_data else ""
        out = {
            "app_id": app_id,
            "uc_game_id": int(app_id) if str(app_id).isdigit() else app_id,
            "package_name": package_name,
        }
        version_code = attr_value(manifest, "versionCode", res_table)
        version_name = attr_value(manifest, "versionName", res_table)
        if version_code:
            out["version_code"] = int(version_code)
        if version_name:
            out["version_name"] = version_name

        sdk_ver = parse_channel_infos(zf, "uc_platform")
        if not sdk_ver:
            text = read_text(zf, "assets/ucgamesdk/config/sdk_info.txt")
            if text:
                m = re.search(r"version=([\d.]+)", text)
                if m:
                    sdk_ver = m.group(1)
        if sdk_ver:
            out["sdk_ver"] = sdk_ver
        logging.info("uc: app_id=%s sdk_ver=%s" % (out["app_id"], out.get("sdk_ver")))
        return out

    if app_channel == "4399com":
        out = {
            "app_key": validate(my_data, "APP_KEY") if my_data else "",
            "channel_id": "4399com",
            "package_name": package_name,
        }
        # 4399 的 sdk_ver 优先取 ReadMe（channel_infos 里的值可能带 "(3)" 后缀）
        sdk_ver = parse_readme_version(zf, "4399com")
        if not sdk_ver:
            sdk_ver = parse_channel_infos(zf, "4399com")
        if sdk_ver:
            out["sdk_ver"] = sdk_ver
        logging.info("4399: app_key=%s sdk_ver=%s" % (out["app_key"], out.get("sdk_ver")))
        return out

    if app_channel == "360_assistant":
        app_id = validate(my_data, "APPID") if my_data else ""
        app_key = validate(my_data, "APP_KEY") if my_data else ""
        # manifest 是权威值，不一致时以 manifest 为准
        meta_id = meta_value(manifest, "QHOPENSDK_APPID", res_table)
        meta_key = meta_value(manifest, "QHOPENSDK_APPKEY", res_table)
        if meta_id and meta_id != app_id:
            logging.warning("360 app_id 解密值 %s 与 manifest %s 不一致，采用 manifest"
                            % (app_id, meta_id))
            app_id = meta_id
        if meta_key and meta_key != app_key:
            logging.warning("360 app_key 解密值与 manifest 不一致，采用 manifest")
            app_key = meta_key

        out = {
            "app_id": app_id,
            "app_key": app_key,
            "protocol_app_channel": validate(my_data, "APP_CHANNEL") if my_data else "",
        }
        # 下划线必须保留：网易白名单按字面匹配，"2.4.0.816" 会 401 subcode 13
        sdk_ver = parse_channel_infos(zf, "360_assistant")
        if not sdk_ver:
            sdk_ver = parse_readme_version(zf, "360_assistant")
        if sdk_ver:
            out["sdk_ver"] = sdk_ver
        logging.info("360: app_id=%s sdk_ver=%s" % (out["app_id"], out.get("sdk_ver")))
        return out

    logging.error("没有 %s 的提取分支" % app_channel)
    return None


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────
def update_cloud_res(item, token=None):
    """把条目追加到 assets/cloudRes.json（通过 GitHub API）。"""
    token = token or os.getenv("GITHUB_TOKEN")
    headers = {"Authorization": "token %s" % token} if token else {}

    resp = requests.get(CLOUDRES_API, headers=headers, timeout=30)
    file_info = resp.json()
    sha = file_info["sha"]
    data = json.loads(base64.b64decode(file_info["content"]).decode())

    data["lastModified"] = int(time.time())
    data["data"].append(item)

    payload = {
        "message": "Live update for %s-%s" % (item["game_id"], item["app_channel"]),
        "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
        "sha": sha,
    }
    result = requests.put(CLOUDRES_API, headers=headers, json=payload, timeout=30)
    print(result.json())


def get_netease_game_info(source, token=None):
    """从 APK（本地路径或 URL）提取渠道配置。"""
    import zipfile

    from remotezip import RemoteZip

    if re.match(r"^https?://", source):
        logging.info("远程读取 APK: %s" % source[:100])
        ctx = RemoteZip(source, headers={"User-Agent": "Mozilla/5.0"})
    else:
        logging.info("本地读取 APK: %s" % source)
        ctx = zipfile.ZipFile(source)

    with ctx as zf:
        names = zf.namelist()
        logging.info("zip 条目数: %d" % len(names))
        if "AndroidManifest.xml" not in names:
            raise RuntimeError("不是有效的 APK：缺少 AndroidManifest.xml")

        manifest = parse_manifest(zf.read("AndroidManifest.xml"))
        logging.info("包名: %s  versionCode: %s"
                     % (manifest.package, manifest.version_code))

        # 只在确实有资源引用时才拉 resources.arsc
        has_ref = any(
            isinstance(v, tuple) and v and v[0] == "ref"
            for v in list(manifest.meta_data.values()) + list(manifest.attrs.values())
        )
        res_table = None
        if has_ref and "resources.arsc" in names:
            logging.info("检测到资源引用，解析 resources.arsc")
            res_table = ResourceTable(zf.read("resources.arsc"))

        app_channel, data_file_channel = detect_channel(manifest.package, names)
        if not app_channel:
            logging.error("无法判定渠道，请补充包名特征")
            return None
        logging.info("判定渠道: %s (数据文件: %s)" % (app_channel, data_file_channel))

        my_data = load_channel_data(zf, data_file_channel) if data_file_channel else None

        log_key = ""
        game_id = ""
        if my_data:
            log_key = validate(my_data, "JF_LOG_KEY")
            game_id = validate(my_data, "JF_GAMEID")
            # 文件内的 APP_CHANNEL 可能与文件名不同（360 为 allysdk.360_assistant）
            inner_channel = validate(my_data, "APP_CHANNEL")
            if inner_channel and app_channel != "360_assistant":
                logging.info("数据文件覆盖渠道标识: %s" % inner_channel)
                app_channel = inner_channel
            logging.info("JF_GAMEID=%s  JF_LOG_KEY=%s" % (game_id, log_key))
        else:
            logging.info("该渠道无需 _data 文件，game_id/log_key 留空由云端补全")

        channel_data = build_channel_data(
            zf, manifest, res_table, app_channel, my_data, manifest.package
        )
        if channel_data is None:
            logging.error("参数提取失败")
            return None

    result = {
        "package_name": manifest.package,
        "app_channel": app_channel,
        "log_key": log_key,
        "game_id": game_id,
        app_channel: channel_data,
    }
    print(json.dumps(result, ensure_ascii=False))
    print(json.dumps(result))

    if app_channel in AUTO_UPDATE_CHANNELS:
        update_cloud_res(result, token)
    else:
        logging.info("渠道 %s 不在自动更新名单内，仅打印结果" % app_channel)
    return result


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else (os.getenv("APK_URL") or os.getenv("APK_PATH"))
    if not src:
        print("用法: python tools/unpack.py <apk_url_or_path>")
        sys.exit(2)
    get_netease_game_info(src)
