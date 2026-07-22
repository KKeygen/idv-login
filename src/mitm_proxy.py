# coding=UTF-8
"""
Copyright (c) 2026 KKeygen

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

# TODO: 当 hotfixmgr 支持创建新模块后，将以下功能拆分为独立文件：
# - DNS 策略管理 (NRPT/Hosts) -> dns_policy.py
# - 本地 DNS 服务器 -> local_dns_server.py
# - DNS 回环防止机制 -> 保留在 mitm_proxy.py
# 参见：https://github.com/KKeygen/idv-login/issues/XXX
from __future__ import annotations

import asyncio
import os
import random
import socket
import ssl
import struct
import subprocess
import sys
import threading

from envmgr import genv
from logutil import setup_logger


logger = setup_logger()

# 防止 PowerShell 子进程修改当前控制台字体/代码页
_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Default proxy listen port
DEFAULT_PROXY_PORT = 10717

# Compat mode reverse proxy port (HTTPS 443)
COMPAT_HTTPS_PORT = 443


# ==================================================================
# DNS 回环防止机制
# 重写 socket.getaddrinfo，对目标域名返回预解析的真实 IP
# ==================================================================

_dns_cache: dict[tuple[str, int], list] = {}
_original_getaddrinfo = socket.getaddrinfo


def _is_ipv4(s: str) -> bool:
    return ":" not in s


def add_custom_dns(domain: str, port: int, ip: str):
    """添加自定义 DNS 解析结果，防止 DNS 回环。

    在兼容模式下，Hosts 文件或 NRPT 将目标域名指向 127.0.0.1。
    当 mitmproxy 需要将请求转发到真实服务器时，必须绕过系统 DNS，
    直接使用预解析的真实 IP。

    Args:
        domain: 域名
        port: 端口
        ip: 真实 IP 地址
    """
    key = (domain, port)
    if _is_ipv4(ip):
        value = (
            socket.AddressFamily.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, port),
        )
    else:
        value = (
            socket.AddressFamily.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, port, 0, 0),
        )
    _dns_cache[key] = [value]
    logger.debug(f"添加自定义 DNS: {domain}:{port} -> {ip}")


def remove_custom_dns(domain: str, port: int):
    """移除自定义 DNS 解析。"""
    key = (domain, port)
    _dns_cache.pop(key, None)


def clear_custom_dns():
    """清除所有自定义 DNS 解析。"""
    _dns_cache.clear()


def _patched_getaddrinfo(*args):
    """替换的 getaddrinfo 函数。"""
    try:
        key = args[:2]  # (hostname, port)
        if key in _dns_cache:
            return _dns_cache[key]
    except Exception:
        pass
    return _original_getaddrinfo(*args)


# 替换 socket.getaddrinfo
socket.getaddrinfo = _patched_getaddrinfo


# ==================================================================
# 本地 DNS 服务器 - 兼容模式专用
# TODO: 拆分到 local_dns_server.py
# ==================================================================

# 预配置的上游 DNS 服务器列表（避免 DNS 回环）
UPSTREAM_DNS_SERVERS = [
    "223.5.5.5",      # 阿里 DNS
    "119.29.29.29",   # 腾讯 DNSPod
    "114.114.114.114",  # 114 DNS
    "8.8.8.8",        # Google DNS
]

# 硬编码的目标服务器 IP（用于 DNS 回环时的备用方案）
HARDCODED_IPS = {
    "service.mkey.163.com": "42.186.193.21",
    "sdk-os.mpsdk.easebar.com": "8.222.80.103",
}


def probe_hardcoded_ip(domain: str, timeout: float = 3.0) -> bool:
    """探测硬编码 IP 是否可访问。

    Args:
        domain: 域名
        timeout: 超时时间（秒）

    Returns:
        是否可访问
    """
    ip = HARDCODED_IPS.get(domain)
    if not ip:
        return False

    try:
        # 尝试建立 HTTPS 连接
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                # 连接成功
                return True
    except Exception as e:
        logger.debug(f"探测 {domain} ({ip}) 失败: {e}")
        return False


def resolve_domain_ip(domain: str, use_hardcoded_first: bool = True) -> str | None:
    """解析域名的真实 IP。

    优先使用硬编码 IP（如果可访问），否则通过上游 DNS 解析。

    Args:
        domain: 域名
        use_hardcoded_first: 是否优先尝试硬编码 IP

    Returns:
        IP 地址，解析失败返回 None
    """
    # 1. 尝试硬编码 IP
    if use_hardcoded_first and domain in HARDCODED_IPS:
        hardcoded_ip = HARDCODED_IPS[domain]
        if probe_hardcoded_ip(domain):
            logger.debug(f"使用硬编码 IP: {domain} -> {hardcoded_ip}")
            return hardcoded_ip
        else:
            logger.warning(f"硬编码 IP {hardcoded_ip} 不可访问，尝试 DNS 解析")

    # 2. 并行查询上游 DNS 服务器（取最快响应）
    ip = _query_upstream_parallel(domain)
    if ip:
        return ip

    # 3. 回退到硬编码 IP（即使不可访问）
    if domain in HARDCODED_IPS:
        logger.warning(f"DNS 解析失败，回退到硬编码 IP: {domain} -> {HARDCODED_IPS[domain]}")
        return HARDCODED_IPS[domain]

    return None


def _build_dns_query(domain: str) -> bytes:
    """构造 DNS A 记录查询报文。"""
    # Header
    transaction_id = random.randint(0, 65535)
    flags = 0x0100  # Standard query, recursion desired
    header = struct.pack("!HHHHHH", transaction_id, flags, 1, 0, 0, 0)

    # Question
    question = b""
    for label in domain.split("."):
        question += struct.pack("!B", len(label)) + label.encode("ascii")
    question += b"\x00"  # Null terminator
    question += struct.pack("!HH", 1, 1)  # QTYPE=A, QCLASS=IN

    return header + question


def _parse_dns_response(response: bytes) -> str | None:
    """解析 DNS 响应，提取第一个 A 记录。"""
    try:
        # Skip header (12 bytes)
        pos = 12

        # Skip question section
        while response[pos] != 0:
            pos += response[pos] + 1
        pos += 5  # Null + QTYPE(2) + QCLASS(2)

        # Parse answer section
        an_count = struct.unpack("!H", response[6:8])[0]
        for _ in range(an_count):
            # Skip name (may be compressed)
            if response[pos] >= 192:
                pos += 2
            else:
                while response[pos] != 0:
                    pos += response[pos] + 1
                pos += 1

            rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", response[pos:pos + 10])
            pos += 10

            if rtype == 1 and rdlength == 4:  # A record
                ip = ".".join(str(b) for b in response[pos:pos + 4])
                return ip

            pos += rdlength

    except Exception:
        pass

    return None


def _query_upstream_parallel(domain: str) -> str | None:
    """并行查询多个上游 DNS 服务器，返回最快的成功结果。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    query = _build_dns_query(domain)

    def _query_one(dns_server: str) -> tuple[str, str | None]:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2.0)
            sock.sendto(query, (dns_server, 53))
            response, _ = sock.recvfrom(512)
            return dns_server, _parse_dns_response(response)

    with ThreadPoolExecutor(max_workers=len(UPSTREAM_DNS_SERVERS)) as executor:
        futures = [executor.submit(_query_one, srv) for srv in UPSTREAM_DNS_SERVERS]
        for future in as_completed(futures, timeout=3.0):
            try:
                srv, ip = future.result()
                if ip:
                    logger.debug(f"DNS 解析成功: {domain} -> {ip} (via {srv})")
                    return ip
            except Exception:
                continue

    return None


