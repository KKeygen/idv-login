jadx_path = r"jadx/bin/jadx"
import base64
import json
import os,subprocess,re
import base64
import logging

import requests

def validate(data, key):
    s_key = data["UNISDK_SERVER_KEY"]
    try:
        val=data[key]
        if key=='UNISDK_SERVER_KEY' or key=='APP_CHANNEL':
            return val
        decode = base64.b64decode(s_key)
        if len(decode) != 124:
            logging.error(f"f size error: {len(decode)}<>124")
            return val

        copy_of_range = decode[:62]
        copy_of_range2 = decode[62:124]
        hash_map = {}

        for i in range(62):
            hash_map[(copy_of_range[i] - 76) + copy_of_range2[i]] = copy_of_range2[i]

        char_array = list(val)
        for i in range(len(char_array)):
            b = ord(char_array[i])
            if b in hash_map:
                char_array[i] = chr(hash_map[b])

        return ''.join(char_array)
    except Exception as e:
        logging.exception("Exception occurred")
        return str
github_token=os.getenv("GITHUB_TOKEN")
def updateCloudRes(item):
    #get now cloud res
    url="https://api.github.com/repos/KKeygen/idv-login/contents/assets/cloudRes.json"
    headers={"Authorization":"token "+github_token}
    r=requests.get(url,headers=headers)
    fileInfo=r.json()
    sha=fileInfo["sha"]
    import base64
    import json
    content=base64.b64decode(fileInfo["content"]).decode()
    data=json.loads(content)
    #update cloud res


    import time
    data["lastModified"]=int(time.time())
    data["data"].append(item)

    commitMessage=f"Live update for {item['game_id']}-{item['app_channel']}"
    dataStr=json.dumps(data,indent=4)
    dataStr=base64.b64encode(dataStr.encode()).decode()
    data={
        "message":commitMessage,
        "content":dataStr,
        "sha":sha
    }
    r=requests.put(url,headers=headers,json=data)
    print(r.json())

