import base64
import json
import urllib.parse
from dataclasses import dataclass
from typing import Dict

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Hash import MD5, SHA1, SHA256
from Crypto.Signature import pkcs1_15


OPPO_PROTOCOL_VERSION = "3.0"

OPPO_RSA_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDpgSW5VkZ6/xvh+wMXezrOokNdiupu"
    "vuMj4RVJy44byWDupl4H37z907A26RVdFzMeyLUQB4rsDIaXdxCODlljWW+/K96uF5"
    "MsDtOFUBw7VlOclIjcYTv/YDQEul8JoXoOuy1Yf3b5sbTpTuVTcl97tAuLJ8PoGe2K"
    "7N3B1eUQqQIDAQAB"
)


def _pem_from_spki_b64(spki_b64: str) -> bytes:
    der = base64.b64decode(spki_b64)
    b64 = base64.encodebytes(der).replace(b"\n", b"")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(lines) + b"\n-----END PUBLIC KEY-----\n"


_RSA_PUB = RSA.import_key(_pem_from_spki_b64(OPPO_RSA_PUBLIC_KEY_B64))


def b64_urlsafe_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def b64_std_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64_std_decode(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def aes_ctr_encrypt_to_b64(plaintext: str, aes_key_b64_urlsafe: str, iv: bytes) -> str:
    # key = b64_urlsafe_decode(aes_key_b64_urlsafe)
    # Java Cipher AES/CTR/NoPadding uses the base64 string as key bytes (24 bytes -> AES-192)
    key = aes_key_b64_urlsafe.encode("ascii")
    # Java Cipher AES/CTR/NoPadding + IvParameterSpec(16 bytes) => 128-bit counter init = iv
    initial_value = int.from_bytes(iv, byteorder="big", signed=False)
    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=initial_value)
    ct = cipher.encrypt(plaintext.encode("utf-8"))
    return b64_std_encode(ct)


def aes_ctr_decrypt_from_b64(ciphertext_b64_std: str, aes_key_b64_urlsafe: str, iv: bytes) -> str:
    # key = b64_urlsafe_decode(aes_key_b64_urlsafe)
    key = aes_key_b64_urlsafe.encode("ascii")
    initial_value = int.from_bytes(iv, byteorder="big", signed=False)
    cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=initial_value)
    pt = cipher.decrypt(b64_std_decode(ciphertext_b64_std))
    return pt.decode("utf-8", errors="replace")


def rsa_encrypt_b64(plaintext: str, pubkey: RSA.RsaKey = _RSA_PUB) -> str:
    cipher = PKCS1_v1_5.new(pubkey)
    ct = cipher.encrypt(plaintext.encode("utf-8"))
    return b64_std_encode(ct)


def verify_rsa_signature_of_text(text: str, signature_b64: str, pubkey: RSA.RsaKey = _RSA_PUB) -> bool:
    """尽量兼容 RsaCoder.doCheck：尝试多种 hash 算法验证。"""

    sig = b64_std_decode(signature_b64)
    data = text.encode("utf-8")

    for h in (MD5.new(data), SHA1.new(data), SHA256.new(data)):
        try:
            pkcs1_15.new(pubkey).verify(h, sig)
            return True
        except Exception:
            pass
    return False


@dataclass
class SecurityKey:
    aes_key_b64_urlsafe: str
    iv: bytes
    iv_b64_urlsafe: str
    rsa_b64_std: str
    session_ticket: str = ""

    header_signature_v1: str = ""  # X-Security 原文
    header_signature_v2: str = ""  # urlencoded(X-Security)

    @staticmethod
    def generate(pubkey: RSA.RsaKey = _RSA_PUB) -> "SecurityKey":
        iv = get_random_bytes(16)
        iv_str = b64_urlsafe_encode(iv)

        aes_key_raw = get_random_bytes(16)
        aes_key_str = b64_urlsafe_encode(aes_key_raw)

        rsa_str = rsa_encrypt_b64(aes_key_str, pubkey)
        return SecurityKey(
            aes_key_b64_urlsafe=aes_key_str,
            iv=iv,
            iv_b64_urlsafe=iv_str,
            rsa_b64_std=rsa_str,
        )

    def encrypt(self, plaintext: str) -> str:
        return aes_ctr_encrypt_to_b64(plaintext, self.aes_key_b64_urlsafe, self.iv)

    def decrypt(self, ciphertext_b64_std: str) -> str:
        return aes_ctr_decrypt_from_b64(ciphertext_b64_std, self.aes_key_b64_urlsafe, self.iv)