class DnsPacket:
    """简单的 DNS 报文解析器。"""

    def __init__(self, data: bytes):
        self.raw = data
        self.id = struct.unpack("!H", data[:2])[0]
        self.flags = struct.unpack("!H", data[2:4])[0]
        self.qd_count = struct.unpack("!H", data[4:6])[0]
        self.an_count = struct.unpack("!H", data[6:8])[0]
        self.ns_count = struct.unpack("!H", data[8:10])[0]
        self.ar_count = struct.unpack("!H", data[10:12])[0]

        # 解析查询域名
        self.qname = ""
        self.qtype = 0
        self.qclass = 0
        self._parse_question(data[12:])

    def _parse_question(self, data: bytes):
        """解析 DNS 问题部分。"""
        labels = []
        i = 0
        while i < len(data):
            length = data[i]
            if length == 0:
                i += 1
                break
            if length >= 192:  # 压缩指针，本实现不处理
                i += 2
                break
            labels.append(data[i + 1: i + 1 + length].decode("ascii", errors="ignore"))
            i += 1 + length

        self.qname = ".".join(labels).lower()
        if i + 4 <= len(data):
            self.qtype = struct.unpack("!H", data[i:i + 2])[0]
            self.qclass = struct.unpack("!H", data[i + 2:i + 4])[0]

    def build_response(self, ip_address: str) -> bytes:
        """构建包含单个 A 记录的响应报文。

        Args:
            ip_address: 响应的 IP 地址

        Returns:
            DNS 响应报文字节
        """
        # 响应头
        response_flags = 0x8180  # QR=1, Opcode=0, AA=1, TC=0, RD=1, RA=1, Z=0, RCODE=0
        header = struct.pack(
            "!HHHHHH",
            self.id,
            response_flags,
            1,  # QDCOUNT
            1,  # ANCOUNT
            0,  # NSCOUNT
            0,  # ARCOUNT
        )

        # 问题部分：直接复制原始问题
        question_end = 12
        i = 12
        while i < len(self.raw):
            if self.raw[i] == 0:
                question_end = i + 5  # 包含 null + QTYPE(2) + QCLASS(2)
                break
            i += 1 + self.raw[i]
        question = self.raw[12:question_end]

        # 回答部分
        # Name: 使用指针压缩 (0xC00C 指向偏移 12 处的域名)
        answer = struct.pack(
            "!HHHLH",
            0xC00C,  # 压缩指针
            1,       # TYPE: A
            1,       # CLASS: IN
            300,     # TTL: 300 秒
            4,       # RDLENGTH: 4 字节
        )

        # IP 地址
        ip_parts = [int(x) for x in ip_address.split(".")]
        answer += struct.pack("!BBBB", *ip_parts)

        return header + question + answer

    def build_empty_response(self) -> bytes:
        """构建无记录的空响应报文（用于阻止非A类查询绕过拦截）。

        Returns:
            DNS 空响应报文字节
        """
        response_flags = 0x8180
        header = struct.pack(
            "!HHHHHH",
            self.id,
            response_flags,
            1,  # QDCOUNT
            0,  # ANCOUNT - 无回答
            0,  # NSCOUNT
            0,  # ARCOUNT
        )

        question_end = 12
        i = 12
        while i < len(self.raw):
            if self.raw[i] == 0:
                question_end = i + 5
                break
            i += 1 + self.raw[i]
        question = self.raw[12:question_end]

        return header + question


