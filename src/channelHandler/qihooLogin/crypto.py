# coding=UTF-8
"""360 登录接口的加解密与签名。

协议要点：
  · DES/CBC/PKCS5Padding，key 同时用作 IV
  · sig = MD5(按 key 字典序拼接的「原始值」串 + SIGN_SALT)，小写 hex
  · 传输串用「URLEncoder.encode(原始值)」，但签名用「原始值」
  · parad = URLEncoder.encode(base64(DES(query)))
"""
from __future__ import annotations

import base64
import hashlib

from Crypto.Cipher import DES

from .consts import DES_KEY, FROM_PREFIX, SDK_V, SIGN_SALT

PASSPORT_BASE = "https://login.360.cn/api.php?parad="

# java.net.URLEncoder.encode 不转义的字符集（Java 的 "safe" 集合）
_JAVA_URL_SAFE = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-*_"
)


def md5(text: str) -> str:
    """小写 hex MD5（utils.y.c / MD5Util）。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _pkcs5_pad(data: bytes) -> bytes:
    pad = 8 - (len(data) % 8)
    return data + bytes([pad]) * pad


def des_encrypt(plain: str, key: str = DES_KEY) -> str:
    """DES/CBC/PKCS5Padding，返回 base64。key 同时用作 IV。"""
    raw_key = key.encode("utf-8")
    cipher = DES.new(raw_key, DES.MODE_CBC, raw_key)
    return base64.b64encode(cipher.encrypt(_pkcs5_pad(plain.encode("utf-8")))).decode()


def des_decrypt(b64_text: str, key: str = DES_KEY) -> str:
    """DES/CBC/PKCS5Padding 解密，失败抛异常。"""
    raw_key = key.encode("utf-8")
    cipher = DES.new(raw_key, DES.MODE_CBC, raw_key)
    raw = cipher.decrypt(base64.b64decode(b64_text))
    if not raw:
        return ""
    pad = raw[-1]
    if not 1 <= pad <= 8:
        raise ValueError("PKCS5 padding 非法: %r" % pad)
    return raw[:-pad].decode("utf-8", "replace")


def java_urlencode(text: str) -> str:
    """URL 编码，与 java.net.URLEncoder.encode(s, "UTF-8") 保持一致。

    与 urllib.parse.quote 的差异：空格编码为 '+'，且保留 * _ . -
    """
    out = []
    for ch in text.encode("utf-8").decode("latin-1"):
        if ch in _JAVA_URL_SAFE:
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.append("%%%02X" % ord(ch))
    return "".join(out)


def build_query(params: dict, from_param: str) -> tuple:
    """拼装请求 URL。

    Returns:
        (url, sign_src, query)
    """
    # 按 key 字典序排序；null 值视作 ""
    tree = {k: ("" if v is None else str(v)) for k, v in params.items()}
    tree["from"] = from_param
    tree["v"] = SDK_V
    tree["res_mode"] = "1"
    tree["format"] = "json"

    items = sorted(tree.items())

    # 签名用原始值，不用 URL 编码值
    sign_src = "".join("%s=%s" % (k, v) for k, v in items) + SIGN_SALT
    sig = md5(sign_src)

    # 传输用 URL 编码值
    query = "".join("%s=%s&" % (k, java_urlencode(v)) for k, v in items) + "sig=" + sig

    url = PASSPORT_BASE + java_urlencode(des_encrypt(query)) + "&from=" + from_param
    return url, sign_src, query


def build_from(app_id: int) -> str:
    """拼装 from 参数：FROM_PREFIX + appId。"""
    return FROM_PREFIX + str(app_id)


# ── cookie 工具 ─────────────────────────────────────────────
def extract_qt(cookie: str) -> dict:
    """从 cookie 串中提取 Q / T（按 cookie 名精确匹配，不依赖位置）。"""
    out = {}
    for seg in str(cookie or "").replace("\r", ";").replace("\n", ";").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, _, value = seg.partition("=")
        name = name.strip()
        if name in ("Q", "T"):
            out[name] = value.strip()
    return out


def build_qt_cookie(q: str, t: str) -> str:
    """拼装 Cookie 头："Q=" + qCookie + ";T=" + tCookie。"""
    return "Q=%s;T=%s" % (q, t)


def has_qt(cookie: str) -> bool:
    """同时含 Q= 与 T= 才算有效 cookie。"""
    text = str(cookie or "")
    if "Q=" not in text or "T=" not in text:
        return False
    parts = extract_qt(text)
    return bool(parts.get("Q")) and bool(parts.get("T"))
