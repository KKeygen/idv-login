"""Verify embedded cloud-configuration signatures with a pinned public key.

The public key is empty until the release owner provisions it.  An empty key is
an explicit pre-rollout compatibility mode, not a verified configuration.
"""
import base64
import binascii
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


CLOUD_CONFIG_PUBLIC_KEY_PEM = b''
SIGNATURE_DOMAIN = b'IDV-CLOUD-CONFIG-V1\0'


def signatures_required():
    return bool(CLOUD_CONFIG_PUBLIC_KEY_PEM)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_number(_value):
    raise ValueError('non-finite JSON number')


def canonical_cloud_config(data):
    """Parse strict JSON and return the signature record and canonical bytes."""
    parsed = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                        parse_constant=_reject_number)
    if not isinstance(parsed, dict):
        raise ValueError('cloud configuration must be an object')
    record = parsed.pop('_signature')
    if not isinstance(record, dict) or set(record) != {'alg', 'value'} or record['alg'] != 'ECDSA-P256-SHA256-DER':
        raise ValueError('invalid cloud signature record')
    if not isinstance(record['value'], str):
        raise ValueError('invalid cloud signature value')
    canonical = json.dumps(parsed, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False).encode('utf-8')
    return record['value'], canonical


def verify_cloud_config(data, public_key_pem=None):
    """Verify the canonicalized complete config with its embedded signature."""
    pem = CLOUD_CONFIG_PUBLIC_KEY_PEM if public_key_pem is None else public_key_pem
    try:
        if not isinstance(data, bytes) or not pem:
            return False
        signature, canonical = canonical_cloud_config(data)
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            return False
        decoded = base64.b64decode(signature, validate=True)
        key.verify(decoded, SIGNATURE_DOMAIN + canonical, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, InvalidSignature):
        return False