class LocalDnsServer:
    """本地 DNS 服务器。

    对指定域名返回固定 IP，其他请求转发到上游 DNS 服务器。
    """

    def __init__(
        self,
        intercept_domains: set[str],
        target_ip: str = "127.0.0.1",
        listen_host: str = "127.0.0.1",
        listen_port: int = 53,
    ):
        """
        Args:
            intercept_domains: 要拦截的域名集合
            target_ip: 拦截域名返回的 IP 地址
            listen_host: 监听地址
            listen_port: 监听端口
        """
        self.intercept_domains = {d.lower() for d in intercept_domains}
        self.target_ip = target_ip
        self.listen_host = listen_host
        self.listen_port = listen_port

        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

    def start(self) -> bool:
        """启动 DNS 服务器。

        Returns:
            是否成功启动
        """
        if self._running:
            logger.warning("DNS 服务器已在运行")
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.listen_host, self.listen_port))
            self._socket.settimeout(1.0)  # 1 秒超时，用于检查停止事件
        except OSError as e:
            logger.error(f"DNS 服务器绑定端口失败: {e}")
            if self._socket:
                self._socket.close()
                self._socket = None
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._server_loop,
            name="LocalDnsServer",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        logger.debug(f"本地 DNS 服务器已启动: {self.listen_host}:{self.listen_port}")
        return True

    def stop(self):
        """停止 DNS 服务器。"""
        if not self._running:
            return

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        self._running = False
        logger.debug("本地 DNS 服务器已停止")

    def _server_loop(self):
        """服务器主循环。"""
        while not self._stop_event.is_set():
            try:
                data, addr = self._socket.recvfrom(512)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.debug(f"DNS 服务器接收异常: {e}")
                continue

            # 在独立线程中处理请求（避免阻塞主循环）
            threading.Thread(
                target=self._handle_request,
                args=(data, addr),
                daemon=True,
            ).start()

    def _handle_request(self, data: bytes, addr: tuple):
        """处理单个 DNS 请求。"""
        try:
            packet = DnsPacket(data)

            # 检查是否需要拦截
            should_intercept = False
            for domain in self.intercept_domains:
                if packet.qname == domain or packet.qname.endswith("." + domain):
                    should_intercept = True
                    break

            if should_intercept:
                if packet.qtype == 1:  # A 记录
                    response = packet.build_response(self.target_ip)
                else:
                    # 拦截域名的非A查询（AAAA等）：返回空响应，防止绕过
                    response = packet.build_empty_response()
            else:
                # 转发到上游 DNS
                response = self._forward_to_upstream(data)
                if response is None:
                    return

            self._socket.sendto(response, addr)

        except Exception as e:
            logger.debug(f"DNS 请求处理异常: {e}")

    def _forward_to_upstream(self, data: bytes) -> bytes | None:
        """将 DNS 请求转发到上游服务器。

        使用预配置的 IP 地址直接连接，避免 NRPT 回环。
        """
        for dns_server in UPSTREAM_DNS_SERVERS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(2.0)
                    sock.sendto(data, (dns_server, 53))
                    response, _ = sock.recvfrom(512)
                    return response
            except Exception:
                continue

        return None

    @property
    def is_running(self) -> bool:
        """服务器是否正在运行。"""
        return self._running


