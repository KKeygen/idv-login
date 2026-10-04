import base64
import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional
from urllib.parse import quote

from envmgr import genv


OPPO_WEBVIEW_UA = (
    "Mozilla/5.0 (Linux; Android 12.0.0; MI=8 Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 "
    "Safari/537.36 DayNight/0 ColorOSVersion/ language/zh languageTag/zh-CN "
    "locale/zh_CN timeZone/Asia/Shanghai model/MI appPackageName/com.heytap.htms "
    "appVersion/2012701 foldMode/ largeScreen/false displayWidth/1600 displayHeight/864 "
    "realScreenWidth/1600 realScreenHeight/900 navHeight/72 isThird/1 X-BusinessSystem/other "
    "hardwareType/Mobile isMagicWindow/0 isTalkBackState/0 JSBridge/2 ClientType/UserCenter "
    "usercenter/2.1.27_efe796d_250819_outgoing_dom_f91d222_250808 WebFitMethod/1 switchHost/1 "
    "localstorageEncrypt/1 deepThemeColor/rgba(0,189,19,1.0) themeColor/rgba(0,189,19,1.0) "
    "isPanel/0 regionCode/CN Business/account"
)


# 默认登录入口：实际登录页通常会在该域下跳转/渲染
OPPO_UC_CLIENT_DOMAIN = "https://muc.heytap.com/account-external-sdk/login"


def _get_or_create_oppo_guid_uuid() -> str:
    """读取/生成 Oppo GUID 的 uuid(no '-') 部分，并写入 genv 的持久缓存。

    约定：不再把 OPPO 专用字段写回 FAKE_DEVICE/fakeDevice.json，避免污染“通用设备画像”。
    持久化键：oppo.guid_uuid

    注意：该函数必须定义在 DEFAULT_CONSTS 初始化之前，因为 OppoNativeConsts.__post_init__ 会调用它。
    """

    key = "oppo.guid_uuid"
    raw = str(genv.get(key, "") or "").strip()
    if raw:
        return raw.replace("-", "")

    new_uuid = uuid.uuid4().hex
    genv.set(key, new_uuid, True)
    return new_uuid


@dataclass(frozen=True)
class OppoDeviceProfile:
    """与“手机/设备”强相关的画像信息（聚合后再分发到 RAW_*）。"""

    model: str = "MI"
    brand: str = "Xiaomi"
    hardware_type: str = "Mobile"
    screen_wd: int = 1600
    screen_ht: int = 900
    devicetype: str = "Mobile"

    os_version: str = "12.0.0"
    android_version: str = "31"
    os_version_code: str = "31"
    os_build_time: int = 1752315442000
    rpname: str = "dipper"
    rotaver: str = "0"
    rom_build_display: str = "V417IR release-keys"

    # ====== envParam(SysInfo/DevInfo/HardInfo/OtherInfo) 相关默认值 ======
    sys_os_version: str = "V10.0.11.0"
    sec_version: str = "2018-10-01"
    bootloader_version: str = "unknown"
    usb_status: bool = False

    build_id: str = "1.0.0.0"
    hw_name: str = "Xiaomi"

    screen_dpi: int = 240
    cpu_id: str = (
        "fp asimd aes pmull sha1 sha2 crc32 atomics,"
        "ARMv8 processor rev 1 (aarch64),"
        "8,placeholder,null"
    )
    cpu_type: str = "arm64-v8a,armeabi-v7a,armeabi"
    bt_name: str = "NOP"

    battery_status: str = "charging"
    battery_present: bool = True
    battery_health: int = 2
    other_sdk_version: str = "1.1.0"

    nfc: bool = False
    lsd: bool = False

    def build_ext_system(self, *, ac_version: int = 200202, carrier: str = "CHINA+MOBILE") -> str:
        """生成 Ext-System 头（目前按抓包得到的稳定格式拼接）。"""

        # 结构形如：model/osVersion/2/brand//CARRIER/acVersion/
        return f"{self.model}/{self.os_version}/2/{self.brand}//{carrier}/{ac_version}/"


