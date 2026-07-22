"""Classify mpay traffic by its runtime owner.

The same service host is used by native games, the real Fever client, the
mpay instance hosted by this tool, and a Fever-bridged target game.  Policy
must be based on that role instead of a process-wide debug switch.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs


ROLE_NATIVE_GAME = "native_game"
ROLE_REAL_FEVER = "real_fever"
ROLE_HOSTED_FEVER_MPAY = "hosted_fever_mpay"
ROLE_BRIDGED_GAME = "bridged_game"


def _request_values(request) -> dict:
    values = {}
    query = getattr(request, "query", {})
    if hasattr(query, "items"):
        values.update({str(key): str(value) for key, value in query.items()})
    content = getattr(request, "content", b"") or b""
    content_type = getattr(request, "headers", {}).get("content-type", "")
    try:
        if "application/x-www-form-urlencoded" in content_type:
            parsed = parse_qs(content.decode("utf-8", errors="replace"), keep_blank_values=True)
            values.update({key: item[-1] if item else "" for key, item in parsed.items()})
        elif "application/json" in content_type:
            body = json.loads(content)
            if isinstance(body, dict):
                values.update({str(key): str(value) for key, value in body.items()})
    except (ValueError, TypeError):
        pass
    return values


def _short_game_id(value) -> str:
    return str(value or "").strip().split("-")[-1]


def classify_mpay_request(
    request, bridged_game_ids=(), hosted_mpay_active: bool = False
) -> str:
    values = _request_values(request)
    game_id = str(values.get("game_id") or "")
    app_channel = str(values.get("app_channel") or "")
    dst_game_id = str(values.get("dst_jf_game_id") or "")

    is_a50 = app_channel == "a50_sdk_cn" or _short_game_id(game_id) == "a50"
    if is_a50:
        return ROLE_HOSTED_FEVER_MPAY if hosted_mpay_active else ROLE_REAL_FEVER
    if dst_game_id:
        return ROLE_REAL_FEVER
    bridged = {_short_game_id(item) for item in bridged_game_ids}
    if _short_game_id(game_id) in bridged:
        return ROLE_BRIDGED_GAME
    return ROLE_NATIVE_GAME
