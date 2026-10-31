import json
import os
import random
import sqlite3
import string
import tempfile
import time
import shutil

import gevent
import channelHandler.miLogin.utils as utils
import requests
import sys
from faker import Faker
import random
import webbrowser
import pyperclip as cb

from channelHandler.miLogin.consts import DEVICE, DEVICE_RECORD, AES_KEY
from channelHandler.channelUtils import G_clipListener
from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.WebLoginUtils import WebBrowser



class VivoBrowser(WebBrowser):
    def __init__(self, gamePackage):
        super().__init__("nearme_vivo", True)
        self.logger = setup_logger()
        self.gamePackage = gamePackage

    def verify(self, url: str) -> bool:
        return "openid" in self.parse_url_query(url).keys()

    def _snapshot_cookie_db(self, db_path: str):
        tmp_fd, tmp_db = tempfile.mkstemp(prefix="vivo_cookies_", suffix=".sqlite")
        os.close(tmp_fd)
        try:
            src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            try:
                dst_conn = sqlite3.connect(tmp_db)
                try:
                    src_conn.backup(dst_conn)
                    return tmp_db
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
        except Exception as e:
            self.logger.debug(f"SQLite backup快照失败，尝试文件拷贝快照: {e}")

        copied = False
        last_copy_error = None
        for _ in range(60):
            try:
                shutil.copy2(db_path, tmp_db)
                wal_src = db_path + "-wal"
                shm_src = db_path + "-shm"
                if os.path.exists(wal_src):
                    shutil.copy2(wal_src, tmp_db + "-wal")
                if os.path.exists(shm_src):
                    shutil.copy2(shm_src, tmp_db + "-shm")
                copied = True
                break
            except Exception as e:
                last_copy_error = e
                gevent.sleep(0.1)
        if copied:
            return tmp_db
        if last_copy_error is not None:
            self.logger.debug(f"文件拷贝快照仍失败(可能句柄未释放): {last_copy_error}")
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except Exception:
                pass
        return None

    def export_cookie(self):
        cookie_map = self.cookies.copy()
        base_path = getattr(self, "_persistent_storage_path", "")
        db_path = os.path.join(base_path, "Cookies") if base_path else ""
        if not os.path.exists(db_path):
            return cookie_map

        tmp_db = None
        conn = None
        try:
            tmp_db = self._snapshot_cookie_db(db_path)
            if not tmp_db:
                return cookie_map
            conn = sqlite3.connect(tmp_db)
            cursor = conn.execute("SELECT host_key, name, value FROM cookies")
            for _, name, value in cursor:
                if name and value is not None:
                    cookie_map[name] = value
        except Exception as e:
            self.logger.warning(f"从SQLite读取cookie失败，回退内存cookie: {e}")
        finally:
            if conn is not None:
                conn.close()
            if tmp_db and os.path.exists(tmp_db):
                try:
                    os.remove(tmp_db)
                except Exception:
                    pass
            if tmp_db and os.path.exists(tmp_db + "-wal"):
                try:
                    os.remove(tmp_db + "-wal")
                except Exception:
                    pass
            if tmp_db and os.path.exists(tmp_db + "-shm"):
                try:
                    os.remove(tmp_db + "-shm")
                except Exception:
                    pass
        self.cookies = cookie_map
        return cookie_map

    def parseReslt(self, url):
        self.result = {"code": 0, "data": {"redirect_url": url}}
        return True

    def parse_url_query(self, url):
        from urllib.parse import urlparse, parse_qs

        parsed_url = urlparse(url)
        query_dict = parse_qs(parsed_url.query)
        return query_dict


class VivoLogin:
    def __init__(self, gamePackage=""):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.gamePackage = gamePackage
        self.cookies = {}

    def _request(self, method, url, **kwargs):
        session = getattr(self, "_http_session", None)
        if session is None or self.cookies != self._http_cookies:
            if session is not None:
                session.close()
            session = requests.Session()
            # Keep legacy name/value records compatible, scoped to vivo rather
            # than Requests' unrestricted domainless cookies. A live session
            # retains domain/path information received after this snapshot.
            for name, value in self.cookies.items():
                session.cookies.set(name, value, domain=".vivo.com.cn", path="/", secure=True)
            self._http_session = session
            self._http_cookies = self.cookies.copy()
        response = session.request(
            method, url, verify=should_verify_ssl(), **kwargs
        )
        # Requests applies Set-Cookie from all redirects and the final response.
        # Never flatten cookies from another redirect host into a saved vivo
        # record: on restore that would incorrectly rebind them to vivo.
        self.cookies = {
            cookie.name: cookie.value for cookie in session.cookies
            if cookie.domain.lstrip(".") == "vivo.com.cn"
            or cookie.domain.endswith(".vivo.com.cn")
        }
        if self.cookies != self._http_cookies:
            self.logger.info("VIVO 登录 Cookie 已更新")
        self._http_cookies = self.cookies.copy()
        return response

    def webLogin(self, cookies=None):
        u = f"https://joint.vivo.com.cn/h5/union/get?gamePackage={self.gamePackage}"
        self.cookies = cookies or {}

        if self.cookies:
            self.logger.info(f"使用本地cookies登录: {u}")
            try:
                r = self._request("GET", u)
                j = r.json()
                if j.get("code") == 0:
                    return j.get("data")
                self.logger.warning("本地cookies登录失败，拉起浏览器重新登录")
            except Exception as e:
                self.logger.warning(f"本地cookies请求异常，拉起浏览器重新登录: {e}")

        login_url = f"https://passport.vivo.com.cn/#/login?client_id=67&redirect_uri=https%3A%2F%2Fjoint.vivo.com.cn%2Fgame-subaccount-login%3Ffrom%3Dlogin"
        miBrowser = VivoBrowser(self.gamePackage)
        miBrowser.set_url(login_url)
        resp = miBrowser.run()
        try:
            if resp.get("code") == 0:
                # 浏览器退出后再读取Cookies数据库，显著降低Windows文件锁概率
                self.cookies = miBrowser.export_cookie().copy()
                self.logger.info(u)
                r = self._request("GET", u)
                j = r.json()
                if j.get("code") == 0:
                    return j.get("data")
                self.logger.error(j.get("msg"))
                return None
            else:
                self.logger.error(resp.get("msg"))
                return None
        except:
            self.logger.error(f"登录失败，原始响应{resp}")
            return None

    def loginSubAccount(self, subOpenId):
        data = {
            "noLoading": True,
            "subOpenId": subOpenId,
            "gamePackage": self.gamePackage,
        }
        header={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36 Edg/105.0.1343.27"
        }
        r = self._request("POST", "https://joint.vivo.com.cn/h5/union/use", data=data, headers=header)
        try:
            resp=r.json()
            if resp.get("code") == 0:
                return resp.get("data")
            else:
                self.logger.error(resp.get("msg"))
                return None
        except:
            self.logger.exception(f"登录失败")
            return None
