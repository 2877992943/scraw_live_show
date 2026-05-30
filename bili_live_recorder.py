#!/usr/bin/env python3
"""
B站直播录制脚本
支持: 选择清晰度、自动重连、按时间分段、Ctrl+C 安全停止




这次从系统架构和数据流向的角度来解读：
  ──────────────────────────────────────────────────────────────────────────────
  一、整体架构（四层模型）
  ┌─────────────────────────────────────────────────────────────┐
  │  第1层：用户接口层                                           │
  │  ─────────────────                                           │
  │  解析命令行参数 → 提取房间号 → 调用 record_live()              │
  └─────────────────────────────────────────────────────────────┘
                                ↓
  ┌─────────────────────────────────────────────────────────────┐
  │  第2层：信息获取层                                           │
  │  ─────────────────                                           │
  │  get_room_info()      → B站房间API → 直播状态/标题/主播       │
  │  get_stream_urls()    → B站播放API → 真实的直播流CDN地址       │
  │  choose_best_stream() → 从N个地址中挑FLV格式的               │
  └─────────────────────────────────────────────────────────────┘
                                ↓
  ┌─────────────────────────────────────────────────────────────┐
  │  第3层：录制执行层                                           │
  │  ─────────────────                                           │
  │  record_with_ffmpeg() → 启动 ffmpeg 拉流写入文件              │
  │       ├── 内部统一写 MKV（流式容器，异常不损坏）              │
  │       └── 正常结束后 → _remux_to_target() 转封装为 MP4/FLV   │
  └─────────────────────────────────────────────────────────────┘
                                ↓
  ┌─────────────────────────────────────────────────────────────┐
  │  第4层：状态管理层（核心大循环）                              │
  │  ─────────────────────────────────                            │
  │  while not STOP_FLAG:                                        │
  │      检查直播状态 → 获取流地址 → 启动录制 → 处理结果 → 决策   │
  └─────────────────────────────────────────────────────────────┘
  ──────────────────────────────────────────────────────────────────────────────
  二、数据流向（完整链路）
  你输入 "https://live.bilibili.com/1919958156"
             │
             ▼
      extract_room_id() ──→ "1919958156"
             │
             ▼
      get_room_info() ─────→ 调用 B站API
             │                    │
             ▼                    ▼
      得到 {                     得到 {
        live_status: 1,            streams: [
        title: "ququ直播中",         {protocol:"http_stream", format:"flv", url:
  "https://cdn..."},
        uid: 35466...,             {protocol:"http_hls",    format:"ts",  url:"h
  ttps://cdn..."},
      }                            ...
                                 ]
             │                    │
             ▼                    ▼
      如果 live_status != 1      choose_best_stream()
      → 等待10秒重试                → 优先选 FLV 格式
             │                         │
             │    ┌────────────────────┘
             │    ▼
             │  stream_url = "https://cdn.../live_.../xxx.flv"
             │    │
             └───→┘
                  ▼
      record_with_ffmpeg(stream_url, "ququ直播中_20250530_102754.mkv")
             │
             ├── 启动 ffmpeg 进程
             │      ffmpeg -headers "..." -i <stream_url> -c copy -f matroska xx
  x.mkv
             │      ↑ 持续从B站CDN拉取视频数据
             │
             ├── ffmpeg 异常退出？
             │      → 返回 "error"
             │
             ├── 用户按 Ctrl+C？
             │      → signal_handler 设置 STOP_FLAG = True
             │      → proc.terminate() 优雅关闭 ffmpeg
             │      → 返回 "stopped"
             │
             └── ffmpeg 正常结束？
                    → 返回 "done"
                           │
                           ▼
                    如果用户指定 -f mp4
                           │
                           ▼
                    ffmpeg -i xxx.mkv -c copy -movflags +faststart xxx.mp4
                           │
                           ▼
                    删除中间文件 xxx.mkv
                           │
                           ▼
                    最终输出: recordings/ququ直播中_xxx.mp4
  ──────────────────────────────────────────────────────────────────────────────
  三、核心大循环的状态机
  record_live() 里的 while not STOP_FLAG: 是整脚本的心脏，处理了所有实际场景：
                      ┌─────────────┐
                      │   开始循环   │
                      └──────┬──────┘
                             │
                             ▼
                ┌────────────────────────┐
                │  get_room_info()       │
                │  检查 live_status      │
                └──────┬─────────────────┘
                       │
           ┌───────────┼───────────┐
           ▼           ▼           ▼
      live_status=0  网络出错   live_status=1
      (未开播)      (请求失败)   (正在直播)
           │           │           │
           ▼           ▼           ▼
      sleep(10)    sleep(10)   获取流地址
      继续循环       继续循环      启动ffmpeg
                                    │
                                    ▼
                      ┌─────────────────────────┐
                      │   ffmpeg 录制中...       │
                      │   实时读取stdout         │
                      └──────┬──────────────────┘
                             │
                ┌────────────┼────────────┬────────────┐
                ▼            ▼            ▼            ▼
           网络断开      主播下播       用户Ctrl+C    一切正常
           ffmpeg报错   流自然结束    STOP_FLAG=True  proc.wait()=0
                │            │            │            │
                ▼            ▼            ▼            ▼
           返回"error"   返回"done"   返回"stopped" 返回"done"
                │            │            │            │
                └────────────┴────────────┴────────────┘
                             │
                             ▼
                ┌────────────────────────┐
                │  如果 result=="error"  │
                │  → sleep(10) 重连      │
                │                        │
                │  如果 result=="done"   │
                │  → 检查是否需要分段     │
                │  → 是：继续下一段       │
                │  → 否：回到循环开头     │
                │                        │
                │  如果 STOP_FLAG=True   │
                │  → break 退出循环       │
                └────────────────────────┘
  这个循环的精妙之处：
  • 主播下播再开播 → 自动检测到 live_status=1，恢复录制
  • 网络短暂断开 → ffmpeg 报错 → 10秒后重连，不丢后续内容
  • 用户按 Ctrl+C → 优雅关闭，文件不损坏
  ──────────────────────────────────────────────────────────────────────────────
  四、文件格式的"双轨策略"
  这是脚本最重要的设计决策：
   阶段       格式          原因
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   录制阶段   MKV           流式容器，数据从头到尾顺序写。即使进程被 kill，已写
                            入的部分完全可读
   输出阶段   MP4/MKV/FLV   用户指定。MP4 用 -movflags +faststart 把元数据移到
                            文件头，播放器兼容最好
  ┌─────────────────┐         ┌─────────────────┐
  │   B站直播流      │  ffmpeg │   临时 MKV 文件  │
  │  (FLV over HTTP) │ ──────→ │  (流式写入)      │
  └─────────────────┘  -c copy └─────────────────┘
                                            │
                                            ▼
                                如果用户要 MP4：
                                            │
                                            ▼
                                ffmpeg -c copy -movflags +faststart
                                            │
                                            ▼
                                ┌─────────────────┐
                                │   最终 MP4 文件  │
                                │  (moov在头部)    │
                                └─────────────────┘
  为什么不用 FLV 直接录？ FLV 也是流式容器，但尾部需要写 onMetaData，被 kill 后
  会损坏。
  为什么不用 MP4 直接录？ MP4 的 moov 元数据通常在文件末尾，异常退出 → moov 丢 …
  → 播放器无法识别。
  ──────────────────────────────────────────────────────────────────────────────
  五、信号处理的"优雅退出"机制
  用户按下 Ctrl+C
       │
       ▼
  OS 发送 SIGINT 信号
       │
       ▼
  signal_handler() 被调用
       │
       └──→ STOP_FLAG = True
                  │
                  ▼
      record_with_ffmpeg() 的 for 循环中：
                  │
                  ├── 检测到 STOP_FLAG == True
                  │      → break 跳出读取循环
                  │
                  ├── proc.terminate()  ← 发送 SIGTERM 给 ffmpeg
                  │   （ffmpeg 收到后把缓冲区数据flush到磁盘，关闭文件）
                  │
                  ├── proc.wait(timeout=5)  ← 等 ffmpeg 自己退出
                  │
                  └── 如果 5秒还没退：
                          → proc.kill() 强制杀死
                          → 但此时文件已经安全了
  对比粗暴 kill： 如果不用这个机制，直接 kill -9 ffmpeg，MKV 虽然大概率还能播（流式容器），但最后一个 GO…
  可能不完整。
  ───────────────────────────────────────────────────────────────────────────────────────────────────────
  六、关键参数的作用
   参数                      作用
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   -c copy                   不重新编码，CPU 占用接近 0，录 8 小时也不卡
   -headers "Referer: ..."   没有这个，B站 CDN 返回 403 Forbidden
   -rw_timeout 5000000       5 秒没拉到数据就认为断线，ffmpeg 退出
   -movflags +faststart      把 MP4 的元数据（moov）移到文件头部，在线播放也能秒开
   -f matroska               输出 MKV 容器，流式格式，异常安全
  ────────────────────────────────────────────────────────────────────────────────────────────────
  
"""

