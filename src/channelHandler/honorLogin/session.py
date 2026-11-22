"""荣耀 OAuth 回调及游戏会话校验。"""
from urllib.parse import parse_qs, urlsplit


def _warn(message, *args):
    """热修复诊断：即使 logger 不可用也不能影响登录流程。"""
    try:
        from logutil import setup_logger
        setup_logger().warning(message, *args)
    except Exception:
        pass


def valid_redirect(url, expected_state, redirect_uri):
    parsed, expected = urlsplit(url), urlsplit(redirect_uri)
    params = parse_qs(parsed.query, keep_blank_values=True)
    codes = params.get("code", [])
    # 荣耀实际回跳可能带尾斜杠（honorid://redirect_url/?...），
    # 与 redirect_uri 的 path('') 严格比较会永远失败。
    path_ok = parsed.path.rstrip("/") == expected.path.rstrip("/")
    states = params.get("state")
    state_match = bool(expected_state) and states == [expected_state]
    # 服务端不回传 state 时按兼容模式放行；回传但不匹配仍然拒绝。
    state_ok = state_match or states is None
    ok = (bool(expected_state) and parsed.scheme == expected.scheme
          and parsed.netloc == expected.netloc and path_ok
          and len(codes) == 1 and bool(codes[0]) and "error" not in params
          and state_ok)
    if ok and states is None:
        _warn("荣耀 OAuth 回调未携带 state，已按兼容模式继续")
    elif not ok:
        _warn("荣耀 OAuth 回调校验未通过: scheme={} netloc={} path={!r} code_count={} "
              "state_present={} state_match={} error={}",
              parsed.scheme, parsed.netloc, parsed.path, len(codes),
              states is not None, state_match, params.get("error"))
    return ok


class HonorSessionError(ValueError):
    def __init__(self, code=None, *, login_data=False):
        self.code = code if isinstance(code, int) or isinstance(code, str) and code.lstrip("-").isdigit() else None
        self.code = int(self.code) if self.code is not None else None
        self.blocks_reauthorization = login_data and self.code in (7008, 7009)
        super().__init__("荣耀游戏会话响应无效" + (f"（错误码 {self.code}）" if self.code is not None else ""))


def app_version_code(config):
    """The client game version code, not the Honor SDK version/name."""
    try:
        value = int(str(config.get("app_version_code", "")))
        return value if -(2 ** 31) <= value < 2 ** 31 else 0
    except (TypeError, ValueError):
        return 0


def parse_session(data):
    """解析荣耀 Game Center 登录和刷新响应。"""
    if not isinstance(data, dict):
        raise HonorSessionError()
    if data.get("errorCode") not in (None, 0, "0"):
        raise HonorSessionError(data.get("errorCode"))
    login_wrapper = data.get("loginData") or data.get("data") or {}
    if not isinstance(login_wrapper, dict):
        raise HonorSessionError()
    if login_wrapper.get("errorCode") not in (None, 0, "0"):
        raise HonorSessionError(login_wrapper.get("errorCode"), login_data=True)
    login_data = login_wrapper.get("data") or login_wrapper
    if not isinstance(login_data, dict):
        raise ValueError("荣耀游戏会话响应无效")
    token = login_data.get("unionToken")
    if not isinstance(token, dict) or any(not isinstance(token.get(k), str) or not token[k]
                                          for k in ("openId", "token")):
        raise ValueError("荣耀游戏会话身份无效")
    # The SDK accepts successful sessions without a positive expireTimeout.
    # Keep a supplied positive cache hint; do not invent a one-hour lifetime.
    lifetime = token.get("expireTimeout")
    if lifetime is not None:
        try:
            if isinstance(lifetime, bool):
                raise ValueError
            lifetime = int(lifetime)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("荣耀游戏会话期限格式无效") from None
    if lifetime is not None and lifetime <= 0:
        lifetime = None
    return dict(token), str(login_data.get("displayName") or ""), lifetime
