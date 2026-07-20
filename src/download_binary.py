import os
import requests
# -*- coding: utf-8 -*-
import zipfile
import zmq
import time
import json
import sys
import argparse
import socket
import shutil

# --- UI Server 配置 ---
# 防止防火墙问题
BIND_HOST = "127.0.0.1"

# 端口定义
# 1740: UI 在这里 LISTEN (SUB)，接收 Worker 发来的进度
PORT_RECEIVE_PROGRESS = 1740
# 1737: UI 在这里 LISTEN (PUB)，向 Worker 发送心跳/指令
PORT_SEND_HEARTBEAT = 1737

# 协议常量
TOPIC = b"434"
# UI 发送的心跳内容，告诉 Worker "我准备好接收状态 4 的数据了"
UI_HEARTBEAT_PAYLOAD = b"4"
# UI 发送心跳的频率
UI_HEARTBEAT_INTERVAL_S = 1.0

# downloadIPC message types (confirmed from the bundled binary).
MSG_PROGRESS = b"4"
MSG_SETUP_ERROR = b"101"
MSG_DISK_FULL = b"206"
MSG_QUIT = b"-1"

# DownloadStateResp.StateFlags values exposed by the bundled Go binary.
STATE_NOT_STARTED = 1
STATE_RESUME = 2
STATE_DOWNLOADING_HEAD = 3
STATE_DOWNLOADING = 4
STATE_BUILDING = 5
STATE_PAUSED = 6
STATE_DISK_FULL = 7
STATE_FINISHED = 8
STATE_FAILED = 9
STATE_VERIFYING = 11


