import os
import time

import requests

from PyQt6.QtWebEngineCore import QWebEngineUrlSchemeHandler
from channelHandler.WebLoginUtils import WebBrowser
from logutil import setup_logger
from ssl_utils import should_verify_ssl


def sig_helper(magicValue="5C2F##3[6$^(68#%#D3E96;]35q#FB46", ts="1"):
    from hashlib import md5
    return md5((ts + magicValue).encode())


class QQBrowser(WebBrowser):
    """QQ OAuth 浏览器，拦截 auth:// scheme 获取 code/token。"""

    class AuthSchemeHandler(QWebEngineUrlSchemeHandler):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.parent = parent

        def requestStarted(self, info):
            url = info.requestUrl().toString()
            if url.startswith("auth://"):
                self.parent._on_auth_redirect(info.requestUrl())

    def __init__(self, qq_appid):
        super().__init__("myapp_qq", False, frameless=True)
        self.qq_appid = qq_appid
        self._scheme_handler = self.AuthSchemeHandler(self)
        self.profile.removeAllUrlSchemeHandlers()
        self.profile.installUrlSchemeHandler(b"auth", self._scheme_handler)

    def _on_auth_redirect(self, url):
        """auth:// scheme 拦截回调"""
        url_str = url.toString()
        if self.verify(url_str):
            if self.parseReslt(url_str):
                try:
                    self.profile.removeAllUrlSchemeHandlers()
                except Exception:
                    pass
                self.cleanup()

    def verify(self, url: str) -> bool:
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(url)
        # Code flow: auth://tauth.qq.com/?code=...
        if "tauth.qq.com" in url and "code=" in url:
            return True
        # Implicit flow fallback: imgcache.qq.com with access_token in fragment
        if parsed_url.netloc == "imgcache.qq.com" and parsed_url.path == "/open/connect/widget/mobile/login/proxy.htm":
            query_dict = parse_qs(parsed_url.fragment)
            return "access_token" in query_dict.keys()
        return False

    def parseReslt(self, url):
        from urllib.parse import urlparse, parse_qs
        from logutil import setup_logger
        logger = setup_logger()

        # Code flow: auth://tauth.qq.com/?code=...
        if "tauth.qq.com" in url and "code=" in url:
            query_str = url.split("?", 1)[1] if "?" in url else ""
            query_dict = parse_qs(query_str)
            logger.debug(f"QQ OAuth code flow keys: {list(query_dict.keys())}")
            self.result = {
                "flow": "code",
                "code": query_dict.get("code", [None])[0],
                "pf": query_dict.get("pf", [None])[0],
                "pfkey": query_dict.get("pfkey", [None])[0],
            }
            logger.debug(f"QQ OAuth code flow: code={self.result['code'][:8] if self.result['code'] else None}...")
            return True

        # Implicit flow fallback
        parsed_url = urlparse(url)
        query_dict = parse_qs(parsed_url.fragment)
        logger.debug(f"QQ OAuth implicit flow keys: {list(query_dict.keys())}")
        self.result = {
            "flow": "implicit",
            "access_token": query_dict.get("access_token", [None])[0],
            "openid": query_dict.get("openid", [None])[0],
            "pay_token": query_dict.get("pay_token", [None])[0],
        }
        logger.debug(f"QQ OAuth implicit result: openid={self.result['openid']}, "
                     f"has_access_token={bool(self.result['access_token'])}")
        return True


