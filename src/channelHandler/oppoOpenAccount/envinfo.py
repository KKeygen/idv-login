#仅供技术交流，请下载后24小时内删除，禁止商用！如有侵权请联系仓库维护者删除！谢谢！
import json
import time
import urllib.parse
from typing import Any, Dict, Optional

from channelHandler.oppoLogin.consts import OppoNativeConsts, DEFAULT_CONSTS


DEVICE_SECURITY_HEADER_OBJ: Dict[str, Any] = {
    "imei": "",
    "imei1": "",
    "mac": "",
    "serialNum": "",
    "serial": "",
    "wifissid": "",
    "hasPermission": False,
    "deviceName": "",
    "marketName": "",
}


# TODO: 死代码。该明文头仅用于构造加密请求的 X-Security，而 OPPO 已改为纯明文请求
# （见 oppoOpenAccount/client.py::post_plain_json），故当前无任何调用方。
# 暂保留，确认无需恢复加密后再删除。
def build_device_security_header_plain() -> str:
    """对应 DeviceSecurityHeader.getDeviceSecurityHeader 的明文（此阶段按 mockNative.js 全空实现）。"""

    return json.dumps(DEVICE_SECURITY_HEADER_OBJ, ensure_ascii=False, separators=(",", ":"))

#
def build_env_param_minimal(consts: OppoNativeConsts = DEFAULT_CONSTS) -> str:
    """构建 envParam（percent-encoded JSON），结构对齐 authro 示例。

    固定项直接硬编码；可变项(curTime/upTime/activeTime)运行时生成；其它默认值来自 consts.DEVICE。
    """

    device = consts.DEVICE
    now_ms = int(time.time() * 1000)
    uptime_ms = int(time.monotonic() * 1000)

    payload: Dict[str, Any] = {
        "SysInfo": {
            "osVersion": device.sys_os_version,
            "romVersion": device.rom_build_display,
            "apiVersion": int(device.android_version) if str(device.android_version).isdigit() else device.android_version,
            "secVersion": device.sec_version,
            "bootloaderVersion": device.bootloader_version,
            "usbStatus": False,
            "curTime": now_ms,
            "upTime": uptime_ms,
            "activeTime": uptime_ms,
        },
        "DevInfo": {
            "buildID": device.build_id,
            "model": device.model,
            "product": device.rpname,
            "brand": device.brand,
            "hwName": device.hw_name,
            "platform": "phone",
        },
        "NetInfo": {
            "networkType": "",
            "cellIP": "",
            "isVpn": False,
            "vpnIP": "",
        },
        "EnvInfo": {
            "isRoot": False,
            "isVirtual": False,
            "vmApp": "",
            "hookFrame": False,
            "hookMethods": False,
            "isFileExist": False,
            "OSisDebuggable": False,
            "roSecure": 1,
        },
        "HardInfo": {
            "screenSize": f"{device.screen_wd},{device.screen_ht}",
            "screenDpi": device.screen_dpi,
            "cpuID": device.cpu_id,
            "cpuType": device.cpu_type,
            "btName": device.bt_name,
            "btMac": None,
        },
        "OtherInfo": {
            "batteryStatus": device.battery_status,
            "batteryPresent": device.battery_present,
            "batteryHealth": device.battery_health,
            "sdkVersion": device.other_sdk_version,
        },
    }

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return urllib.parse.quote(raw, safe="")


def build_env_info_pkg(
    app_id: str,
    device_id: str,
    pkg_name: str,
    pkg_name_sign: str,
    env_param: Optional[str] = None,
) -> str:
    obj = {
        "appId": app_id,
        "deviceId": device_id or "",
        "envParam": env_param or "",
        "pkgName": pkg_name,
        "pkgNameSign": pkg_name_sign,
    }
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
