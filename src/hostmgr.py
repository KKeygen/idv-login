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

from python_hosts import Hosts, HostsEntry
from logutil import setup_logger, user_notice, safe_exception

import os
import sys
import subprocess
import stat

FN_HOSTS = Hosts.determine_hosts_path()


def _fix_hosts_permission():
    """
    修复 hosts 文件权限，使其可被管理员读写。
    流程：读取内容 -> 删除文件 -> 重建文件 -> 设置管理员可读写权限
    """
    logger = setup_logger()
    
    # 1. 读取当前 hosts 内容
    content = b""
    try:
        with open(FN_HOSTS, "rb") as f:
            content = f.read()
        logger.info(f"已读取 hosts 文件内容，大小: {len(content)} 字节")
    except Exception as e:
        logger.warning(f"读取 hosts 文件失败: {e}，将创建空文件")
    
    # 2. 删除当前 hosts 文件
    try:
        # 先尝试移除只读属性（跨平台）
        if sys.platform == "win32":
            # Windows: 使用 attrib 移除只读属性
            subprocess.run(
                ["attrib", "-R", "-S", "-H", FN_HOSTS],
                capture_output=True, timeout=10
            )
        else:
            # Unix: 尝试添加写权限
            current_mode = os.stat(FN_HOSTS).st_mode
            os.chmod(FN_HOSTS, current_mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        
        os.remove(FN_HOSTS)
        logger.info(f"已删除原 hosts 文件")
    except Exception as e:
        logger.error(f"删除 hosts 文件失败: {e}")
        raise
    
    # 3. 创建新 hosts 文件并写入内容
    try:
        with open(FN_HOSTS, "wb") as f:
            f.write(content)
        logger.info(f"已重新创建 hosts 文件")
    except Exception as e:
        logger.error(f"创建 hosts 文件失败: {e}")
        raise
    
    # 4. 设置管理员可读写权限
    try:
        if sys.platform == "win32":
            # Windows: 重置 ACL，授权 Administrators 和 SYSTEM 完全控制，普通用户可读
            # /reset 清除所有显式 ACE 并恢复为继承权限
            subprocess.run(
                ["icacls", FN_HOSTS, "/reset"],
                capture_output=True, timeout=10
            )
            # /inheritance:r 移除所有继承的 ACE
            # S-1-5-32-544 = BUILTIN\Administrators (完全控制)
            # S-1-5-18     = NT AUTHORITY\SYSTEM (完全控制)
            # S-1-5-32-545 = BUILTIN\Users (读取)
            subprocess.run(
                ["icacls", FN_HOSTS, "/inheritance:r",
                 "/grant:r", "*S-1-5-32-544:(F)",
                 "/grant:r", "*S-1-5-18:(F)",
                 "/grant:r", "*S-1-5-32-545:(R)"],
                capture_output=True, timeout=10
            )
            logger.info("已设置 Windows hosts 文件权限 (Administrators/SYSTEM 完全控制, Users 只读)")
        else:
            # Unix/macOS: 设置 root 可读写，其他用户只读
            # hosts 文件通常属于 root:root (Linux) 或 root:wheel (macOS)
            os.chmod(FN_HOSTS, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o644
            logger.info("已设置 Unix hosts 文件权限 (644)")
    except Exception as e:
        logger.warning(f"设置 hosts 文件权限失败: {e}，但文件已重建")


class hostmgr:
    def __init__(self) -> None:
        self.logger=setup_logger()
        if (os.path.isfile(FN_HOSTS) == False):
            self.logger.warning(f"Hosts文件不存在，尝试创建中...")
            try:
                open(FN_HOSTS, 'w').close()
            except:
                safe_exception(self.logger, "Hosts文件创建失败")
                raise Exception()
        elif not os.access(FN_HOSTS, os.W_OK):
            self.logger.warning(f"Hosts文件不可写，正在尝试修复权限...")
            try:
                _fix_hosts_permission()
                self.logger.info(f"Hosts文件权限修复成功，继续初始化...")
                # 修复后重新检查
                if not os.access(FN_HOSTS, os.W_OK):
                    self.logger.error("Hosts文件权限修复后仍不可写")
                    user_notice("Hosts文件权限修复后仍不可写，请以管理员身份运行！", level='ERROR')
                    raise Exception()
            except Exception as e:
                self.logger.error(f"修复Hosts文件权限失败: {e}")
                user_notice(f"请以管理员身份运行，或手动检查{FN_HOSTS}的权限设置！", level='ERROR')
                raise Exception()
        
        # 验证 hosts 文件可正常读取
        try:
            m_host = Hosts()
            hostsOkay = m_host.exists(['localhost'])
            self.logger.debug(hostsOkay)
        except:
            self.logger.warning(f"Hosts文件编码异常，正在尝试修复...")
            try:
                _fix_hosts_permission()
                # 修复后再次尝试读取
                m_host = Hosts()
                hostsOkay = m_host.exists(['localhost'])
                self.logger.debug(hostsOkay)
                self.logger.info("Hosts文件修复成功")
            except:
                self.logger.error("Hosts文件修复失败")
                user_notice(f"Hosts文件修复失败，请手动检查{FN_HOSTS}", level='ERROR')
                input("按任意键退出。")
                sys.exit(1)

    def add(self, dnsname, ip) :
        m_host = Hosts()
        m_host.add([HostsEntry(entry_type="ipv4", address=ip, names=[dnsname])])
        try:
            m_host.write()
        except:
            self.logger.error("写Hosts文件失败")
            user_notice("写Hosts文件失败，请参考常见问题解决方案。", level='ERROR')
            raise Exception()
    def remove(self, dnsname) :
        m_host = Hosts()
        m_host.remove_all_matching(name=dnsname)
        m_host.write()
    
    def isExist(self, dnsname)->bool :
        m_host = Hosts()
        return m_host.exists(names=[dnsname])