# ==================================================================
# DNS 策略管理 - 兼容模式专用 (NRPT/Hosts)
# TODO: 拆分到 dns_policy.py
# ==================================================================

# 用于标识本工具创建的 NRPT 规则的显示名称前缀
_NRPT_RULE_PREFIX = "IDVLogin_"


def is_nrpt_available() -> bool:
    """检测 Windows NRPT 命令是否可用。

    NRPT 功能需要 Windows 7+ 且具有管理员权限。
    """
    if sys.platform != "win32":
        return False

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Command Add-DnsClientNrptRule -ErrorAction SilentlyContinue"],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        return result.returncode == 0 and b"Add-DnsClientNrptRule" in result.stdout
    except Exception as e:
        logger.debug(f"检测 NRPT 可用性失败: {e}")
        return False


def add_nrpt_rule(domain: str, dns_server: str = "127.0.0.1") -> bool:
    """为指定域名添加 NRPT 规则，将 DNS 解析指向本地 DNS 服务器。

    Args:
        domain: 要劫持的域名，如 "service.mkey.163.com"
        dns_server: DNS 服务器地址，默认 "127.0.0.1"

    Returns:
        是否成功添加规则
    """
    if sys.platform != "win32":
        logger.warning("NRPT 仅支持 Windows 平台")
        return False

    rule_name = f"{_NRPT_RULE_PREFIX}{domain.replace('.', '_')}"

    # 先尝试删除同名规则（如果存在）
    remove_nrpt_rule(domain)

    # 添加 NRPT 规则
    # -Namespace: 匹配的域名后缀（以 . 开头表示后缀匹配，不带 . 表示精确匹配）
    # -NameServers: 指定该域名使用的 DNS 服务器
    # -DisplayName: 规则显示名称，用于标识
    ps_cmd = (
        f'Add-DnsClientNrptRule -Namespace ".{domain}" '
        f'-NameServers "{dns_server}" '
        f'-DisplayName "{rule_name}" '
        f'-ErrorAction Stop'
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            timeout=15,
            text=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            logger.debug(f"已添加 NRPT 规则: {domain} -> {dns_server}")
            return True
        else:
            logger.error(f"添加 NRPT 规则失败: {result.stderr}")
            return False
    except Exception as e:
        logger.error(f"添加 NRPT 规则异常: {e}")
        return False


def remove_nrpt_rule(domain: str) -> bool:
    """移除指定域名的 NRPT 规则。

    Args:
        domain: 域名

    Returns:
        是否成功移除（规则不存在也返回 True）
    """
    if sys.platform != "win32":
        return True

    rule_name = f"{_NRPT_RULE_PREFIX}{domain.replace('.', '_')}"

    # 查找并删除匹配的规则
    ps_cmd = (
        f'Get-DnsClientNrptRule | '
        f'Where-Object {{ $_.DisplayName -eq "{rule_name}" }} | '
        f'Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue'
    )

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        logger.debug(f"已移除 NRPT 规则: {domain}")
        return True
    except Exception as e:
        logger.warning(f"移除 NRPT 规则失败: {e}")
        return False


def remove_all_nrpt_rules() -> bool:
    """移除本工具创建的所有 NRPT 规则。"""
    if sys.platform != "win32":
        return True

    ps_cmd = (
        f'Get-DnsClientNrptRule | '
        f'Where-Object {{ $_.DisplayName -like "{_NRPT_RULE_PREFIX}*" }} | '
        f'Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue'
    )

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
        logger.debug("已移除所有 IDVLogin NRPT 规则")
        return True
    except Exception as e:
        logger.warning(f"移除 NRPT 规则失败: {e}")
        return False


def flush_dns_cache() -> bool:
    """刷新 DNS 缓存。"""
    if sys.platform != "win32":
        return True

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Clear-DnsClientCache"],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
        logger.debug("已刷新 DNS 缓存")
        return True
    except Exception as e:
        logger.warning(f"刷新 DNS 缓存失败: {e}")
        return False


def _setup_nrpt_batch(domains: list[str], target_ip: str) -> str:
    """用单次 PowerShell 调用完成清理旧规则、添加新规则、刷新缓存。

    跳过 Get-Command 检测，直接 try Add-DnsClientNrptRule，失败则回退。

    Returns:
        "OK" / "FAILED"
    """
    if sys.platform != "win32":
        return "FAILED"

    add_cmds = "\n    ".join(
        f'Add-DnsClientNrptRule -Namespace ".{d}" '
        f'-NameServers "{target_ip}" '
        f'-DisplayName "{_NRPT_RULE_PREFIX}{d.replace(".", "_")}" '
        f'-ErrorAction Stop'
        for d in domains
    )

    ps_script = (
        f'Get-DnsClientNrptRule | Where-Object {{ $_.DisplayName -like "{_NRPT_RULE_PREFIX}*" }}'
        f' | Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue\n'
        f'try {{\n    {add_cmds}\n}} catch {{\n'
        f'    Get-DnsClientNrptRule | Where-Object {{ $_.DisplayName -like "{_NRPT_RULE_PREFIX}*" }}'
        f' | Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue\n'
        f'    Write-Output "FAILED"; exit 0\n}}\n'
        f'Clear-DnsClientCache -ErrorAction SilentlyContinue\n'
        f'Write-Output "OK"'
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        output = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if output == "OK":
            return "OK"
        else:
            logger.warning(f"NRPT 设置失败: {result.stderr.strip()}")
            return "FAILED"
    except Exception as e:
        logger.error(f"NRPT 批量设置异常: {e}")
        return "FAILED"


def _cleanup_nrpt_batch():
    """单次 PowerShell 调用完成 NRPT 规则删除 + DNS 缓存刷新。"""
    if sys.platform != "win32":
        return

    ps_script = (
        f'Get-DnsClientNrptRule | Where-Object {{ $_.DisplayName -like "{_NRPT_RULE_PREFIX}*" }}'
        f' | Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue\n'
        f'Clear-DnsClientCache -ErrorAction SilentlyContinue'
    )

    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.warning(f"NRPT 批量清理失败: {e}")


class DnsPolicyManager:
    """DNS 策略管理器 - 统一管理 NRPT 和 Hosts 方式的 DNS 劫持。

    优先使用 NRPT，如果不可用则回退到 Hosts 文件方式。
    """

    def __init__(self, domains: list[str], target_ip: str = "127.0.0.1"):
        """
        Args:
            domains: 要劫持的域名列表
            target_ip: 劫持目标 IP，默认 127.0.0.1
        """
        self.domains = domains
        self.target_ip = target_ip
        self._use_nrpt = False
        self._active = False
        self._hostmgr = None

    def setup(self) -> bool:
        """设置 DNS 劫持策略（单次 PowerShell 调用完成 NRPT 检测+设置+刷新）。

        Returns:
            是否成功设置
        """
        if self._active:
            logger.warning("DNS 策略已激活，无需重复设置")
            return True

        # 单次 PowerShell 调用：旧规则清理、新规则添加、DNS 刷新
        nrpt_result = _setup_nrpt_batch(self.domains, self.target_ip)
        if nrpt_result == "OK":
            self._use_nrpt = True
            self._active = True
            logger.debug("DNS 策略已设置 (方式: NRPT)")
            return True

        logger.warning("NRPT 设置失败，尝试 Hosts 方式")

        # 回退到 Hosts 文件方式
        logger.debug("使用 Hosts 文件方式进行 DNS 劫持")
        try:
            from hostmgr import hostmgr
            self._hostmgr = hostmgr()
            for domain in self.domains:
                # 先移除可能存在的旧记录
                if self._hostmgr.isExist(domain):
                    self._hostmgr.remove(domain)
                self._hostmgr.add(domain, self.target_ip)
                logger.debug(f"已添加 Hosts 记录: {domain} -> {self.target_ip}")

            flush_dns_cache()
            self._use_nrpt = False
            self._active = True
            return True
        except Exception as e:
            logger.error(f"Hosts 文件方式设置失败: {e}")
            return False

    def cleanup(self):
        """清理 DNS 劫持策略，恢复原始状态。"""
        if not self._active:
            return

        if self._use_nrpt:
            # 单次 PowerShell 调用完成规则删除 + DNS 缓存刷新
            _cleanup_nrpt_batch()
            logger.debug("已清理 NRPT DNS 策略")
        else:
            # 清理 Hosts 记录
            if self._hostmgr is None:
                try:
                    from hostmgr import hostmgr
                    self._hostmgr = hostmgr()
                except Exception:
                    pass

            if self._hostmgr:
                for domain in self.domains:
                    try:
                        if self._hostmgr.isExist(domain):
                            self._hostmgr.remove(domain)
                            logger.debug(f"已移除 Hosts 记录: {domain}")
                    except Exception as e:
                        logger.warning(f"移除 Hosts 记录失败 ({domain}): {e}")
            flush_dns_cache()

        self._active = False

    @property
    def is_using_nrpt(self) -> bool:
        """是否正在使用 NRPT 方式。"""
        return self._use_nrpt

    @property
    def is_active(self) -> bool:
        """DNS 策略是否已激活。"""
        return self._active


class MitmProxyManager:
    """Manages mitmproxy running in normal (regular) proxy mode.

    Replaces the old ``proxymgr`` / ``macProxyMgr`` which listened on
    port 443 and required hosts-file manipulation.  The new approach
    sets ``HTTP_PROXY`` / ``HTTPS_PROXY`` environment variables for the
    game subprocess so that all game traffic flows through mitmproxy.

    Certificate generation and system trust-store installation is
    handled by ``generate_certificates_if_needed()`` in ``main.py``
    *before* this manager is started.  The mitmproxy confdir is
    pre-populated with ``mitmproxy-ca.pem`` (key + cert) and
    ``mitmproxy-ca-cert.pem`` (cert only).

    Modes:
    - "regular" (default): Standard HTTP proxy on configurable port
    - "compat": Reverse proxy on port 443 for DNS-based traffic interception
    """

    def __init__(self, *, addon, port=DEFAULT_PROXY_PORT, mode="regular"):
        """
        Args:
            addon: The mitmproxy addon to use
            port: Proxy listen port (used in regular mode)
            mode: "regular" for HTTP proxy, "compat" for reverse proxy on 443
        """
        self.addon = addon
        self.port = port
        self.mode = mode
        self._thread: threading.Thread | None = None
        self._master = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Port selection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_port_available(port: int) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def _resolve_port(self):
        """If the configured port is occupied, pick an available one."""
        # In compat mode, we must use port 443
        if self.mode == "compat":
            if not self._is_port_available(COMPAT_HTTPS_PORT):
                logger.error(f"兼容模式需要端口 {COMPAT_HTTPS_PORT}，但该端口已被占用")
                raise RuntimeError(f"Port {COMPAT_HTTPS_PORT} is not available for compat mode")
            self.port = COMPAT_HTTPS_PORT
            return

        if self._is_port_available(self.port):
            return
        original = self.port
        # Try a few ports near the original, then pick a random high port
        for candidate in range(original + 1, original + 20):
            if self._is_port_available(candidate):
                self.port = candidate
                logger.warning(f"端口 {original} 已被占用，改用端口 {self.port}")
                return
        # Fallback: let the OS pick a random available port
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        logger.warning(f"端口 {original} 及附近端口均被占用，改用随机端口 {self.port}")

    # ------------------------------------------------------------------
    # Certificate helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_confdir() -> str:
        """Return the mitmproxy config directory used for certs."""
        return os.path.join(genv.get("FP_WORKDIR", ""), "mitmproxy-conf")

    # ------------------------------------------------------------------
    # Proxy lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start mitmproxy in a daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("mitmproxy 已经在运行中")
            return

        self._resolve_port()

        self._thread = threading.Thread(
            target=self._run_proxy,
            name="mitmproxy-worker",
            daemon=True,
        )
        self._thread.start()

        mode_desc = "兼容模式 (反向代理)" if self.mode == "compat" else "常规代理模式"
        logger.info(f"mitmproxy {mode_desc}已启动，监听端口 {self.port}")

    def _run_proxy(self):
        """Thread target: create an asyncio event loop and run mitmproxy."""
        # Windows ProactorEventLoop 在多线程环境下 accept() 存在已知 bug
        # (WinError 10014 / WSAEFAULT)，改用更稳定的 SelectorEventLoop
        if sys.platform == "win32":
            self._loop = asyncio.SelectorEventLoop()
        else:
            self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._run_proxy_async())
        except Exception as e:
            logger.exception(f"mitmproxy 运行出错: {e}")
        finally:
            # 取消所有待处理的任务，避免 "task destroyed but pending" 警告
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                
                # 等待所有任务完成取消
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            
            self._loop.close()

    async def _run_proxy_async(self):
        """Async entry point – DumpMaster needs a *running* event loop."""
        import logging as _logging
        from mitmproxy.options import Options
        from mitmproxy.tools.dump import DumpMaster

        # 抑制 mitmproxy 自身的控制台日志输出
        _logging.getLogger("mitmproxy").setLevel(_logging.ERROR)

        confdir = self.get_confdir()

        if self.mode == "compat":
            # 兼容模式：作为反向代理监听 443 端口
            # 客户端（游戏）的 DNS 被劫持到 127.0.0.1，会直接连接 127.0.0.1:443
            # 使用 reverse 模式将请求转发到默认目标服务器
            # _CompatModeAddon 会根据 Host 头将请求路由到正确的服务器
            _patch_mitmproxy_reverse_https_tcp_only()
            default_target = genv.get("DOMAIN_TARGET", "service.mkey.163.com")
            opts = Options(
                listen_host="127.0.0.1",
                listen_port=self.port,
                confdir=confdir,
                ssl_insecure=False,
                http3=False,  # 禁用 QUIC/HTTP3，避免 UDP 443 端口占用冲突
                mode=[f"reverse:https://{default_target}/"],
            )
        else:
            # 常规代理模式
            opts = Options(
                listen_host="127.0.0.1",
                listen_port=self.port,
                confdir=confdir,
                ssl_insecure=False,  # DO verify upstream certs
            )

        self._master = DumpMaster(opts, with_dumper=False)
        self._master.addons.add(self.addon)
        if self.mode == "compat":
            # 兼容模式添加特殊的请求重写 addon
            self._master.addons.add(_CompatModeAddon())
        self._master.addons.add(_ResponseLogAddon())
        await self._master.run()

    def stop(self):
        """Shut down the mitmproxy proxy."""
        if self._master:
            try:
                self._master.shutdown()
            except Exception:
                pass
        
        # 等待代理线程优雅退出
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Game launching
    # ------------------------------------------------------------------

    def get_proxy_env(self) -> dict:
        """Return a copy of ``os.environ`` with proxy variables set.
        
        在兼容模式下，流量通过 DNS 劫持拦截，不需要代理环境变量。
        因此兼容模式返回不包含代理变量的环境副本。
        """
        env = os.environ.copy()
        
        # 兼容模式：不设置代理环境变量，流量通过 DNS 劫持处理
        if self.mode == "compat":
            return env
        
        # 常规模式 / 进程模式：设置代理环境变量
        proxy_url = f"http://127.0.0.1:{self.port}"
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        # 设置 NO_PROXY 以绕过特定域名
        no_proxy_value = self._get_no_proxy_value()
        if no_proxy_value:
            env["NO_PROXY"] = no_proxy_value
        if sys.platform != "win32":
            # Unix 环境变量区分大小写，需要同时设置小写
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
            if no_proxy_value:
                env["no_proxy"] = no_proxy_value
        return env

    def _get_no_proxy_value(self) -> str:
        """从 CloudRes 获取 NO_PROXY 域名列表并拼接为逗号分隔的字符串。"""
        try:
            from cloudRes import CloudRes
            cloudres = CloudRes()
            domains = cloudres.get_no_proxy_domains()
            return ",".join(domains) if domains else ""
        except Exception:
            return ""


def _patch_mitmproxy_reverse_https_tcp_only():
    """Force mitmproxy reverse:https listeners to use TCP only in compat mode."""
    from mitmproxy.proxy import mode_specs

    reverse_mode = mode_specs.ReverseMode
    if getattr(reverse_mode, "_idv_https_tcp_only_patched", False):
        return

    original_post_init = reverse_mode.__post_init__

    def _idv_reverse_post_init(self):
        original_post_init(self)
        if getattr(self, "scheme", None) == "https":
            self.transport_protocol = "tcp"

    reverse_mode.__post_init__ = _idv_reverse_post_init
    reverse_mode._idv_https_tcp_only_patched = True
    reverse_mode._idv_original_post_init = original_post_init
    logger.debug("已限制 mitmproxy reverse:https 仅监听 TCP，避免绑定 UDP 443")


class _CompatModeAddon:
    """兼容模式的请求重写 addon。

    在兼容模式下，客户端直接连接到 127.0.0.1:443，
    mitmproxy 使用反向代理模式接收请求。
    此 addon 负责根据 Host 头将请求路由到正确的上游服务器。
    """

    def request(self, flow):
        from mitmproxy import http

        # 获取原始 Host 头
        host = flow.request.host_header or flow.request.host
        if not host:
            return
        host = str(host).lower()

        # 获取目标域名配置
        from envmgr import genv
        target_domains = {
            str(domain).strip()
            for domain in (
                str(genv.get("DOMAIN_TARGET", "service.mkey.163.com")).lower(),
                str(genv.get("DOMAIN_TARGET_OVERSEA", "sdk-os.mpsdk.easebar.com")).lower(),
                str(genv.get("DOMAIN_TARGET_AUTH_STATUS", "")).lower(),
            )
            if str(domain or "").strip()
        }

        # 如果是目标域名，修改 flow 的目标地址
        if host in target_domains:
            # 重写请求的目标服务器为真实服务器
            flow.request.host = host
            flow.request.port = 443
            flow.request.scheme = "https"

    def responseheaders(self, flow):
        if flow.response and "alt-svc" in flow.response.headers:
            del flow.response.headers["alt-svc"]


class _ResponseLogAddon:
    """仅将非 200/404 的响应以 DEBUG 级别写入日志，不输出到控制台。"""

    def response(self, flow):
        pass
