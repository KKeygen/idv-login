import os
import sys
from logutil import logger
from envmgr import genv
import app_state
class ShortcutEntry:
    def __init__(self, url, name, policy="always") -> None:
        self.url = url
        self.name = name
        self.policy = policy  # 快捷方式创建策略: once-仅创建一次, always-每次都创建
class ShortcutMgr:
    def __init__(self) -> None:
        pass
    def _get_shortcut_dir(self):
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(self._detect_current_executable_path()[0])
        else:
            base_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.exists(base_dir):
            return base_dir
        return os.path.dirname(self._detect_current_executable_path()[0])
    def create_shortcut(self,url,name):
        try:
            content=f'''[InternetShortcut]
    URL={url}
    '''
            savePath=os.path.join(self._get_shortcut_dir(),f"{name}.url")
            with open(savePath,"w") as f:
                f.write(content)
            return True
        except Exception as e:
            logger.error(f"创建快捷方式失败: {e}")
            return False
    
    def _detect_current_executable_path(self):
        """检测当前可执行文件或脚本的路径"""
        try:
            if getattr(sys, 'frozen', False):
                # 如果是PyInstaller打包的执行文件
                exe_path = sys.executable
                logger.debug(f"检测到PyInstaller打包环境，可执行文件路径: {exe_path}")
                return exe_path, True  # 返回路径和是否为exe的标志
            else:
                # 如果是Python脚本
                script_path = os.path.abspath(sys.argv[0])
                logger.debug(f"检测到Python脚本环境，脚本路径: {script_path}")
                return script_path, False  # 返回路径和是否为exe的标志
        except Exception as e:
            logger.error(f"检测可执行文件路径时出错: {e}")
            return None, False

    def get_shortcuts(self):
        cloudResMgr_instance = app_state.cloud_res
        if cloudResMgr_instance is None:
            return []
        return cloudResMgr_instance.get_shortcuts()
    
    def handle_shortcuts(self):
        shortcuts=self.get_shortcuts()
        for shortcut in shortcuts:
            entry=ShortcutEntry(shortcut['url'],shortcut['name'],shortcut['policy'])
            if (not genv.get(f"shortcut_{entry.name}_{entry.url}",False)) or entry.policy=="always":
                if self.create_shortcut(entry.url,entry.name):
                    genv.set(f"shortcut_{entry.name}_{entry.url}",True,True)
    
if __name__=='__main__':
    ShortcutMgr().handle_shortcuts()
