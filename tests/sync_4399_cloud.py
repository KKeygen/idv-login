#!/usr/bin/env python3
"""4399 渠道云配置同步器（验证通过即写入 cloudRes）

流程:
  1. 读本地 4399 会话凭证 (/tmp/4399_session.json)
  2. 若 state 过期则用 refresh_token 续期 (oauth-getinfobyrefresh.html)
  3. 对候选游戏批量 uni_sauth 验证 (sessionid=state, log_key=各游戏 cloudRes 值)
  4. 验证成功 (code 200) 的游戏自动补 4399com 条目，已存在则跳过
  5. 写回 cloudRes.json (indent=2, ensure_ascii=False, 更新 lastModified)

用法:
  python3 sync_4399_cloud.py            # 默认全部候选
  python3 sync_4399_cloud.py --dry-run  # 只验证不写入
"""
import argparse
import hashlib
import hmac
import json
import random
import ssl
import string
import time
import urllib.parse
import urllib.request

import requests
import urllib3
urllib3.disable_warnings()

CLOUD_PATH = "/root/idv-login/assets/cloudRes.json"
SESSION_PATH = "/tmp/4399_session.json"

# 候选游戏: game_id -> 4399 渠道包名 (scan_cloud_games.cjs 实测)
CANDIDATES = {
    "h55": "com.netease.dwrg.m4399",
    "g37": "com.netease.onmyoji.m4399",
    "g78": "com.netease.moba.m4399",
    "g10": "com.netease.stzb.m4399",
    "g66": "com.netease.mrzh.m4399",
    "g112": "com.netease.dhxy.m4399",
    "ma75": "com.netease.sky.m4399",
    "h75": "com.netease.yhtj.m4399",
    "h65": "com.netease.aceracer.m4399",
    # 网易侧未开 4399 白名单 (uni_sauth 400 invalid param: login_channel):
    # h72 com.netease.yysbwp.m4399 / g126 com.aligames.sgzzlb.m4399
}

SDK_VER = "3.16.0"
APP_KEY = "114816"


class CustomEncoder(json.JSONEncoder):
    def encode(self, obj):
        return super().encode(obj).replace("/", "\\/")


def _ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_my_ip():
    try:
        return requests.get("https://who.nie.netease.com/", verify=False, timeout=10).json().get("ip")
    except Exception:
        return "127.0.0.1"


def refresh_session(sess):
    """refresh_token 全量轮换续期 (母凭据不轮换)"""
    device = {
        "DEVICE_IDENTIFIER": "test-device-001",
        "PLATFORM_TYPE": "android",
        "SDK_VERSION": "3.18.0.51",
        "GAME_KEY": APP_KEY,
        "GAME_VERSION": "1.0.286",
        "GAME_VERSION_CODE": 286,
        "BID": "com.netease.dwrg.m4399",
        "RUNTIME": "android",
        "CANAL_IDENTIFIER": "4399com",
        "UDID": "20260808113900" + "a" * 48,
        "DEBUG": "false",
        "VIP_INFO": "",
        "TEAM": 0,
        "UID": sess["uid"],
        "SCREEN_RESOLUTION": "1080*1920",
        "DEVICE_MODEL": "Pixel 7",
        "SYSTEM_VERSION": "13",
        "NETWORK_TYPE": "wifi",
    }
    body = urllib.parse.urlencode({
        "device": json.dumps(device),
        "refresh_token": sess["refresh_token"],
        "source": "4399",
        "cloud_ext": "",
    })
    req = urllib.request.Request(
        "https://m.4399api.com/openapiv2/oauth-getinfobyrefresh.html",
        data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "User-Agent": "Mozilla/5.0"},
    )
    resp = urllib.request.urlopen(req, timeout=15, context=_ctx())
    data = json.loads(resp.read().decode())
    if data.get("code") != 200:
        raise RuntimeError("refresh failed: %s" % json.dumps(data, ensure_ascii=False)[:200])
    r = data["result"]
    sess["state"] = r["state"]
    sess["access_token"] = r["access_token"]
    sess["oauth_state"] = r["state"].split("|")[4]
    sess["code"] = r.get("code", "")
    sess["saved_at"] = int(time.time())
    sess["expired_at"] = int(time.time()) + sess.get("expires_in", 1296000)
    return sess


def get_sign_src(str1, str2, str3):
    str4 = ""
    replaced = str2.replace("://", "")
    if replaced.find("/") != -1:
        str4 = replaced[replaced.find("/"):]
    return str1.upper() + str4 + str3


