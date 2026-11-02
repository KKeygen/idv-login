"""荣耀 OAuth 回调及游戏会话校验。"""
from urllib.parse import parse_qs, urlsplit


def valid_redirect(url, expected_state, redirect_uri):
    parsed, expected = urlsplit(url), urlsplit(redirect_uri)
    params = parse_qs(parsed.query, keep_blank_values=True)
    codes = params.get("code", [])
    return (bool(expected_state) and parsed.scheme == expected.scheme
            and parsed.netloc == expected.netloc and parsed.path == expected.path
            and len(codes) == 1 and bool(codes[0]) and "error" not in params
            and params.get("state") == [expected_state])


def parse_session(data):
    """解析荣耀 Game Center 登录和刷新响应。"""
    if not isinstance(data, dict) or data.get("errorCode") not in (None, 0, "0"):
        raise ValueError("荣耀游戏会话响应无效")
    login_wrapper = data.get("loginData") or data.get("data") or {}
    if not isinstance(login_wrapper, dict) or login_wrapper.get("errorCode") not in (None, 0, "0"):
        raise ValueError("荣耀游戏会话响应无效")
    login_data = login_wrapper.get("data") or login_wrapper
    if not isinstance(login_data, dict):
        raise ValueError("荣耀游戏会话响应无效")
    token = login_data.get("unionToken")
    if not isinstance(token, dict) or any(not isinstance(token.get(k), str) or not token[k]
                                          for k in ("openId", "token")):
        raise ValueError("荣耀游戏会话身份无效")
    lifetime = int(token.get("expireTimeout", 3600))
    if lifetime <= 0:
        raise ValueError("荣耀游戏会话已过期")
    return dict(token), str(login_data.get("displayName") or ""), lifetime
