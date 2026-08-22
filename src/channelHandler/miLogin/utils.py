import json
from urllib.parse import urlencode
from Crypto.Cipher import AES
import base64


def pad(data):
    block_size = 16
    pad_size = block_size - len(data) % block_size
    return data + chr(pad_size) * pad_size


def unpad(data):
    pad_size = ord(data[-1])
    return data[:-pad_size]


def aes_encrypt(data, key):
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(data).encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def aes_decrypt(encrypted_data, key):
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    decrypted = cipher.decrypt(base64.b64decode(encrypted_data))
    return unpad(decrypted.decode("utf-8"))


def quote(s, safe="", encoding=None, errors=None):
    return s


def generate_unsign_request(params, AES_KEY):
    encoded_params = urlencode(params, quote_via=quote)
    encrypted_p = aes_encrypt(encoded_params, AES_KEY)

    final_params = {"p": encrypted_p}

    return final_params


def decrypt_response(encrypted_response, key):
    return json.loads(aes_decrypt(encrypted_response, key))
