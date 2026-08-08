#!/usr/bin/env python3
"""补 h72 的 xiaomi_app + oppo 渠道（ssr 代号渠道包实测提取）"""
import json
import time

CLOUD_PATH = "/root/idv-login/assets/cloudRes.json"

NEW = [
    {
        "package_name": "com.netease.yysbwp.mi",
        "app_channel": "xiaomi_app",
        "log_key": "F3ylEK9qT1xNHZvf_A9k_kA4F1kKVwis",
        "game_id": "h72",
        "xiaomi_app": "mi_2882303761517947987",
        "channel": "xiaomi_app",
        "name": "小米账号",
    },
    {
        "package_name": "com.netease.yysbwp.nearme.gamecenter",
        "app_channel": "oppo",
        "log_key": "F3ylEK9qT1xNHZvf_A9k_kA4F1kKVwis",
        "game_id": "h72",
        "oppo": "com.netease.yysbwp.nearme.gamecenter",
    },
]

cloud = json.load(open(CLOUD_PATH))
data = cloud["data"]
added = 0
for entry in NEW:
    exists = any(i.get("game_id") == entry["game_id"] and i.get("app_channel") == entry["app_channel"] for i in data)
    if exists:
        print("[%s] %s 已存在，跳过" % (entry["game_id"], entry["app_channel"]))
        continue
    data.append(entry)
    added += 1
    print("[+] %s/%s %s" % (entry["game_id"], entry["app_channel"], entry.get("xiaomi_app") or entry.get("oppo")))

if added:
    cloud["lastModified"] = int(time.time())
    json.dump(cloud, open(CLOUD_PATH, "w"), ensure_ascii=False, indent=2)
print("新增 %d 条，total=%d" % (added, len(data)))
