"""One Channel-owned UniSDK packet; no login or persistence side effects."""
from __future__ import annotations

import base64
import json
import re
import traceback
from urllib.parse import unquote

from channelHandler.channelUtils import getShortGameId


def packet_identity(packet):
    """Read actual SAUTH identity; error text names fields, never their values."""
    if not isinstance(packet, dict):
        raise ValueError('field=packet: expected object')
    extra = packet.get('extra_unisdk_data')
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError) as error:
            raise ValueError('field=extra_unisdk_data: invalid JSON') from error
    if not isinstance(extra, dict):
        raise ValueError('field=extra_unisdk_data: expected object')
    encoded = extra.get('SAUTH_JSON')
    if not isinstance(encoded, str) or not encoded:
        raise ValueError('field=SAUTH_JSON: missing nonempty string')
    try:
        auth = json.loads(base64.b64decode(unquote(encoded)))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError('field=SAUTH_JSON: invalid base64/JSON') from error
    if not isinstance(auth, dict):
        raise ValueError('field=SAUTH_JSON: expected object')
    for field in ('gameid', 'login_channel'):
        value = auth.get(field)
        if not isinstance(value, str) or not value or '\0' in value:
            raise ValueError(f'field=SAUTH_JSON.{field}: missing/invalid string')
    sdkuid = auth.get('sdkuid')
    if (isinstance(sdkuid, bool) or not isinstance(sdkuid, (str, int))
            or not str(sdkuid) or '\0' in str(sdkuid)):
        raise ValueError('field=SAUTH_JSON.sdkuid: missing/invalid SDK UID')
    if auth['login_channel'] != packet.get('login_channel'):
        raise ValueError('field=SAUTH_JSON.login_channel: differs from packet.login_channel')
    game = getShortGameId(auth['gameid'])
    if not game:
        raise ValueError('field=SAUTH_JSON.gameid: empty short game ID')
    return game, auth['login_channel'], str(sdkuid)


def exception_summary(error):
    """Exception chain with source locations and approved facts, never frame locals."""
    lines, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        facts = type(error).__name__
        value = str(error)
        if re.fullmatch(r'field=[A-Za-z0-9_.]+: [A-Za-z0-9 _/().-]+', value) or re.fullmatch(
                r'MPay Wasm [a-z_]+ failed: status=-?[0-9]+', value) or re.fullmatch(
                r'channel=[a-z0-9_]+ step=[a-z0-9_]+ code=(-?[0-9]+|invalid)', value):
            facts += ': ' + value
        if isinstance(getattr(error, 'errno', None), int):
            facts += f' errno={error.errno}'
        if isinstance(error, json.JSONDecodeError):
            facts += f' line={error.lineno} column={error.colno}'
        response = getattr(error, 'response', None)
        status = getattr(response, 'status_code', None)
        if isinstance(status, int):
            facts += f' http_status={status}'
        lines.append(facts + '\n' + ''.join(traceback.format_tb(error.__traceback__)))
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return '\nCaused by: '.join(lines)


def unisdk_expires_at(entry):
    """Huawei gameAuthSign is bound to ts and expires five minutes after issuance."""
    deadlines = [entry.get('expires_at')]
    packet = entry['packet']
    if packet['login_channel'] == 'huawei':
        extra = json.loads(packet['extra_unisdk_data'])
        auth = json.loads(base64.b64decode(unquote(extra['SAUTH_JSON'])))
        deadlines.append(int(auth['timestamp']) / 1000 + 300)
    return min((value for value in deadlines if value is not None), default=None)


def credential_expires_at(record):
    """Only consult local credential fields; never invoke a login accessor."""
    attrs = vars(record)
    channel = record.channel_name
    value = None
    if attrs.get('record_source') == 'scan':
        user = record.user_info
        value = user.get('expires', user.get('expire'))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None
    if channel in ('myapp', 'myapp_qq'):
        session = attrs.get('session')
        session = session if isinstance(session, dict) else vars(session) if session else {}
        duration = session.get('atk_expire')
        if duration:
            value = attrs.get('token_issued_at', record.last_login_time) + int(duration)
    elif channel in ('honor', 'honor_sdk'):
        login = attrs.get('honorLogin')
        value = getattr(login, 'expiredTime', None)
    elif channel == 'bilibili_sdk':
        data = attrs.get('loginResp') or {}
        value = (data.get('data') or data).get('expires')
    elif channel == 'uc_platform':
        value = attrs.get('sid_expire_time')
    elif channel in ('qihoo', '360_assistant'):
        value = attrs.get('token_expire_time')
    try:
        return int(value) if value is not None and int(value) > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None
