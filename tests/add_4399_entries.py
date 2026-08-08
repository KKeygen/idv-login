# 给 8 个游戏补 4399com 条目（indent=2 + ensure_ascii=False 匹配原格式）
import json

path = '/root/idv-login/assets/cloudRes.json'
c = json.load(open(path))
data = c['data']

CHANNEL_PKGS = {
    'g37': 'com.netease.onmyoji.m4399',
    'g78': 'com.netease.moba.m4399',
    'g10': 'com.netease.stzb.m4399',
    'g66': 'com.netease.mrzh.m4399',
    'g112': 'com.netease.dhxy.m4399',
    'ma75': 'com.netease.sky.m4399',
    'h75': 'com.netease.yhtj.m4399',
    'h65': 'com.netease.aceracer.m4399',
}

logkey = {}
for item in data:
    gid = item.get('game_id')
    if gid and gid not in logkey:
        logkey[gid] = item.get('log_key')

added = 0
for gid, pkg in CHANNEL_PKGS.items():
    exists = any(item.get('game_id') == gid and item.get('app_channel') == '4399com' for item in data)
    if exists:
        print('[%s] 已存在，跳过' % gid)
        continue
    entry = {
        "package_name": pkg,
        "app_channel": "4399com",
        "log_key": logkey.get(gid, ""),
        "game_id": gid,
        "4399com": {
            "app_key": "114816",
            "channel_id": "4399com",
            "sdk_ver": "3.16.0",
        },
        "name": "4399账号",
    }
    data.append(entry)
    added += 1
    print('[%s] + 4399com %s' % (gid, pkg))

# 更新 lastModified
c['lastModified'] = int(__import__('time').time())
json.dump(c, open(path, 'w'), ensure_ascii=False, indent=2)
print('共新增 %d 条，total = %d' % (added, len(data)))
