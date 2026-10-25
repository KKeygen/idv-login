import json
import random
import string
import hmac
import hashlib

import requests
import app_state
from ssl_utils import should_verify_ssl

class CustomEncoder(json.JSONEncoder):
    def encode(self, obj):
        json_str = super().encode(obj)
        return json_str.replace('/', '\\/')


def _get_my_ip():
        #get my IP
    try:
        return requests.get("https://who.nie.netease.com/", verify=should_verify_ssl()).json().get("ip")
    except Exception as e:
        return "127.0.0.1"

def get_sign_src(str1, str2, str3):
    str4 = ""
    replaced = str2.replace("://", "")
    if replaced.find("/") != -1:
        str4 = replaced[replaced.find("/"):]
    return str1.upper() + str4 + str3

def calcSign(url,method,data,key):
    src=get_sign_src(method,url,data)
    #sha256
    return hmac.new(key.encode(), src.encode(), hashlib.sha256).hexdigest()

def buildSAUTH(login_channel, app_channel,uid,session,game_id,sdk_version,custom_data={}):
    fakeData = app_state.fake_device
    ip=_get_my_ip()
    data = {
        "gameid": game_id,#maybe works for all games
        "login_channel": login_channel,
        "app_channel": app_channel,
        "platform": "ad",
        "sdkuid": uid,
        "udid": fakeData["udid"],
        "sessionid": session,
        "sdk_version": sdk_version,
        "is_unisdk_guest": 0,
        "ip": ip,
        "aim_info": '{"tz":"+0800","tzid":"Asia/Shanghai","aim":"'+ip+'","country":"CN"}',
        "source_app_channel": app_channel,
        "source_platform": "ad",
        "client_login_sn": "".join(random.choices((string.hexdigits), k=16)),
        "step": "".join(random.choices(string.digits, k=10)),
        "step2": "".join(random.choices(string.digits, k=9)),
        "hostid": 0,
        "sdklog": json.dumps(fakeData),
    }
    data.update(custom_data)
    return data


def postSignedData(data,game_id,need_custom_encode=False):
    url=f"https://mgbsdk.matrix.netease.com/{game_id}/sdk/uni_sauth"
    method="POST"
    from cloudRes import CloudRes
    key=CloudRes().get_by_game_id_and_key(game_id,"log_key")
    if need_custom_encode:
        data=json.dumps(data,cls=CustomEncoder)
    else:
        data=json.dumps(data)
    headers={"X-Client-Sign":calcSign(url,method,data,key),
             "Content-Type":"application/json",
             "User-Agent":"Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)",}
    r=requests.post(url,data=data,headers=headers,verify=should_verify_ssl())
    return r.json()

def getShortGameId(game_id):
    return game_id.split("-")[-1]

def cmp_game_id(game_id_a, game_id_b):
    if game_id_a == "" or game_id_b == "":
        return game_id_a == game_id_b
    short_a = getShortGameId(game_id_a)
    short_b = getShortGameId(game_id_b)
    return short_a == short_b




#jsodn=r'''{"gameid":"g37","login_channel":"huawei","app_channel":"huawei","platform":"ad","sdkuid":"2850086000509138399","udid":"ff6030012b3b7523","sessionid":"XhsKeIsuSahyroMt4\/h20maxXw4U7zsrjUzm6M76EY074yS6DdLOob7upxgVpmkuO8ctydVfUqEHJHh9C3hAtlM9Rw8+aawKIbKH3lbi7KIkuy48zAjfTHSNpzrAh5OgMhgmtR0IsriqKsLyLEftt\/VWZVLgpXQtLnJ+U+\/wRFF+bdKJJgSlYIMrwKNS8RnpYd1GwEtQbsXJll9QWYSWfbzh3eSYGhJNlaRcO+kTLfgFC69oqCxKo0t1hhXINO\/5O8QomaUqNqdXHSKZNda+OtOYcaR79+YsIASomS9UM0JOuLX7rEGtuu6L4SVJuj9zq3J4otjQy4p6GvCvn46dsg==","sdk_version":"6.8.0.300","is_unisdk_guest":0,"ip":"117.182.131.79","aim_info":"{\"tz\":\"+0800\",\"tzid\":\"Asia\\\/Shanghai\",\"country\":\"CN\",\"aim\":\"117.182.131.79\"}","source_app_channel":"huawei","source_platform":"ad","extra_data":"{\"playerLevel\":\"1\",\"sdk_info\":{\"openid\":\"MDFAMTA1MzExODlAYzEyNGY3MzFlZmIyYTUyNTViaYjhkYTdiaMDMxMGU3NzJANjcyNjg5OTM5NDdhYWNhZjEwMTZjOGZlM2ZkMjJhM2Y1OGIwNTZjYzY2MTE4ZmVmZTFjNjMyNjU\",\"accessToken\":\"DQEAAPQETsGlqdrFHSaDsBPLzjXbXmy7JHnCYfGcO3FyqUfqZfclJ+SduEr81L9e5u3Fuoxv\\\/gGzpvF0Q5lRjsBORmqk18R2BoveWRr7U3KD\",\"transtition_version\":1}}","get_access_token":"1","timestamp":"1722481565653","realname":"{\"realname_type\":0}","client_login_sn":"7b26ec3b046d99ccc03af95334d35dc1","step":"-872582536","step2":"884046348","hostid":0,"sdklog":"{\"device_model\":\"BLA-AL00\",\"os_name\":\"android\",\"os_ver\":\"12\",\"udid\":\"ff6030012b3b7523\",\"app_ver\":\"240131\",\"imei\":\"\",\"country_code\":\"CN\",\"is_emulator\":1,\"is_root\":1,\"oaid\":\"\"}"}'''
#print(calcSign("https://mgbsdk.matrix.netease.com/g37/sdk/uni_sauth","POST",(jsodn)))