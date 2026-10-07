# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen & fwilliamhe

 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with this program. If not, see <https://www.gnu.org/licenses/>.
 """

import os
import json
import random
import time
import requests
from envmgr import genv
from logutil import setup_logger
from const import manual_login_channels
from channelHandler.channelUtils import cmp_game_id
from ssl_utils import should_verify_ssl


class channel:
    def __init__(
        self,
        login_info: dict,
        user_info: dict = {},
        ext_info: dict = {},
        device_info: dict = {},
        create_time: int = int(time.time()),
        last_login_time: int = 0,
        name: str = "",
        uuid: str = "",
    ) -> None:
        self.login_info = login_info
        self.user_info = user_info
        self.ext_info = ext_info
        self.device_info = device_info
        self.exchange_data = {
            "device": device_info,
            "ext_info": ext_info,
            "user": user_info,
        }

        self.create_time = create_time
        self.last_login_time = last_login_time
        self.uuid = f"{login_info['login_channel']}-{login_info['code']}" if uuid == "" else uuid
        self.channel_name = login_info["login_channel"]
        self.crossGames = True
        if name == "":
            self.name = self.uuid
        else:
            self.name = name

    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            login_info=data.get("login_info", {}),
            user_info=data.get("user_info", {}),
            ext_info=data.get("ext_info", {}),
            device_info=data.get("device_info", {}),
            create_time=data.get("create_time", int(time.time())),
            last_login_time=data.get("last_login_time", 0),
            name=data.get("name", ""),
            uuid=data.get("uuid", ""),
        )

    def get_uniSdk_data(self, game_id: str = ""):
        return {
            "user_id": self.user_info["id"],
            "token": self.user_info["token"],
            "login_channel": self.ext_info["src_app_channel2"],
            "udid": self.ext_info["src_udid"],
            "app_channel": self.ext_info["src_app_channel"],
            "sdk_version": self.ext_info["src_jf_game_id"],
            "jf_game_id": self.ext_info["src_jf_game_id"],
            "pay_channel": self.ext_info["src_pay_channel"],
            "extra_data": "",
            "extra_unisdk_data": self.ext_info["extra_unisdk_data"],
            "gv": "157",
            "gvn": "1.5.80",
            "cv": "a1.5.0",
        }

    def get_non_sensitive_data(self):
        return {
            "create_time": self.create_time,
            "last_login_time": self.last_login_time,
            "uuid": self.uuid,
            "name": self.name,
        }

    def before_save(self):
        pass
class ChannelManager:
    def __init__(self):
        self.logger = setup_logger()
        self.channels = []
        self._pending_login_channel = None  # 异步登录期间保持对 channel 的引用，防止 GC
        from channelHandler.miChannelHandler import miChannel
        from channelHandler.huaChannelHandler import huaweiChannel
        from channelHandler.vivoChannelHandler import vivoChannel
        from channelHandler.wechatChannelHandler import wechatChannel
        from channelHandler.oppoChannelHandler import oppoChannel

        if os.path.exists(genv.get("FP_CHANNEL_RECORD")):
            with open(genv.get("FP_CHANNEL_RECORD"), "r",encoding='utf-8') as file:
                try:
                    data = json.load(file)
                    for item in data:
                        if "login_info" in item.keys():
                            channel_name = item["login_info"]["login_channel"]
                            if channel_name == "xiaomi_app":

                                tmpChannel: miChannel = miChannel.from_dict(item)
                                # if tmpChannel.is_token_valid():
                                self.channels.append(tmpChannel)
                                # else:
                                #    self.logger.error(f"渠道服登录信息失效: {tmpChannel.name}")
                            elif channel_name == "huawei":
                                tmpChannel: huaweiChannel = huaweiChannel.from_dict(item)
                                # if tmpChannel.is_token_valid():
                                self.channels.append(tmpChannel)
                                # else:
                                #    self.logger.error(f"渠道服登录信息失效: {tmpChannel.name}")
                            elif channel_name =="nearme_vivo":
                                tmpChannel: vivoChannel = vivoChannel.from_dict(item)
                                self.channels.append(tmpChannel)
                            elif channel_name == "myapp" and item["uuid"].startswith("wx-"):
                                tmpChannel:wechatChannel=wechatChannel.from_dict(item)
                                self.channels.append(tmpChannel)
                            elif channel_name == "oppo" and item["uuid"].startswith("phone-"):
                                tmpChannel: oppoChannel = oppoChannel.from_dict(item)
                                self.channels.append(tmpChannel)
                            else:
                                self.channels.append(channel.from_dict(item))
                except:
                    self.logger.exception(f"读取渠道服登录信息失败。已经清空渠道服信息。")
                    from secure_write import write_json_restricted
                    write_json_restricted(genv.get("FP_CHANNEL_RECORD"), [])
        else:
            from secure_write import write_json_restricted
            write_json_restricted(genv.get("FP_CHANNEL_RECORD"), [])
            self.channels = []



    def save_records(self):
        for i in self.channels:
            i.before_save()
        oldData = [channel.__dict__.copy() for channel in self.channels]
        data = oldData.copy()
        for channel_data in data:
            to_be_deleted = []
            for key in channel_data.keys():
                mini_data = {"data": channel_data[key]}
                try:
                    json.dumps(mini_data)
                except:
                    to_be_deleted.append(key)
            for key in to_be_deleted:
                del channel_data[key]
        from secure_write import write_json_restricted
        write_json_restricted(genv.get("FP_CHANNEL_RECORD"), data)
        self.logger.info("渠道服登录信息已更新")
        callback = genv.get("CHANNELS_UPDATED_CALLBACK", None)
        if callable(callback):
            try:
                callback("channel_records_updated")
            except Exception:
                self.logger.exception("触发账号记录更新回调失败")

    def list_channels(self,game_id: str):
        return sorted(
            [channel.get_non_sensitive_data()  for channel in self.channels if game_id == "" or channel.crossGames or (cmp_game_id(channel.game_id, game_id))],
            key=lambda x: x["last_login_time"],
            reverse=True,
        )

    def import_from_scan(self, login_info: dict, exchange_info: dict):
        tmp_channel: channel = channel(
            login_info,
            exchange_info["user"],
            exchange_info["ext_info"] if "ext_info" in exchange_info.keys() else {},
            exchange_info["device"] if "device" in exchange_info.keys() else {},
        )
        import app_state
        native_save = genv.get("NATIVE_SAVE_ENABLED", False)
        login_channel = login_info["login_channel"]

        if native_save:
            toast_text = "扫码结果已临时保存，时长约3天，可在游戏的下拉框中选择账号登录。"
        else:
            toast_text = "扫码结果已保存在渠道服管理界面，有效期一个月及以上。"

        if login_channel in [i["channel"] for i in manual_login_channels] and login_channel != "myapp" and login_channel != "oppo":
            if native_save:
                self.logger.info("扫码结果已临时保存在游戏账号列表中，有效期约3天。如需长期保存请进入渠道服管理界面手动登录。")
                toast_text = "扫码结果已临时保存，时长约3天。如需长期保存请进入渠道服管理界面手动登录。"
            else:
                self.logger.warning("该渠道不支持扫码自动保存，请进入渠道服管理界面使用手动登录功能。")
                toast_text = "该渠道需要通过渠道服管理界面手动登录来长期保存。"
            app_state.toast(toast_text, duration=5000)
            return False
        if login_channel == "myapp":
            self.logger.warning("正在导入应用宝账号，请使用手动导入功能导入微信渠道服！如果您使用的是QQ渠道服，请忽略此信息。")
            toast_text += "\n正在导入应用宝账号，请使用手动导入功能导入微信渠道服！如果您使用的是QQ渠道服，请忽略此信息。"
        if login_channel == "oppo":
            self.logger.warning("您正在扫码导入OPPO账号，扫码登录有效期在一周到三个月不等，如需长期免扫码登录，请使用手动登录。具体方法请参见教程。")
            if native_save:
                toast_text = "已临时保存OPPO账号，有效期约3天。扫码登录有效期在一周到三个月不等，如需长期免扫码登录，请使用手动登录。"
            else:
                toast_text = "已保存OPPO账号在渠道服管理界面，扫码登录有效期在一周到三个月不等，如需长期免扫码登录，请使用手动登录。"
        app_state.toast(toast_text, duration=5000)
        #寻找是否有重复的self.user_info["id"]
        to_be_deleted = []
        try:
            account_name=tmp_channel.user_info["id"]
            for i_channel in self.channels:
                if "id" in i_channel.user_info and i_channel.user_info["id"] == account_name:
                    to_be_deleted.append(i_channel)
            #按self.last_login_time排序，取最近一次登录过的账号的名字和uuid给新账号
            if len(to_be_deleted) > 0:
                to_be_deleted = sorted(to_be_deleted, key=lambda x: x.last_login_time, reverse=True)
                tmp_channel.name = to_be_deleted[0].name
                tmp_channel.uuid = to_be_deleted[0].uuid
                tmp_channel.last_login_time = int(time.time())
                for i in to_be_deleted:
                    self.channels.remove(i)
                self.logger.warning(f"发现{login_info['login_channel']}账号{tmp_channel.name}({account_name})有{len(to_be_deleted)}条重复记录，已删除重复账号，并自动继承最近一次登录的账号名和uuid。")
        except:
            self.logger.exception("删除旧记录时发生错误")
        self.channels.append(tmp_channel)  
        self.save_records()

    def manual_import(self, channle_name: str, game_id: str, on_complete=None):
        tmpData = {
            "code": str(random.randint(100000, 999999)),
            "src_client_type": 1,
            "login_channel": channle_name,
            "src_client_country_code": "CN",
        }
        if channle_name == "xiaomi_app":
            from channelHandler.miChannelHandler import miChannel

            tmp_channel: miChannel = miChannel(tmpData,game_id=game_id)
        if channle_name == "huawei":
            from channelHandler.huaChannelHandler import huaweiChannel

            tmp_channel: huaweiChannel = huaweiChannel(tmpData,game_id=game_id)
        if channle_name == "nearme_vivo":
            from channelHandler.vivoChannelHandler import vivoChannel

            tmp_channel: vivoChannel = vivoChannel(tmpData,game_id=game_id)

        if channle_name == "myapp":
            from channelHandler.wechatChannelHandler import wechatChannel
            tmp_channel: wechatChannel = wechatChannel(tmpData,game_id=game_id)
            tmp_channel.uuid=f"wx-{tmp_channel.uuid}"

        if channle_name == "oppo":
            from channelHandler.oppoChannelHandler import oppoChannel
            tmp_channel: oppoChannel = oppoChannel(tmpData, game_id=game_id)
            tmp_channel.uuid=f"phone-{tmp_channel.uuid}"

        if on_complete is not None:
            # 保持对 tmp_channel 的引用，防止异步登录期间被 GC
            # 否则 tmp_channel 是局部变量，函数返回后会被销毁
            self._pending_login_channel = tmp_channel
            
            def _finish_import(success):
                self._pending_login_channel = None  # 登录完成，释放引用
                try:
                    if success and tmp_channel.is_token_valid():
                        tmp_channel.last_login_time = int(time.time())
                        self.channels.append(tmp_channel)
                        self.save_records()
                        on_complete(True)
                    else:
                        self.logger.error(f"手动导入失败: {tmp_channel.name}")
                        on_complete(False)
                except Exception:
                    self.logger.exception(f"异步手动导入失败: {tmp_channel.name}")
                    on_complete(False)

            try:
                if channle_name == "myapp":
                    # 微信登录不使用浏览器，在后台线程运行避免阻塞主线程
                    import threading
                    import app_state
                    def _run_sync():
                        try:
                            self.logger.info("微信登录：开始 request_user_login")
                            tmp_channel.request_user_login()
                            self.logger.info(f"微信登录：request_user_login 完成，session={tmp_channel.session is not None}")
                            success = tmp_channel.is_token_valid()
                            self.logger.info(f"微信登录：is_token_valid={success}")
                        except Exception:
                            self.logger.exception(f"微信异步登录失败")
                            success = False
                        # 必须在主线程调用 _finish_import，因为后续可能涉及 Qt 操作
                        self.logger.info(f"微信登录：准备调用 _finish_import(success={success})")
                        app_state.run_on_main_thread(lambda: _finish_import(success))
                    threading.Thread(target=_run_sync, daemon=True).start()
                else:
                    tmp_channel.request_user_login(on_complete=_finish_import)
            except Exception:
                self._pending_login_channel = None  # 异常时也要释放
                self.logger.exception(f"手动导入失败: {tmp_channel.name}")
                on_complete(False)
            return

        try:
            tmp_channel.request_user_login()
            if tmp_channel.is_token_valid():
                tmp_channel.last_login_time = int(time.time())
                self.channels.append(tmp_channel)
                self.save_records()
                return True
            else:
                self.logger.error(f"手动导入失败: {tmp_channel.name}")
                return False
        except:
            self.logger.exception(f"手动导入失败: {tmp_channel.name}")
            return False

    def login(self, uuid: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                data = channel.login()
                self.save_records()
                return data
        return False

    def rename(self, uuid: str, new_name: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                channel.name = new_name
                self.save_records()
                return True
        return False

    def delete(self, uuid: str):
        """删除渠道账号，如果是 weblogin 账号则同时删除 profile 和 cache 文件夹"""
        for i, channel in enumerate(self.channels):
            if channel.uuid == uuid:
                # 删除账号前，如果是 weblogin 账号，清理对应的 profile 和 cache 文件夹
                self._cleanup_weblogin_data(uuid)
                
                del self.channels[i]
                self.save_records()
                return True
        return False
    
    def _cleanup_weblogin_data(self, uuid: str):
        """清理 weblogin 账号的 profile 和 cache 文件夹
        
        weblogin 账号（使用 WebBrowser 的渠道）：
        - phone-xxx (OPPO)
        - xiaomi_app-xxx (小米)
        - huawei-xxx (华为)
        - nearme_vivo-xxx (vivo)
        
        不包括微信 (wx-xxx)，微信使用扫码登录，不产生 profile。
        """
        import shutil
        
        # 判断是否是 weblogin 账号（使用 WebBrowser 的渠道）
        is_weblogin = (
            uuid.startswith("phone-") or 
            uuid.startswith("xiaomi_app-") or 
            uuid.startswith("huawei-") or 
            uuid.startswith("nearme_vivo-")
        )
        
        if not is_weblogin:
            return
        
        # 删除 profile 文件夹
        profile_base = genv.get("GLOB_LOGIN_PROFILE_PATH")
        if profile_base:
            profile_path = os.path.join(profile_base, uuid)
            if os.path.exists(profile_path):
                try:
                    shutil.rmtree(profile_path)
                    self.logger.info(f"已删除 weblogin profile: {profile_path}")
                except Exception as e:
                    self.logger.warning(f"删除 profile 文件夹失败: {profile_path}, 错误: {e}")
        
        # 删除 cache 文件夹
        cache_base = genv.get("GLOB_LOGIN_CACHE_PATH")
        if cache_base:
            cache_path = os.path.join(cache_base, uuid)
            if os.path.exists(cache_path):
                try:
                    shutil.rmtree(cache_path)
                    self.logger.info(f"已删除 weblogin cache: {cache_path}")
                except Exception as e:
                    self.logger.warning(f"删除 cache 文件夹失败: {cache_path}, 错误: {e}")

    def cleanup_orphaned_weblogin_profiles(self):
        """v6.0.0 新增：清理孤立的 weblogin profile/cache 文件夹
        
        逻辑：
        1. 检查是否有 weblogin 账号，没有则跳过
        2. 遍历 profile/ 和 cache/ 文件夹，删除不在 channels 中的孤立文件夹
        
        由 main.py 在首次启动时调用。
        """
        # 检查是否有 weblogin 账号
        has_weblogin = any(
            ch.uuid.startswith("phone-") or 
            ch.uuid.startswith("xiaomi_app-") or 
            ch.uuid.startswith("huawei-") or 
            ch.uuid.startswith("nearme_vivo-")
            for ch in self.channels
        )
        
        if not has_weblogin:
            return
        
        # 收集所有合法的 uuid
        valid_uuids = {ch.uuid for ch in self.channels}
        
        import shutil
        
        # 清理孤立的 profile 文件夹
        profile_base = genv.get("GLOB_LOGIN_PROFILE_PATH")
        if profile_base and os.path.exists(profile_base):
            try:
                for folder_name in os.listdir(profile_base):
                    folder_path = os.path.join(profile_base, folder_name)
                    if not os.path.isdir(folder_path):
                        continue
                    
                    # 只清理 weblogin 类型的文件夹
                    is_weblogin_folder = (
                        folder_name.startswith("phone-") or 
                        folder_name.startswith("xiaomi_app-") or 
                        folder_name.startswith("huawei-") or 
                        folder_name.startswith("nearme_vivo-")
                    )
                    
                    if is_weblogin_folder and folder_name not in valid_uuids:
                        try:
                            shutil.rmtree(folder_path)
                            self.logger.info(f"清理孤立 profile: {folder_path}")
                        except Exception as e:
                            self.logger.warning(f"清理孤立 profile 失败: {folder_path}, 错误: {e}")
            except Exception as e:
                self.logger.warning(f"扫描 profile 文件夹失败: {e}")
        
        # 清理孤立的 cache 文件夹
        cache_base = genv.get("GLOB_LOGIN_CACHE_PATH")
        if cache_base and os.path.exists(cache_base):
            try:
                for folder_name in os.listdir(cache_base):
                    folder_path = os.path.join(cache_base, folder_name)
                    if not os.path.isdir(folder_path):
                        continue
                    
                    # 只清理 weblogin 类型的文件夹
                    is_weblogin_folder = (
                        folder_name.startswith("phone-") or 
                        folder_name.startswith("xiaomi_app-") or 
                        folder_name.startswith("huawei-") or 
                        folder_name.startswith("nearme_vivo-")
                    )
                    
                    if is_weblogin_folder and folder_name not in valid_uuids:
                        try:
                            shutil.rmtree(folder_path)
                            self.logger.info(f"清理孤立 cache: {folder_path}")
                        except Exception as e:
                            self.logger.warning(f"清理孤立 cache 失败: {folder_path}, 错误: {e}")
            except Exception as e:
                self.logger.warning(f"扫描 cache 文件夹失败: {e}")
        
        self.logger.info("孤立 weblogin profile/cache 清理完成")

    def build_query_res(self, uuid: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                data = channel.login_info
                return data
        return None

    def query_channel(self, uuid: str):
        for channel in self.channels:
            if channel.uuid == uuid:
                return channel
        return None

    def simulate_confirm(self, channel: channel, scanner_uuid: str, game_id: str, on_complete=None):
        def _do_confirm(channel_data):
            if not channel_data:
                genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                if on_complete:
                    on_complete(False)
                return False
            channel_data["uuid"] = scanner_uuid
            channel_data["game_id"] = game_id
            body = "&".join([f"{k}={v}" for k, v in channel_data.items()])
            r = requests.post(
                "https://service.mkey.163.com/mpay/api/qrcode/confirm_login",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                verify=should_verify_ssl()
            )
            self.logger.info(f"模拟确认请求返回: {r.json()}")
            if r.status_code == 200:
                channel.last_login_time = int(time.time())
                self.save_records()
                result = r.json()
                if on_complete:
                    on_complete(result)
                return result
            else:
                genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                if on_complete:
                    on_complete(False)
                return False

        # 检查渠道是否支持异步 get_uniSdk_data
        if on_complete is not None and hasattr(channel, 'get_uniSdk_data'):
            import inspect
            sig = inspect.signature(channel.get_uniSdk_data)
            if 'on_complete' in sig.parameters:
                channel.get_uniSdk_data(game_id, on_complete=_do_confirm)
                return None

        # 同步模式
        channel_data = channel.get_uniSdk_data(game_id)
        return _do_confirm(channel_data)

    def simulate_scan(self, uuid: str, scanner_uuid: str, game_id: str, on_complete=None):
        for channel in self.channels:
            if channel.uuid == uuid:
                data = {
                    "uuid": scanner_uuid,
                    "login_channel": channel.channel_name,
                    "app_channel": channel.channel_name,
                    "pay_channel": channel.channel_name,
                    "game_id": game_id,
                    "gv": "157",
                    "gvn": "1.5.80",
                    "cv": "a1.5.0",
                }
                try:
                    if scanner_uuid=="Kinich":
                        # 支持异步模式
                        if on_complete is not None and hasattr(channel, 'get_uniSdk_data'):
                            import inspect
                            sig = inspect.signature(channel.get_uniSdk_data)
                            if 'on_complete' in sig.parameters:
                                channel.get_uniSdk_data(on_complete=on_complete)
                                return None
                        return channel.get_uniSdk_data()
                    r = requests.get(
                        "https://service.mkey.163.com/mpay/api/qrcode/scan",
                        params=data,
                        verify=should_verify_ssl()
                    )
                    
                    resp=r.json()
                    if resp.get("code",-1)==1424:
                        data["game_id"]=resp["game"]["id"]
                        self.logger.info(f"发烧平台游戏id:{data['game_id']}")
                        r=requests.get(
                            "https://service.mkey.163.com/mpay/api/qrcode/scan",
                            params=data,
                            verify=should_verify_ssl()
                        )
                    if r.status_code == 200:
                        return self.simulate_confirm(channel, scanner_uuid, data["game_id"], on_complete=on_complete)
                    else:
                        genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                        if on_complete:
                            on_complete(False)
                        return False
                except:
                    self.logger.exception("模拟扫码请求失败")
                    genv.set("CHANNEL_ACCOUNT_SELECTED", "")
                    if on_complete:
                        on_complete(False)
                    return False
        if on_complete:
            on_complete(None)
        return None