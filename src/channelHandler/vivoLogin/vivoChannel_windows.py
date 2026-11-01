import os
import time

import requests

from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.WebLoginUtils import WebBrowser
from browser_cookies import CookieExport, CookieExportError, cookies_for_url, normalize_cookie


VIVO_GET = "https://joint.vivo.com.cn/h5/union/get"
VIVO_USE = "https://joint.vivo.com.cn/h5/union/use"
_UNCHANGED = object()

class VivoBrowser(WebBrowser):
    cookie_export = CookieExport(
        source_origins=("https://joint.vivo.com.cn",),
        source_paths=("/game-subaccount-login",),
        targets=(VIVO_GET, VIVO_USE),
    )

    def __init__(self, gamePackage):
        super().__init__("nearme_vivo", True, frameless=True)
        self.logger = setup_logger()
        self.gamePackage = gamePackage

    def verify(self, url: str) -> bool:
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        values = self.parse_url_query(url).get("openid", [])
        return (parsed.scheme == "https" and parsed.netloc == "joint.vivo.com.cn"
                and parsed.path == "/game-subaccount-login"
                and len(values) == 1 and bool(values[0]))

    def export_cookie(self):
        return cookies_for_url(self.cookie_records, VIVO_GET)

    def parseReslt(self, url):
        self.result = {"code": 0, "data": {"redirect_url": url}}
        return True

    def parse_url_query(self, url):
        from urllib.parse import urlparse, parse_qs
        return parse_qs(urlparse(url).query)


