# coding=UTF-8
"""360（奇虎）渠道常量。"""
from __future__ import annotations

# ── 协议端点 ────────────────────────────────────────────────
PASSPORT_HOST = "https://login.360.cn/api.php"
WEB_USER_URL = "https://passport.360.cn/api.php"

# ── 密钥组选择 ──────────────────────────────────────────────
# SDK 提供两套密钥（支付专用 / 默认），依据 SDK 类型是否为纯支付型选择。
# 本渠道类型为 SOCIAL_PAY（含 SOCIAL）→ 非纯支付 → 用默认组。
SDK_TYPE = "SOCIAL_PAY"
_SDK_TYPE_UPPER = SDK_TYPE.upper()
PAY_MODE = ("PAY" in _SDK_TYPE_UPPER) and ("SOCIAL" not in _SDK_TYPE_UPPER)

DES_KEY = "2a9u8q4y" if PAY_MODE else "u72g3fds"
SIGN_SALT = "78ae0wq1h" if PAY_MODE else "13a22fc0a"
FROM_PREFIX = "mpc_open_ms_" if PAY_MODE else "mpc_yxhezi_and_"

# ── 版本号 ──────────────────────────────────────────────────
SDK_VERSION_NAME = "2.4.0"
SDK_VERSION_CODE = 816
PLUGIN_VERSION_CODE = 0
# 请求参数 v 的取值，形如 Qhopensdk-2.4.0-816-0
SDK_V = "Qhopensdk-%s-%d-%d" % (SDK_VERSION_NAME, SDK_VERSION_CODE, PLUGIN_VERSION_CODE)

# 上报给网易 uni_sauth 的 sdk_version。
# 来源：APK 内 ReadMe_UniSDK.txt 的 "360_assistant_5    2.4.0_816"。
# ⚠ 必须保留下划线的原始写法：NetEase 白名单按字面匹配，
#   写成 "2.4.0.816" 会返回 401 subcode 13 (sdk_version whitelist not exists)。
#   实测 "2.4.0_816" → code 200 / subcode 0 / status ok。
UNISDK_SDK_VERSION = "%s_%d" % (SDK_VERSION_NAME, SDK_VERSION_CODE)

# ── 接口 method 名 ─────────────────────────────────────────
METHOD_LOGIN = "UserIntf.login"                 # 账号密码登录
METHOD_USER_INFO = "CommonAccount.getUserInfo"  # 携带 Cookie 换 token（免密码）

# 公共参数 mgsdk_login_type 的取值
LOGIN_METHOD_PASSWORD = 2

# ── Web 登录 ────────────────────────────────────────────────
# 手机端登录页
LOGIN_URL = "https://i.360.cn/login/wap?show_index=1&showH5=1"
# 登录成功判定页
SUCCESS_URL_PREFIX = "https://i.360.cn/index/wap"

# 渠道名（= assets/360_assistant_data 的文件名前缀）
CHANNEL_NAME = "360_assistant"

# 发给网易 uni_sauth 的 app_channel。
# 依据：AndroidManifest 的 protocolAppChannel 与
#       assets/ntunisdk_common_data 的 APP_CHANNEL 均为该值。
APP_CHANNEL_FALLBACK = "allysdk.360_assistant"

# 内置兜底凭证（第五人格 h55，来自 assets/360_assistant_data，
# 已与 AndroidManifest 的 QHOPENSDK_APPID / QHOPENSDK_APPKEY 交叉验证）
DEFAULT_APP_ID = 203840726
DEFAULT_APP_KEY = "838559dca512625db0deadb1a974d5e6"

# 移动端 UA（登录页按移动端渲染）
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G9910) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

# ── 响应字段 ────────────────────────────────────────────────
# 实测响应：{"errno":"0","errmsg":"OK","consume":39,
#            "data":{"qid":..,"qoauth_token":{"access_token":..,"expires_in":..}}}
KEY_ERRNO = "errno"
KEY_ERRMSG = "errmsg"
KEY_DATA = "data"
KEY_QID = "qid"
KEY_ACCESS_TOKEN = "access_token"
KEY_QOAUTH_TOKEN = "qoauth_token"
KEY_EXPIRES_IN = "expires_in"

ERRNO_OK = 0

# cookie 名
COOKIE_Q = "Q"
COOKIE_T = "T"