def build_security_headers(
    security_key: SecurityKey,
    device_security_header_plain: str,
    xor_key_name: str = "key",
) -> Dict[str, str]:
    """复刻 SecurityRequestInterceptor.Header.newHeader。

    返回：包含 Accept / X-Security / X-Key / X-I-V / X-Session-Ticket / X-Protocol* / X-Safety / X-Protocol。
    """

    x_security = security_key.encrypt(device_security_header_plain)
    security_key.header_signature_v1 = x_security

    headers: Dict[str, str] = {
        "X-Protocol-Version": OPPO_PROTOCOL_VERSION,
        "X-Protocol-Ver": OPPO_PROTOCOL_VERSION,
        "Accept": "application/encrypted-json",
        "X-Security": x_security,
        "X-Key": security_key.rsa_b64_std,
        "X-I-V": security_key.iv_b64_urlsafe,
    }

    if security_key.session_ticket:
        headers["X-Session-Ticket"] = security_key.session_ticket

    protocol_obj = {
        xor_key_name: security_key.rsa_b64_std,
        "iv": security_key.iv_b64_urlsafe,
        "sessionTicket": security_key.session_ticket,
    }
    protocol_json = json.dumps(protocol_obj, ensure_ascii=False, separators=(",", ":"))
    # Java 里把 "\/" 替换回 "/"，Python dumps 默认不转义 '/'，无需处理。
    headers["X-Protocol"] = urllib.parse.quote(protocol_json, safe="")

    x_safety = urllib.parse.quote(x_security, safe="")
    security_key.header_signature_v2 = x_safety
    headers["X-Safety"] = x_safety

    return headers

if __name__ == "__main__":
    #{"plaintext": "{\"imei\":\"\",\"imei1\":\"\",\"mac\":\"\",\"serialNum\":\"\",\"serial\":\"\",\"wifissid\":\"\",\"hasPermission\":false,\"deviceName\":\"\",\"marketName\":\"\"}", "ciphertext": "ErE8TiV7CgPzumFU1KBBeg3FflI2aSV7C9JkTJNnle6IaQB5oGAqT7FDWxBwwhQw/mF3B5rWrVyrgCfgQdPl+e6qcu4Ow78G4Gk7MbE1wgXRf4lcY7GddI/rr1/nEaofLH2H9xRSHYeiZkTyOuS58tXLSOknK8WyGuyZQcGx", "key_info": {"aes": "cU2ACiIjWN_yXUm0OzC_cg==", "iv_hex": "ced0518399e1dafc0f311c71944bd765", 
    a=SecurityKey(
        aes_key_b64_urlsafe="cU2ACiIjWN_yXUm0OzC_cg==",
        iv=bytes.fromhex("ced0518399e1dafc0f311c71944bd765"),
        iv_b64_urlsafe="ze0UYDm4dafiD8Mx8kS9w==",
        rsa_b64_std=rsa_encrypt_b64("cU2ACiIjWN_yXUm0OzC_cg==", _RSA_PUB),
    )
    # This matches: ErE8TiV7CgPzumFU1KBBeg3FflI2aSV7C9JkTJNnle6IaQB5oGAqT7FDWxBwwhQw...
    print(a.encrypt('{"imei":"","imei1":"","mac":"","serialNum":"","serial":"","wifissid":"","hasPermission":false,"deviceName":"","marketName":""}'))