@dataclass(frozen=True)
class OppoNativeConsts:
    PKG_ACCOUNT_SDK: str = "com.oplus.account.open.sdk"
    PKG_HOST: str = "com.heytap.htms"
    # 注意：deviceId 初始留空；若本地已有旧 loginResp 且其中 deviceId 非空，则优先使用旧值。
    DEVICE_ID: str = ""
    # GUID = PKG_HOST + (uuid 去掉 '-')；uuid 持久化在 genv 缓存键 oppo.guid_uuid，整个渠道只生成一次。
    GUID: str = ""
    APP_VERSION: int = 2012701
    APP_VERSION_STR: str = "2012701"

    DEVICE: OppoDeviceProfile = field(default_factory=OppoDeviceProfile)

    COUNTRY: str = "CN"
    MASK_REGION: str = "CN"
    TIME_ZONE: str = "Asia/Shanghai"
    LOCALE: str = "zh_CN"
    LANGUAGE: str = "zh"
    LANGUAGE_TAG: str = "zh-CN"
    COLOR_OS_VERSION: str = "0"

    # 这些字段在网页登录时通常为空，登录完成后会由页面/接口返回新的 token
    SSOID: str = ""
    TOKEN: str = ""

    BIZK: str = "3cd48b0c781835478b0a1783a9eff0c9"
    APP_ID: str = "31288517"
    PKG_NAME_SIGN: str = "00e7ec6745698936072925f64fc2a3e8"

    RAW_X_DEVICE: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_APP: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_CONTEXT: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_SDK: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_SYS: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_SYSTEM: Dict[str, Any] = None  # type: ignore[assignment]
    RAW_X_DEVICE_INFO: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        # dataclass frozen + default None -> 在 __post_init__ 用 object.__setattr__ 填充
        guid_uuid = _get_or_create_oppo_guid_uuid()
        object.__setattr__(self, "GUID", f"{self.PKG_HOST}{guid_uuid}")

        device = self.DEVICE

        object.__setattr__(
            self,
            "RAW_X_DEVICE",
            {"wd": device.screen_wd, "ht": device.screen_ht, "devicetype": device.devicetype},
        )
        object.__setattr__(
            self,
            "RAW_X_APP",
            {
                "ucVersion": 0,
                "ucPackage": "",
                "acVersion": 200202,
                "acPackage": self.PKG_ACCOUNT_SDK,
                "payVersion": 0,
                "appPackage": self.PKG_HOST,
                "deviceId": self.DEVICE_ID,
                "appVersion": self.APP_VERSION,
                "instantVersion": "",
                "hostPackage": self.PKG_HOST,
                "hostVersion": self.APP_VERSION,
                "fromHT": "true",
                "overseaClient": "false",
                "foldMode": "",
            },
        )
        object.__setattr__(
            self,
            "RAW_X_CONTEXT",
            {
                "country": self.COUNTRY,
                "maskRegion": self.MASK_REGION,
                "timeZone": self.TIME_ZONE,
                "locale": self.LOCALE,
            },
        )
        object.__setattr__(
            self,
            "RAW_X_SDK",
            {
                "sdkName": "UCBasic",
                "sdkBuildTime": "2024-01-16 14:58:22",
                "sdkVersionName": "2.0.8",
                "headerRevisedVersion": 1,
            },
        )
        object.__setattr__(
            self,
            "RAW_X_SYS",
            {
                "romVersion": self.COLOR_OS_VERSION,
                "osVersion": device.os_version,
                "androidVersion": device.android_version,
                "osVersionCode": device.os_version_code,
                "osBuildTime": device.os_build_time,
                "uid": "0",
                "utype": "P",
                "betaEnv": False,
                "rpname": device.rpname,
                "rotaver": device.rotaver,
                "guid": f"{self.PKG_HOST}{guid_uuid}",
            },
        )
        object.__setattr__(
            self,
            "RAW_X_SYSTEM",
            {
                "uid": "0",
                "usn": "",
                "utype": "P",
                "rpname": device.rpname,
                "rotaver": device.rotaver,
            },
        )
        object.__setattr__(
            self,
            "RAW_X_DEVICE_INFO",
            {
                "model": device.model,
                "ht": device.screen_ht,
                "wd": device.screen_wd,
                "brand": device.brand,
                "hardwareType": device.hardware_type,
                "nfc": device.nfc,
                "lsd": device.lsd,
            },
        )