def calc_sign(url, method, data, key):
    return hmac.new(key.encode(), get_sign_src(method, url, data).encode(), hashlib.sha256).hexdigest()


def build_sauth(uid, session, game_id, sdk_version, ip, udid):
    fake = {
        "device_model": "M2102K1AC", "os_name": "android", "os_ver": "12",
        "udid": udid, "app_ver": "157",
        "imei": "".join(random.choices(string.digits, k=15)),
        "country_code": "CN", "is_emulator": 0, "is_root": 0, "oaid": "",
    }
    return {
        "gameid": game_id, "login_channel": "4399com", "app_channel": "4399com",
        "platform": "ad", "sdkuid": uid, "udid": udid, "sessionid": session,
        "sdk_version": sdk_version, "is_unisdk_guest": 0, "ip": ip,
        "aim_info": '{"tz":"+0800","tzid":"Asia/Shanghai","aim":"' + ip + '","country":"CN"}',
        "source_app_channel": "4399com", "source_platform": "ad",
        "client_login_sn": "".join(random.choices(string.hexdigits, k=16)),
        "step": "".join(random.choices(string.digits, k=10)),
        "step2": "".join(random.choices(string.digits, k=9)),
        "hostid": 0, "sdklog": json.dumps(fake),
    }


def uni_sauth(data, game_id, log_key):
    url = "https://mgbsdk.matrix.netease.com/%s/sdk/uni_sauth" % game_id
    body = json.dumps(data, cls=CustomEncoder)
    headers = {
        "X-Client-Sign": calc_sign(url, "POST", body, log_key),
        "Content-Type": "application/json",
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; M2102K1AC Build/V417IR)",
    }
    r = requests.post(url, data=body, headers=headers, verify=False, timeout=20)
    return r.status_code, r.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只验证不写入")
    args = ap.parse_args()

    sess = json.load(open(SESSION_PATH))
    if int(time.time()) >= sess.get("expired_at", 0) - 60:
        print("state 过期，refresh 续期 ...")
        sess = refresh_session(sess)
        json.dump(sess, open(SESSION_PATH, "w"), ensure_ascii=False, indent=2)
    uid = sess["uid"]
    state = sess["state"]
    print("uid=%s state_len=%d" % (uid, len(state)))

    cloud = json.load(open(CLOUD_PATH))
    data = cloud["data"]
    logkey = {}
    for item in data:
        gid = item.get("game_id")
        if gid and gid not in logkey:
            logkey[gid] = item.get("log_key")

    ip = _get_my_ip()
    udid = "".join(random.choices(string.hexdigits, k=16))

    ok, fail = [], []
    for gid, pkg in CANDIDATES.items():
        lk = logkey.get(gid)
        if not lk:
            print("[%s] 无 log_key，跳过" % gid)
            continue
        sauth = build_sauth(uid, state, gid, SDK_VER, ip, udid)
        code, text = uni_sauth(sauth, gid, lk)
        try:
            j = json.loads(text)
            status = j.get("code")
            detail = j.get("status", "")
        except Exception:
            status, detail = "?", text[:60]
        if status == 200:
            ok.append(gid)
            print("[%s] OK   %s (aid=%s)" % (gid, pkg, j.get("aid", "?")))
        else:
            fail.append((gid, status, detail))
            print("[%s] FAIL %s -> code=%s %s" % (gid, pkg, status, detail))

    if args.dry_run:
        print("")
        print("[dry-run] 不写入。成功 %d / 失败 %d" % (len(ok), len(fail)))
        return

    added = 0
    for gid in ok:
        exists = any(i.get("game_id") == gid and i.get("app_channel") == "4399com" for i in data)
        if exists:
            print("[%s] 条目已存在，跳过写入" % gid)
            continue
        data.append({
            "package_name": CANDIDATES[gid],
            "app_channel": "4399com",
            "log_key": logkey[gid],
            "game_id": gid,
            "4399com": {"app_key": APP_KEY, "channel_id": "4399com", "sdk_ver": SDK_VER},
            "name": "4399账号",
        })
        added += 1
        print("[%s] + 写入 4399com 条目" % gid)

    if added:
        cloud["lastModified"] = int(time.time())
        json.dump(cloud, open(CLOUD_PATH, "w"), ensure_ascii=False, indent=2)
        print("")
        print("已写入 %d 条，cloudRes total=%d" % (added, len(data)))
    else:
        print("")
        print("无新增（全部已存在）")


if __name__ == "__main__":
    main()
