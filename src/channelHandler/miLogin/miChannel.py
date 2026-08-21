import json
import os
import random
import string
import time
import channelHandler.miLogin.utils as utils
import requests
from faker import Faker
from PyQt6.QtWidgets import QPushButton

from channelHandler.miLogin.consts import DEVICE, DEVICE_RECORD, AES_KEY
from logutil import setup_logger
from channelHandler.WebLoginUtils import WebBrowser
from ssl_utils import should_verify_ssl




class MiBrowser(WebBrowser):
    def __init__(self):
        super().__init__("xiaomi_app", False, frameless=True)
        self.isQQ=False

        # 无边框窗口使用悬浮按钮，不占用网页布局。
        self.qq_login_button = QPushButton("QQ 登录", self)
        self.qq_login_button.setObjectName("xiaomi_qq_login_button")
        self.qq_login_button.setFixedSize(72, 28)
        self.qq_login_button.setToolTip("使用 QQ 登录小米渠道")
        self.qq_login_button.setStyleSheet(
            "QPushButton#xiaomi_qq_login_button {"
            "  background-color: rgba(18, 111, 214, 220);"
            "  color: white;"
            "  border: none;"
            "  border-radius: 14px;"
            "  font-size: 12px;"
            "  font-weight: 500;"
            "}"
            "QPushButton#xiaomi_qq_login_button:hover {"
            "  background-color: rgba(32, 129, 235, 240);"
            "}"
            "QPushButton#xiaomi_qq_login_button:pressed {"
            "  background-color: rgba(12, 85, 170, 245);"
            "}"
        )
        self.qq_login_button.clicked.connect(self.qq_login)
        self.qq_login_button.raise_()
        self._reposition_qq_login_button()

    def _reposition_qq_login_button(self):
        button = getattr(self, "qq_login_button", None)
        close_button = getattr(self, "_frameless_close_button", None)
        if button is None or close_button is None:
            return
        button.move(
            close_button.x() - button.width() - 8,
            close_button.y(),
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_qq_login_button()

    def verify(self, url: str) -> bool:
        if self.isQQ:
            from urllib.parse import urlparse, parse_qs
            parsed_url = urlparse(url)
            if parsed_url.netloc=="imgcache.qq.com" and parsed_url.path=="/open/connect/widget/mobile/login/proxy.htm":
                query_dict = parse_qs(parsed_url.fragment)
                return "access_token" in query_dict.keys()
            return False
        return "code" in self.parse_url_query(url).keys()

    def parseReslt(self, url):
        if self.isQQ:
            from urllib.parse import urlparse, parse_qs
            parsed_url = urlparse(url)
            query_dict = parse_qs(parsed_url.fragment)
            self.result = query_dict,self.isQQ
        else:
            self.result = self.parse_url_query(url).get("code")[0],self.isQQ
        return True

    def parse_url_query(self,url):
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(url)
        query_dict = parse_qs(parsed_url.query)
        return query_dict
    
    def handle_url_change(self, url):
        super().handle_url_change(url)
        if "graph.qq.com" in url.toString():
            self.isQQ=True
            self.logger.info(f"识别到小米渠道QQ登录")
            self.set_url("https://openmobile.qq.com/oauth2.0/m_authorize?client_id=1106134065&scope=all&redirect_uri=auth://tauth.qq.com/&style=qr&response_type=token")
        if url.toString().startswith("https://game.xiaomi.com/") and "oauthcallback" not in url.toString():
            self.set_url(f"https://account.xiaomi.com/oauth2/authorize?client_id=2882303761517516898&response_type=code&scope=1%203&redirect_uri=http%3A%2F%2Fgame.xiaomi.com%2Foauthcallback%2Fmioauth&state={generate_md5(str(time.time()))[0:16]}")
    
    def qq_login(self):
        """QQ登录方法"""
        self.isQQ = True
        self.logger.info(f"手动切换到小米渠道QQ登录")
        self.set_url("https://openmobile.qq.com/oauth2.0/m_authorize?client_id=1106134065&scope=all&redirect_uri=auth://tauth.qq.com/&style=qr&response_type=token")

def generate_fake_data():
    fake = Faker()
    manufacturers = ["Samsung", "Huawei", "Xiaomi", "OPPO"]
    architectures = ["32", "64"]

    manufacturer = random.choice(manufacturers)
    model = fake.lexify(text="SM-????", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    os_version = fake.lexify(
        text="??_stable_??", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    build_id = fake.lexify(
        text="V???IR release-keys", letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    architecture = random.choice(architectures)
    return f"{manufacturer}|{model}|{os_version}|{build_id}|{architecture}|{model}"


import hashlib


def generate_md5(input_string):
    md5_hash = hashlib.md5()
    md5_hash.update(input_string.encode("utf-8"))
    return md5_hash.hexdigest()


class MiLogin:
    def __init__(self, appId, oauthData=None,account_type=4):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.appId = appId
        self.oauthData = oauthData
        self.account_type=account_type
        self._active_browser: MiBrowser = None  # 异步模式下保持强引用

        if os.path.exists(DEVICE_RECORD):
            with open(DEVICE_RECORD, "r", encoding="utf-8") as f:
                self.device = json.load(f)
        else:
            self.device = self.makeFakeDevice()
            from secure_write import write_json_restricted
            write_json_restricted(DEVICE_RECORD, self.device)

    def initAccountData(self) -> object:
        if self.oauthData == None:
            self.webLogin()
        params = {
            "fuid": self.oauthData["uuid"],  # 用户ID
            "devAppId": self.appId,  # apk中的appid
            "toke": self.oauthData["st"],
        }
        params.update(self.device)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "close",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)",
            "Host": "account.migc.g.mi.com",
            "Accept-Encoding": "gzip",
        }
        response = requests.post(
            "http://account.migc.g.mi.com/migc-sdk-account/getLoginAppAccount_v2",
            data=utils.generate_unsign_request(params, AES_KEY),
            headers=headers,
            verify=should_verify_ssl()
        )
        res = utils.decrypt_response(response.text, AES_KEY)
        if res["retCode"] == 200:
            return res
        else:
            self.logger.error(f"InitAccountData 失败, retCode={res.get('retCode')}")
            raise Exception("Init account data failed")

    def getSTbyQQResp(self,qAuthResp):
        params = {
            "accountType": self.account_type,
            "openid":qAuthResp.get("openid")[0],
            "accessToken":qAuthResp.get("access_token")[0],
            "isSaveSt": "true",
            "appid": "2000202",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "close",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)",
            "Host": "account.migc.g.mi.com",
            "Accept-Encoding": "gzip",
        }
        response = requests.get(
            "http://account.migc.g.mi.com/misdk/v2/oauth",
            params=utils.generate_unsign_request(params, AES_KEY),
            headers=headers,
            verify=should_verify_ssl()
        )
        res = utils.decrypt_response(response.text, AES_KEY)
        if res["code"] == 0:
            self.oauthData = res
            return res
        else:
            self.logger.error(f"小米QQ登录失败, code={res.get('code')}")
            self.oauthData=None
            return None

    def getSTbyCode(self, code) -> None:
        params = {
            "accountType": self.account_type,
            "code": code,
            "isSaveSt": "true",
            "appid": "2000202",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "close",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)",
            "Host": "account.migc.g.mi.com",
            "Accept-Encoding": "gzip",
        }
        response = requests.get(
            "http://account.migc.g.mi.com/misdk/v2/oauth",
            params=utils.generate_unsign_request(params, AES_KEY),
            headers=headers,
            verify=should_verify_ssl()
        )
        res = utils.decrypt_response(response.text, AES_KEY)
        if res["code"] == 0:
            self.oauthData = res
            return res
        else:
            self.logger.error(f"小米Code登录失败, code={res.get('code')}")
            self.oauthData=None
            return None

    def webLogin(self, on_complete=None):
        login_url = "http://account.xiaomi.com/fe/service/login/password?sid=newgamecenterweb&qs=%253Fsid%253Dnewgamecenterweb%2526callback%253Dhttps%25253A%25252F%25252Fgame.xiaomi.com%25252Fauth%25252Fmi_login&callback=https%3A%2F%2Fgame.xiaomi.com%2Fauth%2Fmi_login&_sign=GDzEamQvXougqttdJc8mC0nEyRA%3D&serviceParam=%7B%22checkSafePhone%22%3Afalse%2C%22checkSafeAddress%22%3Afalse%2C%22lsrp_score%22%3A0.0%7D&showActiveX=false&theme=&needTheme=false&bizDeviceType=&_locale=zh_CN"
        miBrowser=MiBrowser()
        miBrowser.set_url(login_url)
        result=miBrowser.run()

        if result is None:
            # 异步模式：浏览器已显示，等待用户登录完成
            # 必须保持对 browser 的强引用，否则函数返回后局部变量被销毁，
            # 导致 profile 被释放而 WebEnginePage 仍在运行。
            self._active_browser = miBrowser
            if on_complete is not None:
                def _on_async_done(browser):
                    self._active_browser = None  # 登录完成后释放引用
                    try:
                        if not browser.result or not isinstance(browser.result, tuple):
                            self.oauthData = None
                            on_complete(None)
                            return
                        resp, isQQ = browser.result
                        if isQQ:
                            self.account_type = 2
                            on_complete(self.getSTbyQQResp(resp))
                        else:
                            on_complete(self.getSTbyCode(resp))
                    except Exception:
                        self.logger.exception("小米异步登录处理失败")
                        self.oauthData = None
                        on_complete(None)
                miBrowser._async_completion_callback = _on_async_done
            return None

        resp, isQQ=result
        if isQQ:
            self.account_type=2
            return self.getSTbyQQResp(resp)
        return self.getSTbyCode(resp)

    def makeFakeDevice(self):
        device = DEVICE.copy()
        device["imei"] = utils.aes_encrypt(
            "".join(random.choices(string.ascii_letters + string.digits, k=8)),
            str(time.time())[0:16],
        )[0:8]
        device["imeiMd5"] = generate_md5(device["imei"])
        device["ua"] = generate_fake_data()
        return device