import os
import sys
import re
import time
import json
import signal
import subprocess
import argparse
import requests
from datetime import datetime
from urllib.parse import urlencode

# 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
}

# 清晰度代码
QN_MAP = {
    "原画": 10000,
    "蓝光": 400,
    "超清": 250,
    "高清": 150,
    "流畅": 80,
}

# 全局停止标志
STOP_FLAG = False


def signal_handler(signum, frame):
    global STOP_FLAG
    print("\n[!] 收到停止信号，正在安全退出...")
    STOP_FLAG = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def extract_room_id(url_or_id: str) -> str:
    """从URL或ID中提取房间号"""
    if url_or_id.isdigit():
        return url_or_id
    m = re.search(r"live\.bilibili\.com/(\d+)", url_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"无法解析房间号: {url_or_id}")


def get_room_info(room_id: str):
    """获取直播间基本信息"""
    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?id={room_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取房间信息失败: {data}")
    return data["data"]


def get_stream_urls(room_id: str, qn: int = 80):
    """
    获取直播流地址
    qn: 清晰度代码 (10000=原画, 400=蓝光, 250=超清, 150=高清, 80=流畅)
    """
    params = {
        "room_id": room_id,
        "protocol": "0,1",      # 0=http_stream, 1=http_hls
        "format": "0,1,2",      # flv, ts, fmp4
        "codec": "0,1,2",       # AVC, HEVC, AV1
        "qn": qn,
        "platform": "web",
        "ptype": 8,
        "dolby": 5,
        "panorama": 1,
    }
    url = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?" + urlencode(params)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取播放信息失败: {data}")

    playurl = data["data"]["playurl_info"]["playurl"]
    streams = []
    for stream in playurl.get("stream", []):
        protocol_name = stream.get("protocol_name", "")
        for format_info in stream.get("format", []):
            format_name = format_info.get("format_name", "")
            for codec in format_info.get("codec", []):
                base_url = codec.get("base_url", "")
                url_info = codec.get("url_info", [])
                current_qn = codec.get("current_qn", 0)
                accept_qn = codec.get("accept_qn", [])
                for info in url_info:
                    host = info.get("host", "")
                    extra = info.get("extra", "")
                    full_url = host + base_url + extra
                    streams.append({
                        "protocol": protocol_name,
                        "format": format_name,
                        "qn": current_qn,
                        "accept_qn": accept_qn,
                        "url": full_url,
                    })
    return streams


