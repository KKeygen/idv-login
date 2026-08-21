# coding=UTF-8
"""
 Copyright (c) 2025 KKeygen

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

import platform
import socket
import getpass
import os
import subprocess
from datetime import datetime
from channelHandler.channelUtils import _get_my_ip

try:
    import psutil
except ImportError:
    psutil = None

try:
    import winreg
except ImportError:
    winreg = None

class DebugMgr:
    """调试管理器 - 仅限Windows系统使用"""
    
    @staticmethod
    def is_windows():
        """检查是否为Windows系统"""
        return platform.system().lower() == 'windows'
    
    @staticmethod
    def get_process_list():
        """获取当前进程列表"""
        if not psutil:
            return ["psutil库未安装，无法获取进程列表"]
        
        processes = []
        try:
            for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_percent']):
                try:
                    process_info = {
                        'pid': proc.info['pid'],
                        'name': proc.info['name'],
                        'memory_mb': round(proc.info['memory_info'].rss / 1024 / 1024, 2) if proc.info['memory_info'] else 0,
                        'cpu_percent': proc.info['cpu_percent']
                    }
                    processes.append(process_info)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            processes.append(f"获取进程列表时出错: {str(e)}")
        
        return processes
    
    @staticmethod
    def get_installed_apps():
        """获取已安装应用列表（Windows注册表）"""
        if not winreg or not DebugMgr.is_windows():
            return ["winreg库不可用或非Windows系统，无法获取已安装应用"]
        
        apps = []
        try:
            # 检查64位应用
            uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, uninstall_key) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            try:
                                display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                try:
                                    display_version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                                except FileNotFoundError:
                                    display_version = "未知版本"
                                apps.append(f"{display_name} - {display_version}")
                            except FileNotFoundError:
                                continue
                    except Exception:
                        continue
            
            # 检查32位应用（在64位系统上）
            try:
                uninstall_key_32 = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, uninstall_key_32) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            subkey_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                try:
                                    display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                    try:
                                        display_version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                                    except FileNotFoundError:
                                        display_version = "未知版本"
                                    apps.append(f"{display_name} - {display_version} (32位)")
                                except FileNotFoundError:
                                    continue
                        except Exception:
                            continue
            except FileNotFoundError:
                pass  # 32位注册表项不存在（可能是32位系统）
                
        except Exception as e:
            apps.append(f"获取已安装应用时出错: {str(e)}")
        
        return apps
    
    @staticmethod
    def get_system_info():
        """获取系统信息"""
        info = {}
        try:
            # 基本系统信息
            info['操作系统'] = platform.system()
            info['系统版本'] = platform.version()
            info['系统发行版'] = platform.release()
            info['CPU架构'] = platform.machine()
            info['处理器'] = platform.processor()
            info['Python版本'] = platform.python_version()
            info['Python架构'] = platform.architecture()[0]
            
            # 用户和计算机信息
            info['用户名'] = getpass.getuser()
            info['计算机名'] = platform.node()
            
            # 网络信息
            try:
                hostname = socket.gethostname()
                info['主机名'] = hostname
                info['IP地址'] = _get_my_ip()
            except Exception as e:
                info['网络信息错误'] = str(e)
            
            # Windows特定信息
            if DebugMgr.is_windows():
                try:
                    # 获取制造商和型号信息
                    result = subprocess.run(['wmic', 'computersystem', 'get', 'manufacturer,model', '/format:csv'], 
                                          capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        lines = result.stdout.strip().split('\n')
                        for line in lines[1:]:  # 跳过标题行
                            if line.strip() and ',' in line:
                                parts = line.split(',')
                                if len(parts) >= 3:
                                    info['制造商'] = parts[1].strip()
                                    info['机器型号'] = parts[2].strip()
                                    break
                except Exception as e:
                    info['制造商信息错误'] = str(e)
                
                try:
                    # 获取BIOS信息
                    result = subprocess.run(['wmic', 'bios', 'get', 'version', '/format:csv'], 
                                          capture_output=True, text=True, timeout=10)
                    if result.returncode == 0:
                        lines = result.stdout.strip().split('\n')
                        for line in lines[1:]:
                            if line.strip() and ',' in line:
                                parts = line.split(',')
                                if len(parts) >= 2:
                                    info['BIOS版本'] = parts[1].strip()
                                    break
                except Exception as e:
                    info['BIOS信息错误'] = str(e)
            
            return info
        except Exception as e:
            info['系统信息获取错误'] = str(e)
            return info
    
    @staticmethod
    def get_proxy_info():
        """获取Windows代理信息，包括HTTPS代理和TUN模式代理"""
        if not DebugMgr.is_windows():
            return {"错误": "仅支持Windows系统"}
        
        proxy_info = {}
        
        try:
            # 1. 检查系统代理设置（注册表）
            if winreg:
                try:
                    # 打开Internet Settings注册表项
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 
                                       r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
                    
                    # 检查代理是否启用
                    try:
                        proxy_enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                        proxy_info['系统代理启用'] = bool(proxy_enable)
                    except FileNotFoundError:
                        proxy_info['系统代理启用'] = False
                    
                    # 获取代理服务器地址
                    try:
                        proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
                        proxy_info['代理服务器'] = proxy_server
                    except FileNotFoundError:
                        proxy_info['代理服务器'] = "未设置"
                    
                    # 获取代理覆盖（不使用代理的地址）
                    try:
                        proxy_override, _ = winreg.QueryValueEx(key, "ProxyOverride")
                        proxy_info['代理例外'] = proxy_override
                    except FileNotFoundError:
                        proxy_info['代理例外'] = "未设置"
                    
                    # 检查自动配置脚本
                    try:
                        auto_config_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
                        proxy_info['自动配置脚本'] = auto_config_url
                    except FileNotFoundError:
                        proxy_info['自动配置脚本'] = "未设置"
                    
                    winreg.CloseKey(key)
                    
                except Exception as e:
                    proxy_info['注册表读取错误'] = str(e)
            
            # 2. 检查环境变量代理
            env_proxies = {}
            for proxy_var in ['HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'ALL_PROXY', 'NO_PROXY']:
                value = os.environ.get(proxy_var) or os.environ.get(proxy_var.lower())
                if value:
                    env_proxies[proxy_var] = value
            
            if env_proxies:
                proxy_info['环境变量代理'] = env_proxies
            else:
                proxy_info['环境变量代理'] = "未设置"
            
            # 3. 检测TUN模式代理（通过网络适配器）
            tun_adapters = []
            if psutil:
                try:
                    # 获取网络接口信息
                    interfaces = psutil.net_if_addrs()
                    stats = psutil.net_if_stats()
                    
                    for interface_name, addresses in interfaces.items():
                        # 检查是否为TUN/TAP适配器
                        interface_lower = interface_name.lower()
                        if any(keyword in interface_lower for keyword in 
                               ['tun', 'tap', 'wintun', 'wireguard', 'openvpn', 'clash', 'v2ray', 'shadowsocks']):
                            
                            adapter_info = {
                                '名称': interface_name,
                                '状态': 'UP' if stats.get(interface_name, {}).isup else 'DOWN',
                                '地址': []
                            }
                            
                            for addr in addresses:
                                if addr.family == socket.AF_INET:  # IPv4
                                    adapter_info['地址'].append(f"IPv4: {addr.address}")
                                elif addr.family == socket.AF_INET6:  # IPv6
                                    adapter_info['地址'].append(f"IPv6: {addr.address}")
                            
                            tun_adapters.append(adapter_info)
                    
                    proxy_info['TUN模式适配器'] = tun_adapters if tun_adapters else "未检测到"
                    
                except Exception as e:
                    proxy_info['TUN适配器检测错误'] = str(e)
            
            # 4. 检查常见代理软件进程
            proxy_processes = []
            if psutil:
                try:
                    proxy_keywords = ['clash', 'v2ray', 'shadowsocks', 'proxifier', 'fiddler', 
                                    'charles', 'burp', 'mitmproxy', 'squid', 'privoxy']
                    
                    for proc in psutil.process_iter(['pid', 'name', 'exe']):
                        try:
                            proc_name = proc.info['name'].lower() if proc.info['name'] else ''
                            proc_exe = proc.info['exe'].lower() if proc.info['exe'] else ''
                            
                            for keyword in proxy_keywords:
                                if keyword in proc_name or keyword in proc_exe:
                                    proxy_processes.append({
                                        'PID': proc.info['pid'],
                                        '进程名': proc.info['name'],
                                        '路径': proc.info['exe'] or '未知'
                                    })
                                    break
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                    
                    proxy_info['代理软件进程'] = proxy_processes if proxy_processes else "未检测到"
                    
                except Exception as e:
                    proxy_info['进程检测错误'] = str(e)
            
            # 5. 检查网络连接（代理端口）
            proxy_connections = []
            if psutil:
                try:
                    # 常见代理端口
                    proxy_ports = [1080, 7890, 7891, 8080, 8081, 8888, 9090, 10809, 10810]
                    
                    connections = psutil.net_connections(kind='inet')
                    for conn in connections:
                        if conn.laddr and conn.laddr.port in proxy_ports and conn.status == 'LISTEN':
                            try:
                                proc = psutil.Process(conn.pid) if conn.pid else None
                                proxy_connections.append({
                                    '端口': conn.laddr.port,
                                    '地址': conn.laddr.ip,
                                    '进程': proc.name() if proc else '未知',
                                    'PID': conn.pid or '未知'
                                })
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                proxy_connections.append({
                                    '端口': conn.laddr.port,
                                    '地址': conn.laddr.ip,
                                    '进程': '未知',
                                    'PID': conn.pid or '未知'
                                })
                    
                    proxy_info['代理端口监听'] = proxy_connections if proxy_connections else "未检测到"
                    
                except Exception as e:
                    proxy_info['端口检测错误'] = str(e)
            
        except Exception as e:
            proxy_info['检测错误'] = str(e)
        
        return proxy_info
    
    @staticmethod
    def get_firewall_rules():
        """获取Windows防火墙规则"""
        if not DebugMgr.is_windows():
            return {'错误': '仅支持Windows系统'}
        
        firewall_info = {
            '入站规则': [],
            '出站规则': [],
            '防火墙状态': {},
            '错误信息': []
        }
        
        try:
            # 获取防火墙状态
            try:
                result = subprocess.run(
                    ['netsh', 'advfirewall', 'show', 'allprofiles', 'state'],
                    capture_output=True, timeout=30
                )
                if result.returncode == 0:
                    stdout_text = DebugMgr._decode_output(result.stdout)
                    firewall_info['防火墙状态']['原始输出'] = stdout_text
                else:
                    stderr_text = DebugMgr._decode_output(result.stderr)
                    firewall_info['错误信息'].append(f'获取防火墙状态失败: {stderr_text}')
            except Exception as e:
                firewall_info['错误信息'].append(f'获取防火墙状态异常: {str(e)}')
            
            # 获取入站规则
            try:
                result = subprocess.run(
                    ['netsh', 'advfirewall', 'firewall', 'show', 'rule', 'name=all', 'dir=in'],
                    capture_output=True, timeout=60
                )
                if result.returncode == 0:
                    # 尝试多种编码方式解码输出
                    stdout_text = DebugMgr._decode_output(result.stdout)
                    firewall_info['入站规则'] = DebugMgr._parse_firewall_rules(stdout_text)
                else:
                    stderr_text = DebugMgr._decode_output(result.stderr)
                    firewall_info['错误信息'].append(f'获取入站规则失败: {stderr_text}')
            except Exception as e:
                firewall_info['错误信息'].append(f'获取入站规则异常: {str(e)}')
            
            # 获取出站规则
            try:
                result = subprocess.run(
                    ['netsh', 'advfirewall', 'firewall', 'show', 'rule', 'name=all', 'dir=out'],
                    capture_output=True, timeout=60
                )
                if result.returncode == 0:
                    # 尝试多种编码方式解码输出
                    stdout_text = DebugMgr._decode_output(result.stdout)
                    firewall_info['出站规则'] = DebugMgr._parse_firewall_rules(stdout_text)
                else:
                    stderr_text = DebugMgr._decode_output(result.stderr)
                    firewall_info['错误信息'].append(f'获取出站规则失败: {stderr_text}')
            except Exception as e:
                firewall_info['错误信息'].append(f'获取出站规则异常: {str(e)}')
                
        except Exception as e:
            firewall_info['错误信息'].append(f'获取防火墙信息时发生错误: {str(e)}')
        
        return firewall_info
    
    @staticmethod
    def _decode_output(byte_output):
        """尝试多种编码方式解码输出"""
        if isinstance(byte_output, str):
            return byte_output
        
        # 尝试的编码列表，按优先级排序
        encodings = ['utf-8', 'gbk', 'gb2312', 'cp936', 'latin1']
        
        for encoding in encodings:
            try:
                return byte_output.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        
        # 如果所有编码都失败，使用 errors='replace' 强制解码
        try:
            return byte_output.decode('utf-8', errors='replace')
        except Exception:
            return str(byte_output)
    
    @staticmethod
    def _parse_firewall_rules(output):
        """解析防火墙规则输出"""
        rules = []
        current_rule = {}
        
        try:
            lines = output.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    if current_rule:
                        rules.append(current_rule.copy())
                        current_rule = {}
                    continue
                
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    current_rule[key] = value
            
            # 添加最后一个规则
            if current_rule:
                rules.append(current_rule)
                
        except Exception as e:
            rules.append({'解析错误': str(e)})
        
        return rules  # 返回所有规则
    
    
    @staticmethod
    def export_debug_info_json():
        if not DebugMgr.is_windows():
            print("警告: debugmgr仅支持Windows系统")
            return False
        

        try:
            debug_data = {
                'export_time': datetime.now().isoformat(),
                'system_info': DebugMgr.get_system_info(),
                'processes': DebugMgr.get_process_list(),
                'installed_apps': DebugMgr.get_installed_apps(),
                'proxy_info': DebugMgr.get_proxy_info(),
                'firewall_rules': DebugMgr.get_firewall_rules()
            }
            return debug_data
        except Exception as e:
            print(f"收集调试信息时出错: {str(e)}")
            return False