class QQLogin:
    def __init__(self, qq_appid, game_id=""):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.qq_appid = qq_appid
        self.game_id = game_id
        self._active_browser: QQBrowser = None

    def _verify_qq_login(self, access_token, openid, pay_token=None):
        """通过 YSDK 验证 QQ 登录，获取 atk/pf/pfKey/rtk"""
        ts = str(int(time.time() * 1000))
        verify_data = {
            "channel": "00000000",
            "offerid": self.qq_appid,
            "atk": access_token,
            "ptk": pay_token or "",
            "openid": openid,
            "loginType": "0",
            "loginplatform": "0",
            "platform": "desktop_m_qq",
            "isCheckATK": "1",
            "isCheckPTK": "1",
            "version": "2.2.2",
            "sig": sig_helper(ts=ts).hexdigest(),
            "timestamp": ts,
            "appid": self.qq_appid,
            "client_hope_switch": "1",
            "anti_hope_switch": "1",
            "visitor_switch": "-1",
            "regChannel": "",
        }
        self.logger.debug(f"QQ verify params: {verify_data}")
        url = "https://ysdk.qq.com/auth/qq_verify_login"
        r = requests.get(
            url,
            params=verify_data,
            verify=should_verify_ssl(),
        )
        self.logger.debug(f"QQ verify request URL: {r.url}")
        rjson = r.json()
        self.logger.info(f"QQ verify response: {rjson}")
        if r.status_code != 200 or rjson.get("ret") != 0:
            self.logger.error(f"QQ YSDK 验证失败: {rjson}")
            return None
        self.logger.info("QQ YSDK 验证成功")
        # YSDK qq_verify_login 不返回 atk/openid/atk_expire，需用 QQ 原始 token 补全
        if "atk" not in rjson:
            rjson["atk"] = access_token
        if "openid" not in rjson:
            rjson["openid"] = openid
        if "atk_expire" not in rjson:
            rjson["atk_expire"] = 7200  # QQ access_token 默认 2 小时
        if "pay_token" not in rjson:
            rjson["pay_token"] = pay_token
        # 处理健康系统（实名认证等）
        self._handle_judge_login(rjson)
        return rjson

    def _handle_judge_login(self, rjson):
        """处理 judgeLoginData 中的健康系统指令"""
        judge_data = rjson.get("judgeLoginData")
        if not judge_data:
            return
        import json as _json
        try:
            if isinstance(judge_data, str):
                judge_data = _json.loads(judge_data)
            instructions = judge_data.get("instructions", [])
            if not instructions:
                return
            for inst in instructions:
                inst_type = inst.get("type")
                msg = inst.get("msg", "")
                url = inst.get("url", "")
                title = inst.get("title", "")
                self.logger.warning(f"健康系统: {title} - {msg}")
                if inst_type == 3 and url:
                    self.logger.info(f"健康系统: 需要跳转到 {url}")
                    import webbrowser
                    webbrowser.open(url)
        except Exception:
            self.logger.exception("解析 judgeLoginData 失败")

    def _exchange_code(self, code, pf=None, pfkey=None):
        """通过 YSDK /cmd/QQCodeLogin 换取 60 天 token"""
        import hmac, hashlib, base64, json as _json
        timestamp = str(int(time.time()))
        body = _json.dumps({"appID": self.qq_appid, "loginCode": code})

        sign_str = f"POST\n/cmd/QQCodeLogin\njson\nysdk\n{timestamp}\n{body}"
        key = "yyb@cloud_game:CQ8FA#"
        digest = base64.b64encode(
            hmac.new(key.encode(), sign_str.encode(), hashlib.sha256).digest()
        ).decode()

        headers = {
            "Content-Type": "json",
            "Auth-Secret-ID": "ysdk",
            "Auth-Secret-Digest": digest,
            "Auth-Request-Time": timestamp,
        }
        url = "https://ysdk.qq.com/cmd/QQCodeLogin?"
        self.logger.debug(f"QQCodeLogin request: appID={self.qq_appid}, code={code[:8]}...")
        r = requests.post(url, data=body, headers=headers, verify=should_verify_ssl())
        rjson = r.json()
        self.logger.info(f"QQCodeLogin response: code={rjson.get('code')}, errmsg={rjson.get('errmsg')}")

        if rjson.get("code") != 0:
            self.logger.error(f"QQCodeLogin 失败: {rjson}")
            return None

        data = rjson.get("data", {})
        if data.get("ret") != 0:
            self.logger.error(f"QQCodeLogin data error: {data}")
            return None

        # 构建与 myappVeriftResp 兼容的结果
        result = {
            "atk": data["accessToken"],
            "openid": data["openID"],
            "pay_token": data.get("payToken"),
            "atk_expire": int(data.get("expiresIn", 5184000)),
            "rtk": data.get("refreshToken"),
            "pf": pf or "",
            "pfKey": pfkey or "",
            "regChannel": "",
            "msg": "ok",
            "first": 0,
        }
        self.logger.info(f"QQCodeLogin 成功: openid={result['openid']}, expires={result['atk_expire']}s")
        return result

    def _process_browser_result(self, result):
        """处理浏览器结果，支持 code flow 和 implicit flow"""
        if not result or not isinstance(result, dict):
            return None

        flow = result.get("flow", "implicit")

        if flow == "code":
            # Code flow: 60 天 token
            return self._exchange_code(
                result["code"],
                pf=result.get("pf"),
                pfkey=result.get("pfkey"),
            )
        else:
            # Implicit flow fallback: 2 小时 token，需要 YSDK verify
            return self._verify_qq_login(
                result["access_token"],
                result["openid"],
                result.get("pay_token"),
            )

    def webLogin(self, on_complete=None):
        login_url = (
            f"https://openmobile.qq.com/oauth2.0/m_authorize"
            f"?client_id={self.qq_appid}&scope=all"
            f"&redirect_uri=auth://tauth.qq.com/&style=qr&response_type=code"
        )
        browser = QQBrowser(self.qq_appid)
        browser.set_url(login_url)
        result = browser.run()

        if result is None:
            # 异步模式
            self._active_browser = browser
            if on_complete is not None:
                def _on_async_done(b):
                    self._active_browser = None
                    try:
                        on_complete(self._process_browser_result(b.result))
                    except Exception:
                        self.logger.exception("QQ异步登录处理失败")
                        on_complete(None)
                browser._async_completion_callback = _on_async_done
            return None

        # 同步模式
        return self._process_browser_result(result)