def choose_best_stream(streams: list, prefer_format: str = "flv"):
    """选择最佳流地址，优先指定格式"""
    # 先尝试指定格式
    for s in streams:
        if s["format"] == prefer_format:
            return s
    #  fallback 到 flv
    for s in streams:
        if s["format"] == "flv":
            return s
    # 最后选第一个
    return streams[0] if streams else None


def _build_ffmpeg_cmd(stream_url: str, output_path: str, is_mkv: bool = True):
    """构建 ffmpeg 命令"""
    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
        "Referer: https://live.bilibili.com/\r\n"
    )
    # 内部录制统一用 MKV（流式容器，异常中断也能播放）
    fmt = "matroska" if is_mkv else "flv"
    cmd = [
        "ffmpeg",
        "-y",
        "-headers", headers,
        "-rw_timeout", "5000000",
        "-i", stream_url,
        "-c", "copy",
        "-f", fmt,
        output_path,
    ]
    return cmd


def _remux_to_target(mkv_path: str, target_path: str):
    """将 MKV 转封装为用户指定的格式（MP4/FLV等）"""
    if target_path == mkv_path:
        return True
    print(f"[*] 正在转封装为 {target_path} ...")
    cmd = [
        "ffmpeg", "-y", "-i", mkv_path,
        "-c", "copy",
        "-movflags", "+faststart",
        target_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0:
            print(f"[+] 转封装完成: {target_path}")
            return True
        else:
            print(f"[!] 转封装失败: {proc.stderr.decode('utf-8', errors='ignore')[:200]}")
            return False
    except Exception as e:
        print(f"[!] 转封装异常: {e}")
        return False


def record_with_ffmpeg(stream_url: str, output_path: str, final_ext: str = "mkv"):
    """使用 ffmpeg 录制直播流
    内部先用 MKV 录制（流式容器，异常中断也能播），
    正常结束后自动转封装为用户指定的格式。
    """
    # 内部先用 MKV 录
    mkv_path = output_path
    if final_ext != "mkv":
        mkv_path = output_path.rsplit(".", 1)[0] + ".mkv"

    cmd = _build_ffmpeg_cmd(stream_url, mkv_path, is_mkv=True)

    print(f"[+] 启动录制: {mkv_path}")
    print(f"    ffmpeg {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    # 实时输出 ffmpeg 日志
    for line in proc.stdout:
        line = line.rstrip()
        if "error" in line.lower() or "Error" in line:
            print(f"    [ffmpeg] {line}")
        if STOP_FLAG:
            break

    if STOP_FLAG and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"[+] 录制已停止，MKV文件保存至: {mkv_path}")
        # 如果用户要的不是 MKV，尝试转封装
        if final_ext != "mkv":
            success = _remux_to_target(mkv_path, output_path)
            if success:
                os.remove(mkv_path)
                print(f"[+] 最终文件: {output_path}")
            else:
                print(f"[!] 保留 MKV 文件: {mkv_path}")
        return "stopped"

    ret = proc.wait()
    if ret != 0:
        print(f"[!] ffmpeg 异常退出，返回码: {ret}")
        return "error"

    # 正常结束，自动转封装
    if final_ext != "mkv":
        success = _remux_to_target(mkv_path, output_path)
        if success and os.path.exists(mkv_path):
            os.remove(mkv_path)
        elif not success:
            print(f"[!] 转封装失败，保留 MKV: {mkv_path}")
    return "done"


def record_live(room_id: str, qn_label: str = "流畅", output_dir: str = "./recordings",
                split_minutes: int = 0, retry_interval: int = 10, fmt: str = "mkv"):
    """
    录制直播主循环
    split_minutes: 分段时长（分钟），0表示不分段
    """
    global STOP_FLAG
    os.makedirs(output_dir, exist_ok=True)

    # 获取真实房间信息
    print(f"[*] 正在获取房间 {room_id} 信息...")
    info = get_room_info(room_id)
    live_status = info.get("live_status")
    title = info.get("title", "unknown")
    uid = info.get("uid", "unknown")
    uname = info.get("anchor_name", "unknown")

    # 清理文件名非法字符
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
    print(f"[*] 主播: {uname} (UID:{uid})")
    print(f"[*] 标题: {title}")
    if live_status != 1:
        print(f"[!] 当前未在直播 (状态码:{live_status})，等待开播...")

    qn = QN_MAP.get(qn_label, 80)
    print(f"[*] 清晰度设置: {qn_label} (qn={qn})")

    segment_index = 0
    current_file = None
    start_time = None

    while not STOP_FLAG:
        # 检查直播状态
        try:
            info = get_room_info(room_id)
        except Exception as e:
            print(f"[!] 获取房间信息失败: {e}")
            time.sleep(retry_interval)
            continue

        if info.get("live_status") != 1:
            if current_file:
                print(f"[*] 直播已结束，当前文件: {current_file}")
                current_file = None
            print(f"[*] 等待直播中... {retry_interval}s 后重试")
            time.sleep(retry_interval)
            continue

        # 获取流地址
        try:
            streams = get_stream_urls(room_id, qn=qn)
        except Exception as e:
            print(f"[!] 获取流地址失败: {e}")
            time.sleep(retry_interval)
            continue

        if not streams:
            print("[!] 未获取到流地址")
            time.sleep(retry_interval)
            continue

        stream = choose_best_stream(streams, prefer_format="flv")
        stream_url = stream["url"]
        actual_qn = stream["qn"]
        print(f"[*] 流格式: {stream['protocol']}/{stream['format']}, 实际qn: {actual_qn}")

        # 生成分段文件名
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = fmt
        if split_minutes > 0:
            segment_index += 1
            filename = f"{safe_title}_{now}_p{segment_index:04d}.{ext}"
        else:
            filename = f"{safe_title}_{now}.{ext}"

        output_path = os.path.join(output_dir, filename)
        current_file = output_path
        start_time = time.time()

        # 开始录制
        result = record_with_ffmpeg(stream_url, output_path, final_ext=fmt)

        if STOP_FLAG:
            break

        if result == "error":
            print(f"[*] {retry_interval}s 后尝试重连...")
            time.sleep(retry_interval)
            continue

        # 检查是否需要分段
        if split_minutes > 0 and not STOP_FLAG:
            elapsed = time.time() - start_time
            if elapsed >= split_minutes * 60:
                continue  # 自然结束，继续下一段

    print("[*] 录制任务已结束")


def main():
    parser = argparse.ArgumentParser(description="B站直播录制工具")
    parser.add_argument("room", help="直播间URL或房间号，例如: https://live.bilibili.com/1919958156 或 1919958156")
    parser.add_argument("-q", "--quality", choices=list(QN_MAP.keys()), default="流畅",
                        help="录制清晰度 (默认: 流畅)")
    parser.add_argument("-o", "--output", default="./recordings", help="输出目录 (默认: ./recordings)")
    parser.add_argument("-s", "--split", type=int, default=0, metavar="MINUTES",
                        help="按分钟分段录制，0表示不分段 (默认: 0)")
    parser.add_argument("-r", "--retry", type=int, default=10, help="断线重试间隔秒数 (默认: 10)")
    parser.add_argument("-f", "--format", choices=["mkv", "mp4", "flv"], default="mkv",
                        help="录制容器格式: mkv(异常也能播,推荐), mp4(兼容性好), flv(直播原生). 默认: mkv")

    args = parser.parse_args()

    room_id = extract_room_id(args.room)
    print(f"=" * 50)
    print(f"B站直播录制工具")
    print(f"目标房间: {room_id}")
    print(f"=" * 50)

    record_live(
        room_id=room_id,
        qn_label=args.quality,
        output_dir=args.output,
        split_minutes=args.split,
        retry_interval=args.retry,
        fmt=args.format,
    )


if __name__ == "__main__":
    main()