def getNeteaseGameInfo(apkPath):
    app_channel = None
    application=None
    package_name = None
    log_key=None
    game_id=None

    os.makedirs('res', exist_ok=True)
    subprocess.check_call([jadx_path, apkPath, '--no-src', '-dr', 'res'])


    import xml.etree.ElementTree as ET

    with open('res/AndroidManifest.xml', 'r') as f:
        data = f.read()
        root = ET.fromstring(data)
        package_name = root.attrib['package']
        application = root.find('application')
        if "qihoo" in package_name or "qihoo360" in package_name or "360" in package_name:
            # 360(奇虎)。资源文件为 assets/360_assistant_data，
            # 而文件内的 APP_CHANNEL 为 allysdk.360_assistant，两者不同，见下方分支处理。
            app_channel = "360_assistant"
        elif "huawei" in package_name or "HUAWEI" in package_name:
            app_channel = "huawei"
        elif "xiaomi" in package_name or "XIAOMI" in package_name or "mi" in package_name or "MI" in package_name:
            app_channel = "xiaomi_app"
        elif "com.tencent" in package_name:
            app_channel = "myapp"
        elif "honor" in package_name or "hihonor" in package_name:
            app_channel = "honor_sdk"
        elif "aligames" in package_name:
            app_channel = "uc_platform"

    # 记录用于定位 {channel}_data 的渠道名。
    # 注意：文件内的 APP_CHANNEL 可能与文件名不同（360 为 allysdk.360_assistant）。
    data_file_channel = app_channel

    #get channel data
    with open(f'res/assets/{app_channel}_data', 'r') as f:
        myData = f.read()
        myData = base64.b64decode(myData).decode()
        myData = json.loads(myData)
        log_key=validate(myData, "JF_LOG_KEY")
        app_channel=validate(myData, "APP_CHANNEL")
        game_id=validate(myData, "JF_GAMEID")

    if app_channel=='xiaomi_app':
        namespaces = {'android': 'http://schemas.android.com/apk/res/android'}
        meta_data = application.find("./meta-data[@android:name='miGameAppId']", namespaces)
        miGameAppId = meta_data.attrib['{http://schemas.android.com/apk/res/android}value']
        channelData=miGameAppId
    if app_channel=='huawei':
        with open('res/assets/agconnect-services.json', 'r') as f:
            hw_data=json.loads(f.read())
            channelData=hw_data['client']
    if app_channel=='myapp':
        #open ysdkconf.ini
        with open('res/assets/ysdkconf.ini', 'r') as f:
            channelData={
                "wx_appid":"",
                "channel":""
            }
            data = f.read()
            data = data.split('\n')
            for line in data:
                line = line.strip()
                try:
                    if 'WX_APP_ID' in line:
                        channelData['wx_appid'] = line.split('=')[1]
                    elif 'OFFER_ID' in line:
                        channelData['channel'] = line.split('=')[1]
                    elif 'QQ_APP_ID' in line:
                        channelData['channel'] = line.split('=')[1]
                except:
                    pass
    if app_channel=='honor_sdk':
        channelData = {}
        namespaces = {'android': 'http://schemas.android.com/apk/res/android'}
        app_id_meta = application.find("./meta-data[@android:name='com.hihonor.iap.sdk.appid']", namespaces)
        cp_id_meta = application.find("./meta-data[@android:name='com.hihonor.iap.sdk.cpid']", namespaces)
        if app_id_meta is not None:
            channelData['app_id'] = app_id_meta.attrib['{http://schemas.android.com/apk/res/android}value']
        if cp_id_meta is not None:
            channelData['cp_id'] = cp_id_meta.attrib['{http://schemas.android.com/apk/res/android}value']
        # Extract sdk_ver from ReadMe_UniSDK.txt
        readme_path = 'res/ReadMe_UniSDK.txt'
        if os.path.exists(readme_path):
            with open(readme_path, 'r') as f:
                for line in f:
                    m = re.match(r'honor_sdk_\d+\s+([\d.]+)', line.strip())
                    if m:
                        channelData['sdk_ver'] = m.group(1)
                        break
    if data_file_channel=='360_assistant':
        # 360: APPID/APP_KEY/APP_SECRET 均为轮转加密后的值，必须用 validate() 解码。
        # APPID/APP_KEY 可与 AndroidManifest 的 QHOPENSDK_APPID/QHOPENSDK_APPKEY 交叉校验。
        # 注意此处的 app_channel 已被文件内的 APP_CHANNEL 覆盖为 allysdk.360_assistant，
        # 这里改回 360_assistant 以保持与 assets/360_assistant_data、UNIFIX_CHANNEL_ID 一致。
        app_channel = data_file_channel
        namespaces = {'android': 'http://schemas.android.com/apk/res/android'}

        def _meta(name):
            node = application.find("./meta-data[@android:name='%s']" % name, namespaces)
            if node is None:
                return ""
            return node.attrib.get('{http://schemas.android.com/apk/res/android}value', '')

        channelData = {
            "app_id": validate(myData, "APPID"),
            "app_key": validate(myData, "APP_KEY"),
            # 发给网易 uni_sauth 的协议 app_channel
            "protocol_app_channel": validate(myData, "APP_CHANNEL"),
        }
        # manifest 为权威值，若解密结果不一致则以 manifest 为准
        for key, meta_name in (
            ("app_id", "QHOPENSDK_APPID"),
            ("app_key", "QHOPENSDK_APPKEY"),
        ):
            value = _meta(meta_name)
            if value and channelData.get(key) != value:
                logging.warning(
                    "360 %s 解密值 %s 与 manifest %s=%s 不一致，采用 manifest 值"
                    % (key, channelData.get(key), meta_name, value)
                )
                channelData[key] = value
        # sdk_ver 取 assets/channel_infos_data -> main_channel.<channel>_N.version （如 V2.4.0_816）
        # 也可从 ReadMe_UniSDK.txt 的 "360_assistant_5  2.4.0_816" 行取得，二者一致。
        # ⚠ 下划线必须保留：这是发给网易 uni_sauth 的 sdk_version，
        #   NetEase 白名单按字面匹配，"2.4.0.816" 会 401 subcode 13。
        sdk_ver = ""
        channel_infos_path = 'res/assets/channel_infos_data'
        if os.path.exists(channel_infos_path):
            with open(channel_infos_path, 'r') as f:
                ci_data = json.loads(f.read())
            for value in (ci_data.get('main_channel') or {}).values():
                if isinstance(value, dict) and value.get('channel_id') == '360_assistant':
                    sdk_ver = str(value.get('version', '')).lstrip('V')
                    break
        if not sdk_ver:
            readme_path = 'res/ReadMe_UniSDK.txt'
            if os.path.exists(readme_path):
                with open(readme_path, 'r') as f:
                    for line in f:
                        m = re.match(r'360_assistant_\d+\s+(\S+)', line.strip())
                        if m:
                            sdk_ver = m.group(1)
                            break
        if sdk_ver:
            channelData['sdk_ver'] = sdk_ver
    if app_channel=='uc_platform':
        channelData = {}
        channelData['app_id'] = validate(myData, "APPID")
        channelData['uc_game_id'] = int(channelData['app_id']) if channelData['app_id'].isdigit() else channelData['app_id']
        channelData['package_name'] = package_name
        # 从 AndroidManifest 提取 versionCode 和 versionName
        namespaces = {'android': 'http://schemas.android.com/apk/res/android'}
        vc = root.attrib.get('{http://schemas.android.com/apk/res/android}versionCode', '')
        vn = root.attrib.get('{http://schemas.android.com/apk/res/android}versionName', '')
        if vc:
            channelData['version_code'] = int(vc)
        if vn:
            channelData['version_name'] = vn
        channel_infos_path = 'res/assets/channel_infos_data'
        if os.path.exists(channel_infos_path):
            with open(channel_infos_path, 'r') as f:
                ci_data = json.loads(f.read())
                if 'version' in ci_data:
                    channelData['sdk_ver'] = ci_data['version'].lstrip('V')
        sdk_info_path = 'res/assets/ucgamesdk/config/sdk_info.txt'
        if 'sdk_ver' not in channelData and os.path.exists(sdk_info_path):
            with open(sdk_info_path, 'r') as f:
                for line in f:
                    m = re.match(r'version=([\d.]+)', line.strip())
                    if m:
                        channelData['sdk_ver'] = m.group(1)
                        break

    RES={}
    RES["package_name"]=package_name
    RES["app_channel"]=app_channel
    RES["log_key"]=log_key
    RES["game_id"]=game_id
    RES[app_channel]=channelData
    if app_channel == "360_assistant":
        RES["channel"] = "360_assistant"
        RES["name"] = "360账号"
    print(RES)
    print(json.dumps(RES))
    if app_channel in ["xiaomi_app","huawei","myapp","honor_sdk","uc_platform","360_assistant"]:
        updateCloudRes(RES)
getNeteaseGameInfo("app.apk")