#!/usr/bin/env python3
"""批量补全 cloudRes 渠道配置：基于实测提取的数据生成条目

数据来源:
- /tmp/channel_extract2.json (渠道 _data 提取: APPID/APP_KEY/APP_SECRET)
- /tmp/channel_pkgs.json (AXML 精确包名)
- /tmp/channel_scan.json (文件清单)
- ysdkconf 提取 (g78 myapp)
"""
import json
import sys

CLOUD_PATH = "/root/idv-login/assets/cloudRes.json"

# 从提取结果构造的新条目
# 格式: game_id -> [(app_channel, package_name, 渠道字段 dict, name)]
NEW_ENTRIES = [
    # g112 大话西游: oppo
    ("g112", "oppo", "com.netease.dhxy.nearme.gamecenter", {"oppo": "com.netease.dhxy.nearme.gamecenter"}, None),
    # g37 阴阳师: oppo + bilibili_sdk
    ("g37", "oppo", "com.netease.onmyoji.nearme.gamecenter", {"oppo": "com.netease.onmyoji.nearme.gamecenter"}, None),
    ("g37", "bilibili_sdk", "com.netease.onmyoji.bili",
     {"bilibili_sdk": {"bili_game_id": "166", "app_key": "8dd76735d22d4191b4eefdc3152f79ac", "sdk_ver": ""}},
     "哔哩哔哩"),
    # g66 明日之后: oppo
    ("g66", "oppo", "com.netease.mrzh.nearme.gamecenter", {"oppo": "com.netease.mrzh.nearme.gamecenter"}, None),
    # g78 决战平安京: xiaomi_app + myapp
    ("g78", "xiaomi_app", "com.netease.moba.mi",
     {"xiaomi_app": "mi_2882303761517629369", "channel": "xiaomi_app"}, "小米账号"),
    ("g78", "myapp", "com.tencent.tmgp.kaopu.jzpaj",
     {"myapp": {"channel": "1106561332", "wx_appid": "wxa7e372b1c9bb6cbb"}, "channel": "myapp"}, "应用宝（微信）"),
    # ma75 光遇: oppo
    ("ma75", "oppo", "com.netease.sky.nearme.gamecenter", {"oppo": "com.netease.sky.nearme.gamecenter"}, None),
]


def main():
    cloud = json.load(open(CLOUD_PATH))
    data = cloud["data"]

    # log_key 映射
    logkey = {}
    for item in data:
        gid = item.get("game_id")
        if gid and gid not in logkey:
            logkey[gid] = item.get("log_key")

    added = 0
    skipped = []
    for gid, ch, pkg, fields, name in NEW_ENTRIES:
        exists = any(i.get("game_id") == gid and i.get("app_channel") == ch for i in data)
        if exists:
            skipped.append("%s/%s" % (gid, ch))
            continue
        entry = {
            "package_name": pkg,
            "app_channel": ch,
            "log_key": logkey.get(gid, ""),
            "game_id": gid,
        }
        entry.update(fields)
        if name:
            entry["name"] = name
        data.append(entry)
        added += 1
        print("[+] %s/%s pkg=%s" % (gid, ch, pkg))

    if added:
        cloud["lastModified"] = int(__import__("time").time())
        json.dump(cloud, open(CLOUD_PATH, "w"), ensure_ascii=False, indent=2)
    print("新增 %d 条，跳过 %s，total=%d" % (added, ",".join(skipped) or "-", len(data)))


if __name__ == "__main__":
    main()
