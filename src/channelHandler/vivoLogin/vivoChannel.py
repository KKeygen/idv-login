import os
import sqlite3
import tempfile
import time
import shutil

import requests

from logutil import setup_logger
from ssl_utils import should_verify_ssl
from channelHandler.WebLoginUtils import WebBrowser



class VivoBrowser(WebBrowser):
    def __init__(self, gamePackage):
        super().__init__("nearme_vivo", True, frameless=True)
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
                time.sleep(0.1)
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
        self._active_browser: VivoBrowser = None  # 异步模式下保持强引用

    def webLogin(self, cookies=None, on_complete=None):
        u = f"https://joint.vivo.com.cn/h5/union/get?gamePackage={self.gamePackage}"
        self.cookies = cookies or {}

        if self.cookies:
            self.logger.info(f"使用本地cookies登录: {u}")
            try:
                r = requests.get(u, cookies=self.cookies, verify=should_verify_ssl())
                j = r.json()
                if j.get("code") == 0:
                    result = j.get("data")
                    if on_complete is not None:
                        on_complete(result)
                        return
                    return result
                self.logger.warning("本地cookies登录失败，拉起浏览器重新登录")
            except Exception as e:
                self.logger.warning(f"本地cookies请求异常，拉起浏览器重新登录: {e}")

        login_url = f"https://passport.vivo.com.cn/#/login?client_id=67&redirect_uri=https%3A%2F%2Fjoint.vivo.com.cn%2Fgame-subaccount-login%3Ffrom%3Dlogin"
        miBrowser = VivoBrowser(self.gamePackage)
        miBrowser.set_url(login_url)
        resp = miBrowser.run()

        if resp is None:
            # 异步模式：浏览器已显示，等待用户登录完成
            # 必须保持对 browser 的强引用，否则函数返回后局部变量被销毁，
            # 导致 profile 被释放而 WebEnginePage 仍在运行。
            self._active_browser = miBrowser
            if on_complete is not None:
                def _on_async_done(browser):
                    self._active_browser = None  # 登录完成后释放引用
                    try:
                        result = browser.result
                        if isinstance(result, dict) and result.get("code") == 0:
                            self.cookies = browser.export_cookie().copy()
                            r = requests.get(u, cookies=self.cookies, verify=should_verify_ssl())
                            j = r.json()
                            if j.get("code") == 0:
                                on_complete(j.get("data"))
                                return
                        on_complete(None)
                    except Exception:
                        self.logger.exception("Vivo异步登录处理失败")
                        on_complete(None)
                miBrowser._async_completion_callback = _on_async_done
            return None

        try:
            if resp.get("code") == 0:
                # 浏览器退出后再读取Cookies数据库，显著降低Windows文件锁概率
                self.cookies = miBrowser.export_cookie().copy()
                self.logger.info(u)
                r = requests.get(u, cookies=self.cookies, verify=should_verify_ssl())
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
        r = requests.post("https://joint.vivo.com.cn/h5/union/use",data=data,cookies=self.cookies,headers=header,verify=should_verify_ssl())
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
