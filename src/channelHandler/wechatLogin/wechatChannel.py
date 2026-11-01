import os
import time
import requests

from logutil import setup_logger
from ssl_utils import should_verify_ssl
from envmgr import genv

#1106682786 is offerid
def sig_helper(magicValue="5C2F##3[6$^(68#%#D3E96;]35q#FB46",ts="1"):
    #用时间戳作为盐值，对magicValue进行md5
    from hashlib import md5
    return md5((ts+magicValue).encode())
    


class WechatLogin:
    def __init__(self,wx_appid,channel,refreshToken="", game_id=""):
        os.chdir(os.path.join(os.environ["PROGRAMDATA"], "idv-login"))
        self.logger = setup_logger()
        self.wx_appid=wx_appid
        self.channel=channel
        self.refreshToken = refreshToken
        self.game_id = game_id

    def _update_qrcode_cache(self, status, qrcode_base64="", uuid=""):
        cache = genv.get("WECHAT_QRCODE_CACHE", {})
        if not isinstance(cache, dict):
            cache = {}
        cache[self.game_id if self.game_id else "_default"] = {
            "status": status,
            "qrcode_base64": qrcode_base64,
            "uuid": uuid,
            "timestamp": int(time.time()),
        }
        genv.set("WECHAT_QRCODE_CACHE", cache)

    def webLogin(self, *, cancel_event=None, on_qrcode=None):
        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def publish(status, qrcode_base64="", uuid=""):
            if cancelled():
                return
            if on_qrcode is not None:
                on_qrcode({"status": status, "qrcode_base64": qrcode_base64,
                           "uuid": uuid, "timestamp": int(time.time())})
            else:
                self._update_qrcode_cache(status, qrcode_base64, uuid)

        if cancelled():
            return None
        publish("loading")
        ts=str(int(time.time()*1000))
        qrcodeData = {
            "noncestr":"!!freeSoftwareDoNOTSell-idv-login!!"+ts,
            "scope":"snsapi_userinfo,snsapi_friend,snsapi_message",
            "wx_appid":self.wx_appid,
            "loginplatform":"2",
            "sig":sig_helper(ts=ts).hexdigest(),
            "appid":self.wx_appid,
            "timestamp":ts
        }
        
        r=requests.get(f"https://ysdk.qq.com/auth/wx_scan_code_login",params=qrcodeData,
                       verify=should_verify_ssl(), timeout=(5, 15))
        if cancelled():
            return None
        if not r.status_code==200 or not r.json()['ret']==0:
            self.logger.error(f"微信扫码请求创建失败, status={r.status_code}, ret={r.json().get('ret')}")
            return False
        #删除响应json里的msg字段
        rjson=r.json()
        rjson.pop("msg")
        rjson.pop("ret")
        #https://open.weixin.qq.com/connect/sdk/qrconnect
        r=requests.get(f"https://open.weixin.qq.com/connect/sdk/qrconnect",params=rjson,
                       headers={"accept-encoding":"gzip",},verify=should_verify_ssl(), timeout=(5, 15))
        if cancelled():
            return None
        #use utf-8
        r.encoding="utf-8"

        #get qrcode img
        qrcode=r.json().get("qrcode").get("qrcodebase64")
        uuid=r.json().get("uuid")
        publish("ready", qrcode_base64=qrcode, uuid=uuid)


        while True:
            if cancelled():
                return None
            r=requests.get(f"https://long.open.weixin.qq.com/connect/l/qrconnect?f=json&uuid={uuid}",
                           verify=should_verify_ssl(), timeout=(5, 35))
            if cancelled():
                return None
            poll = r.json()
            status = str(poll.get("wx_errcode", ""))
            if status == "403":
                publish("cancelled", uuid=uuid)
                return None
            if status == "402":
                publish("failed", uuid=uuid)
                return False
            if r.status_code != 200:
                publish("failed", uuid=uuid)
                return False
            if poll.get("wx_code"):
                self.logger.info("微信扫码成功")
                publish("scanned", uuid=uuid)
                break
            if cancel_event is not None:
                if cancel_event.wait(1):
                    return None
            else:
                time.sleep(1)

        verifyData={
            "channel":"00000000",
            "code":r.json().get("wx_code"),
            "offerid":self.channel,
            "platform":"desktop_m_wechat",
            "client_hope_switch":"1",
            "wx_appid":self.wx_appid,
            "version":"2.2.2",
            "sig":sig_helper(ts=ts).hexdigest(),
            "anti_hope_switch":"1",
            "appid":self.wx_appid,
            "timestamp":ts
        }
        if cancelled():
            return None
        r=requests.get("https://ysdk.qq.com/auth/wx_verify_code",params=verifyData,
                       verify=should_verify_ssl(), timeout=(5, 15))
        if cancelled():
            return None
        if not r.status_code==200 or not r.json()['ret']==0:
            self.logger.error(f"扫码校验失败, status={r.status_code}, ret={r.json().get('ret')}")
            publish("failed", uuid=uuid)
            return False
        publish("verified", uuid=uuid)
        return r.json()


