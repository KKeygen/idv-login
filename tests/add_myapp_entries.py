#!/usr/bin/env python3
"""补 6 个游戏的 myapp(应用宝) 条目：基于应用宝渠道包 ysdkconf.ini 实测提取"""
import json
import time

CLOUD_PATH = "/root/idv-login/assets/cloudRes.json"

# game_id -> (应用宝渠道包名, OFFER_ID, WX_APP_ID)
MYAPP_DATA = {
    "g10": ("com.tencent.tmgp.stzb", "1104833445", "wxc683ec8b0a09da63"),
    "g67": ("com.tencent.tmgp.g67", "1112097427", "wx0fbefe423ec05da7"),
    "g92": ("com.tencent.tmgp.harrypotter", "1111980517", "wx6e319e486f89d163"),
    "h65": ("com.tencent.tmgp.aceracer", "1111869422", "wxbc4f709620735d51"),
    "h72": ("com.tencent.tmgp.yysbwp", "1109203221", "wx9534412afab38dd9"),
    "h75": ("com.tencent.tmgp.ef3", "1109835678", "wx7683a69f52d22e3c"),
}

cloud = json.load(open(CLOUD_PATH))
data = cloud["data"]

logkey = {}
for item in data:
    gid = item.get("game_id")
    if gid and gid not in logkey:
        logkey[gid] = item.get("log_key")

added = 0
for gid, (pkg, offer, wx) in MYAPP_DATA.items():
    exists = any(i.get("game_id") == gid and i.get("app_channel") == "myapp" for i in data)
    if exists:
        print("[%s] myapp 已存在，跳过" % gid)
        continue
    entry = {
        "package_name": pkg,
        "app_channel": "myapp",
        "log_key": logkey.get(gid, ""),
        "game_id": gid,
        "myapp": {"channel": offer, "wx_appid": wx},
        "channel": "myapp",
        "name": "应用宝（微信）",
    }
    data.append(entry)
    added += 1
    print("[+] %s/myapp pkg=%s channel=%s" % (gid, pkg, offer))

if added:
    cloud["lastModified"] = int(time.time())
    json.dump(cloud, open(CLOUD_PATH, "w"), ensure_ascii=False, indent=2)
print("新增 %d 条，total=%d" % (added, len(data)))