def prefer_device_id_from_login_resp(
    login_resp: Any,
    base: Optional[OppoNativeConsts] = None,
) -> OppoNativeConsts:
    """若 loginResp.deviceId 非空，则返回一个替换了 DEVICE_ID 的 consts；否则返回 base。"""

    if base is None:
        base = DEFAULT_CONSTS

    if not isinstance(login_resp, dict):
        return base
    device_id = str(login_resp.get("deviceId") or "").strip()
    if not device_id:
        return base
    return replace(base, DEVICE_ID=device_id)


DEFAULT_CONSTS = OppoNativeConsts()


def _json_dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def url_encode_json(obj: Any) -> str:
    # JS encodeURIComponent
    return quote(_json_dumps_compact(obj), safe="")


def base64_encode_json(obj: Any) -> str:
    # JS btoa(JSON.stringify(...))；这里直接对 UTF-8 bytes 做 base64
    return base64.b64encode(_json_dumps_compact(obj).encode("utf-8")).decode("ascii")


def build_vip_header_json(consts: OppoNativeConsts = DEFAULT_CONSTS) -> Dict[str, str]:
    device = consts.DEVICE
    ac_version = int((consts.RAW_X_APP or {}).get("acVersion") or 200202)
    return {
        "Ext-App": f"/{consts.APP_VERSION_STR}/{consts.PKG_HOST}",
        "Ext-Instant-Version": "",
        "Ext-Mobile": f"//{consts.DEVICE_ID}/1/{consts.COUNTRY}",
        "Ext-System": device.build_ext_system(ac_version=ac_version),
        "X-APP": url_encode_json(consts.RAW_X_APP),
        "X-BIZ-PACKAGE": consts.PKG_HOST,
        "X-BIZ-VERSION": "2.1.27_efe796d_250819_outgoing_dom",
        "X-BusinessSystem": "other",
        "X-Client-Country": consts.COUNTRY,
        "X-Client-Device": consts.DEVICE_ID,
        "X-Client-GUID": consts.GUID,
        "X-Client-HTOSVersion": consts.COLOR_OS_VERSION,
        "X-Client-Locale": consts.LOCALE,
        "X-Client-Timezone": consts.TIME_ZONE,
        "X-Client-package": consts.PKG_ACCOUNT_SDK,
        "X-Context": url_encode_json(consts.RAW_X_CONTEXT),
        "X-Device": base64_encode_json(consts.RAW_X_DEVICE),
        "X-Device-Info": url_encode_json(consts.RAW_X_DEVICE_INFO),
        "X-From-HT": "true",
        "X-Op-Upgrade": "true",
        "X-SDK": url_encode_json(consts.RAW_X_SDK),
        "X-SDK-TYPE": "open",
        "X-SDK-VERSION": "2.2.2",
        "X-Safety": _json_dumps_compact(
            {
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
        ),
        "X-Security": _json_dumps_compact(
            {
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
        ),
        "X-Sys": url_encode_json(consts.RAW_X_SYS),
        "X-Sys-TalkBackState": "false",
        "X-System": base64_encode_json(consts.RAW_X_SYSTEM),
        "accept-language": consts.LANGUAGE_TAG,
    }