def allocate_ipc_ports():
    """Allocate a distinct local port pair for one download process."""
    sockets = []
    try:
        ports = []
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((BIND_HOST, 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        return ports[0], ports[1]
    finally:
        for sock in sockets:
            sock.close()


def _format_size(size_bytes):
    try:
        size = float(size_bytes)
    except (TypeError, ValueError):
        return "N/A"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.2f} {units[idx]}"


def _as_percent(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def _bar(percent, width=28):
    value = _as_percent(percent)
    filled = int(width * value / 100.0)
    return f"[{('#' * filled).ljust(width, '-')}] {value:6.2f}%"


def _progress_view(data):
    """Return the phase and metrics represented by DownloadStateResp."""
    state = data.get("StateFlags", STATE_NOT_STARTED)
    if state == STATE_DOWNLOADING_HEAD:
        return (
            "下载索引",
            data.get("ShowDownloadHeadPercent", 0),
            data.get("ShowDownloadHeadRateStr", "N/A"),
            data.get("ShowDownloadHeadSize", 0),
        )
    if state == STATE_DOWNLOADING:
        return (
            "下载文件",
            data.get("ShowDownloadPercent", 0),
            data.get("ShowDownloadRateStr", "N/A"),
            data.get("ShowDownloadSize", 0),
        )
    if state == STATE_BUILDING:
        return (
            "写入文件",
            data.get("ShowBuildPercent", 0),
            data.get("ShowBuildRateStr", "N/A"),
            data.get("ShowBuildSize", 0),
        )
    if state == STATE_VERIFYING:
        return "校验文件", data.get("ShowVerifyPercent", 0), "N/A", 0
    if state == STATE_FINISHED:
        return "下载完成", 100, "N/A", 0
    labels = {
        STATE_NOT_STARTED: "准备下载",
        STATE_RESUME: "恢复下载",
        STATE_PAUSED: "下载已暂停",
        STATE_DISK_FULL: "磁盘空间不足",
        STATE_FAILED: "下载失败",
    }
    return labels.get(state, data.get("ShowTextKey") or "处理中"), 0, "N/A", 0


def _render_progress_line(data, columns=None):
    phase, percent, rate, total = _progress_view(data)
    value = _as_percent(percent)
    if columns is None:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    if columns < 72:
        rate_text = f" {rate}" if rate and rate != "N/A" else ""
        return f"[{phase}] {value:6.2f}%{rate_text}"
    details = ""
    if total:
        done = value * float(total) / 100.0
        details = f"  {_format_size(done)} / {_format_size(total)}"
    rate_text = f"  速率 {rate}" if rate and rate != "N/A" else ""
    reserved = 32 + len(rate_text) + len(details)
    bar_width = max(8, min(28, columns - reserved))
    return f"[{phase}] {_bar(value, bar_width)}{rate_text}{details}"


class ProgressReporter:
    """Emit bounded progress snapshots without terminal cursor manipulation."""

    def __init__(self, step_percent=1.0):
        self.step_percent = float(step_percent)
        self.last_phase = None
        self.last_percent = -1.0

    def render_if_due(self, data, columns=None):
        phase, percent, _rate, _total = _progress_view(data)
        value = _as_percent(percent)
        due = (
            phase != self.last_phase
            or value >= self.last_percent + self.step_percent
        )
        if not due:
            return ""
        self.last_phase = phase
        self.last_percent = value
        return _render_progress_line(data, columns=columns)


def _render_core_error(msg_type, payload):
    text = payload.decode("utf-8", errors="replace").strip()
    if msg_type == MSG_DISK_FULL:
        try:
            need_size = json.loads(text).get("NeedSpaceSize")
        except (json.JSONDecodeError, AttributeError):
            need_size = None
        if need_size is not None:
            return f"磁盘空间不足，至少需要可用空间：{_format_size(need_size)}"
        return f"磁盘空间不足：{text}" if text else "磁盘空间不足"
    if msg_type == MSG_SETUP_ERROR:
        return f"下载初始化失败：{text}" if text else "下载初始化失败，请检查安装路径"
    if msg_type == MSG_QUIT:
        return "下载核心已退出"
    return ""

def _progress_event(data):
    phase, percent, rate, total = _progress_view(data)
    return {
        # The core can announce STATE_FINISHED before the supervisor has
        # persisted installation metadata.  Only the supervisor publishes the
        # terminal task state.
        "status": "pending",
        "phase": phase,
        "progress_percent": _as_percent(percent),
        "rate": rate,
        "total_bytes": total,
        "state": data.get("StateFlags", STATE_NOT_STARTED),
    }


def main_ui_server(
    topic=None,
    sub_port=None,
    pub_port=None,
    stop_event=None,
    on_event=None,
):
    if topic is None:
        topic = TOPIC
    if sub_port is None:
        sub_port = PORT_SEND_HEARTBEAT
    if pub_port is None:
        pub_port = PORT_RECEIVE_PROGRESS

    
    context = zmq.Context()

    # 1. 创建 SUB 套接字 (用于接收 Worker 的进度)
    # 注意：在 ZMQ 中，Server 也可以是 SUB，只要它 Bind 即可。
    receiver = context.socket(zmq.SUB)
    
    # 启用 TCP Keepalive (可选，但在 Server 端是好习惯)
    receiver.setsockopt(zmq.TCP_KEEPALIVE, 1)
    
    # 关键：订阅主题
    receiver.setsockopt(zmq.SUBSCRIBE, topic)
    
    # 绑定端口 1740
    addr_recv = f"tcp://{BIND_HOST}:{pub_port}"
    try:
        receiver.bind(addr_recv)
    except zmq.ZMQError as e:
        print(f"[!] 无法绑定端口 {pub_port}: {e}")
        return

    # 2. 创建 PUB 套接字 (用于向 Worker 发送心跳)
    sender = context.socket(zmq.PUB)
    
    # 绑定端口 1737
    addr_send = f"tcp://{BIND_HOST}:{sub_port}"
    try:
        sender.bind(addr_send)
    except zmq.ZMQError as e:
        print(f"[!] 无法绑定端口 {sub_port}: {e}")
        return

    print("\nUI Server 已就绪。等待 下载核心 启动并连接...")
    
    last_heartbeat_time = 0
    progress_reporter = ProgressReporter()
    
    # 使用 Poller 实现高效的 I/O 多路复用
    poller = zmq.Poller()
    poller.register(receiver, zmq.POLLIN)

    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            # 1. 处理接收 (非阻塞)
            # poll 等待时间设为 10ms，保证循环能及时处理发送逻辑
            socks = dict(poller.poll(timeout=10))
            
            if receiver in socks:
                try:
                    message_parts = receiver.recv_multipart()
                    
                    if len(message_parts) == 3:
                        topic, msg_type, payload = message_parts
                        # 仅打印非心跳消息，或者特定的进度消息
                        if msg_type == MSG_PROGRESS:
                            try:
                                data = json.loads(payload.decode('utf-8'))
                                if on_event:
                                    on_event(_progress_event(data))
                                line = progress_reporter.render_if_due(data)
                                if line:
                                    print(line, flush=True)
                            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                                print(f"<-- [数据] 类型: {msg_type}, 长度: {len(payload)}")
                        else:
                            error_text = _render_core_error(msg_type, payload)
                            if error_text:
                                if on_event:
                                    on_event({
                                        "status": "done",
                                        "success": False,
                                        "phase": "failed",
                                        "error": error_text,
                                        "message_type": msg_type.decode(
                                            "utf-8", errors="replace"
                                        ),
                                    })
                                print(f"[下载核心] {error_text}")

                except zmq.ZMQError as e:
                    print(f"接收错误: {e}")

            # 2. 处理发送 (UI 心跳)
            # Worker 需要不断收到这个消息，才会认为 UI 在线，并继续发送数据
            current_time = time.time()
            if current_time - last_heartbeat_time > UI_HEARTBEAT_INTERVAL_S:
                # 构造消息: [topic, payload] -> [b"434", b"4"]
                # 注意：Worker (Client) 那边接收的是 2-part message
                sender.send_multipart([topic, UI_HEARTBEAT_PAYLOAD])
                # print(f"--> [UI心跳] 发送 '{UI_HEARTBEAT_PAYLOAD.decode()}' 到端口 1737")
                last_heartbeat_time = current_time

    except KeyboardInterrupt:
        print("\nUI Server 正在停止...")
    except Exception as e:
        import traceback
        print(f"\nUI Server 发生异常: {e}")
        traceback.print_exc()
    finally:
        print("正在关闭 UI Server 资源...")
        try:
            receiver.setsockopt(zmq.LINGER, 0)
            sender.setsockopt(zmq.LINGER, 0)
            receiver.close()
            sender.close()
            context.term()
        except Exception as e:
            print(f"关闭资源时出错: {e}")
        print("已关闭。")
        

def download_file(filename):
    """
    下载文件到当前目录
    """
    url = f"https://gitee.com/opguess/idv-login/raw/main/binaries/{filename}"
    response = requests.get(url)
    if response.status_code != 200:
        print(f"下载文件 {filename} 失败，状态码：{response.status_code}")
        print(response.text)
        #fallback
        url = f"https://raw.githubusercontent.com/KKeygen/idv-login/refs/heads/main/binaries/{filename}"
        response = requests.get(url)
        if response.status_code != 200:
            print(f"下载文件 {filename} 失败，状态码：{response.status_code}")
            return False
    with open(filename, "wb") as f:
        f.write(response.content)
    #如果文件名以.zip结尾，原地解压
    if filename.endswith(".zip"):
        with zipfile.ZipFile(filename, 'r') as zip_ref:
            base_dir = os.path.realpath(".")
            for member in zip_ref.namelist():
                member_path = os.path.realpath(os.path.join(".", member))
                if os.path.commonpath([base_dir, member_path]) != base_dir:
                    raise Exception(f"Zip path traversal detected: {member}")
            zip_ref.extractall(".")
        #删除压缩包
        os.remove(filename)
    return True
def ensure_binary():
    r"""
    确保二进制文件存在：binaries\downloadIPC.exe,binaries\OrbitSDK.dll
    """
    #https://gitee.com/opguess/idv-login/raw/main/binaries/aria2c.exe
    #https://gitee.com/opguess/idv-login/raw/main/binaries/downloadIPC.exe
    #https://gitee.com/opguess/idv-login/raw/main/binaries/OrbitSDK.dll
    #直接下载的工作目录
    #fallback:https://cdn.jsdelivr.net/gh/KKeygen/idv-login@main/
    print("正在检查并下载依赖...")
    if os.path.exists("downloadIPC.exe") and os.path.exists("OrbitSDK.dll") and os.path.exists("aria2c.exe"):
        return True
    if not os.path.exists("downloadIPC.exe"):
        res=download_file("downloadIPC.zip")
        if not res:
            return False
    if not os.path.exists("OrbitSDK.dll"):
        res=download_file("OrbitSDK.dll")
        if not res:
            return False
    if not os.path.exists("aria2c.exe"):
        res=download_file("aria2c.exe")
        if not res:
            return False
    return True

def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-server", action="store_true")
    parser.add_argument("--topic", type=str, default="")
    parser.add_argument("--sub-port", type=int, default=PORT_SEND_HEARTBEAT)
    parser.add_argument("--pub-port", type=int, default=PORT_RECEIVE_PROGRESS)
    return parser.parse_args()

if __name__ == "__main__":
    args = _parse_args()
    if args.ui_server:
        topic = args.topic.encode("utf-8") if args.topic else TOPIC
        main_ui_server(topic=topic, sub_port=args.sub_port, pub_port=args.pub_port)
    else:
        ensure_binary()