class VivoLogin:
    def __init__(self, gamePackage=""):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.gamePackage = gamePackage
        self.cookies = {}
        self.cookieRecords = None
        self._active_browser: VivoBrowser = None  # 异步模式下保持强引用

    def _cookies_for_endpoint(self, endpoint):
        if endpoint not in (VIVO_GET, VIVO_USE):
            raise CookieExportError("Vivo cookie destination is not allowed")
        if self.cookieRecords is not None:
            return cookies_for_url(self.cookieRecords, endpoint)
        return dict(self.cookies)

    def _request(self, method, url, **kwargs):
        endpoint = url.split('?', 1)[0]
        self._cookies_for_endpoint(endpoint)
        state = (dict(self.cookies), None if self.cookieRecords is None else
                 [dict(cookie) for cookie in self.cookieRecords])
        session = getattr(self, '_http_session', None)
        if session is None or state != self._http_state:
            if session is not None:
                session.close()
            session = requests.Session()
            self._synthetic_cookies = set()
            if self.cookieRecords is None:
                for name, value in self.cookies.items():
                    session.cookies.set(name, value, domain='.vivo.com.cn', path='/', secure=True)
                    self._synthetic_cookies.add((name, '.vivo.com.cn', '/'))
            else:
                for raw in self.cookieRecords:
                    cookie = normalize_cookie(raw)
                    if (cookie['domain'].lstrip('.') != 'vivo.com.cn'
                            and not cookie['domain'].endswith('.vivo.com.cn')):
                        continue
                    if not cookie['session'] and cookie['expires'] <= time.time():
                        continue
                    session.cookies.set_cookie(requests.cookies.create_cookie(
                        cookie['name'], cookie['value'], domain=cookie['domain'],
                        path=cookie['path'], secure=cookie['secure'],
                        expires=None if cookie['session'] else int(cookie['expires']),
                        rest={'HttpOnly': None} if cookie['httpOnly'] else {},
                    ))
            self._http_session = session
            self._http_state = state
        response = session.request(method, url, verify=should_verify_ssl(),
                                   allow_redirects=False, **kwargs)
        received = {(cookie.name, cookie.domain, cookie.path) for cookie in response.cookies}
        for synthetic in tuple(self._synthetic_cookies):
            if any(name == synthetic[0] for name, domain, path in received):
                if synthetic not in received:
                    try:
                        session.cookies.clear(synthetic[1], synthetic[2], synthetic[0])
                    except KeyError:
                        pass
                self._synthetic_cookies.discard(synthetic)
        present = {(cookie.name, cookie.domain, cookie.path) for cookie in session.cookies
                   if not cookie.is_expired()}
        self._synthetic_cookies.intersection_update(present)
        records = [
            {'name': cookie.name, 'value': cookie.value, 'domain': cookie.domain,
             'path': cookie.path, 'secure': bool(cookie.secure),
             'httpOnly': cookie.has_nonstandard_attr('HttpOnly'),
             'expires': cookie.expires if cookie.expires is not None else -1,
             'session': cookie.expires is None}
            for cookie in session.cookies
            if (cookie.domain.lstrip('.') == 'vivo.com.cn'
                or cookie.domain.endswith('.vivo.com.cn')) and not cookie.is_expired()
        ]
        self.cookies = cookies_for_url(records, VIVO_GET)
        cookies_for_url(records, VIVO_USE)
        self.cookieRecords = None if self._synthetic_cookies else records
        if self.cookies != state[0]:
            self.logger.info("VIVO 登录 Cookie 已更新")
        self._http_state = (dict(self.cookies), None if self.cookieRecords is None else
                            [dict(cookie) for cookie in self.cookieRecords])
        return response

    def _capture_browser_cookies(self, browser):
        records = browser.cookie_records
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise CookieExportError("Invalid Vivo browser cookie snapshot")
        records = [dict(item) for item in records]
        compatibility = cookies_for_url(records, VIVO_GET)
        cookies_for_url(records, VIVO_USE)
        self.cookieRecords = records
        self.cookies = compatibility

    def webLogin(self, cookies=None, on_complete=None, *, interactive=True, on_browser_login=None,
                 cookie_records=_UNCHANGED):
        u = f"{VIVO_GET}?gamePackage={self.gamePackage}"
        self.cookies = cookies or {}
        if cookie_records is not _UNCHANGED:
            if cookie_records is not None and (not isinstance(cookie_records, list)
                    or any(not isinstance(item, dict) for item in cookie_records)):
                raise CookieExportError("Invalid saved Vivo cookie records")
            self.cookieRecords = None if cookie_records is None else [dict(item) for item in cookie_records]

        if self.cookieRecords or (self.cookieRecords is None and self.cookies):
            self.logger.info(f"使用本地cookies登录: {u}")
            try:
                r = self._request("GET", u)
                j = r.json()
                if j.get("code") == 0:
                    result = j.get("data") or False
                    if on_complete is not None:
                        on_complete(result)
                        return
                    return result
                if not interactive:
                    if on_complete is not None:
                        on_complete(False)
                    return False
                self.logger.warning("本地cookies登录失败，拉起浏览器重新登录")
            except CookieExportError:
                raise
            except Exception:
                if not interactive:
                    raise

        if not interactive:
            if on_complete is not None:
                on_complete(False)
                return None
            return False

        login_url = f"https://passport.vivo.com.cn/#/login?client_id=67&redirect_uri=https%3A%2F%2Fjoint.vivo.com.cn%2Fgame-subaccount-login%3Ffrom%3Dlogin"
        miBrowser = VivoBrowser(self.gamePackage)
        miBrowser.set_url(login_url)
        resp = miBrowser.run()

        if resp is None:
            # 异步模式：浏览器已显示，等待用户登录完成
            # 必须保持对 browser 的强引用，否则函数返回后局部变量被销毁，
            self._active_browser = miBrowser
            if on_complete is not None:
                def _on_async_done(browser):
                    self._active_browser = None  # 登录完成后释放引用
                    try:
                        result = browser.result
                        if isinstance(result, dict) and result.get("code") == 0:
                            self._capture_browser_cookies(browser)
                            r = self._request("GET", u)
                            j = r.json()
                            if j.get("code") == 0:
                                if j.get("data") and on_browser_login:
                                    on_browser_login()
                                data = j.get("data") or False
                            else:
                                data = False
                        else:
                            data = None if result is None or result == "" else False
                    except Exception as error:
                        data = error
                    on_complete(data)
                miBrowser._async_completion_callback = _on_async_done
            return None

        def _complete(result):
            if on_complete is not None:
                on_complete(result)
            return result
        if not resp:
            return _complete(None)
        try:
            if resp.get("code") == 0:
                self._capture_browser_cookies(miBrowser)
                self.logger.info(u)
                r = self._request("GET", u)
                j = r.json()
                if j.get("code") == 0:
                    if j.get("data") and on_browser_login:
                        on_browser_login()
                    result = j.get("data") or False
                else:
                    result = False
            else:
                result = False
        except Exception as error:
            if on_complete is None:
                raise
            result = error
        return _complete(result)

    def loginSubAccount(self, subOpenId):
        data = {
            "noLoading": True,
            "subOpenId": subOpenId,
            "gamePackage": self.gamePackage,
        }
        header={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36 Edg/105.0.1343.27"
        }
        r = self._request("POST", VIVO_USE, data=data, headers=header)
        resp=r.json()
        if resp.get("code") == 0:
            return resp.get("data")
        else:
            self.logger.error(resp.get("msg"))
            return None
