#!/usr/bin/env python3
"""
 
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
    """选择最佳流地址，优先最低分辨率，再优先指定格式"""
    if not streams:
        return None
    # 先按 qn 升序排序，qn 越小分辨率越低
    streams_sorted = sorted(streams, key=lambda s: s.get("qn", 99999))
    lowest_qn = streams_sorted[0].get("qn", 0)
    lowest_streams = [s for s in streams_sorted if s.get("qn", 0) == lowest_qn]

    # 在最低分辨率的流中，优先指定格式
    for s in lowest_streams:
        if s["format"] == prefer_format:
            return s
    # fallback 到 flv
    for s in lowest_streams:
        if s["format"] == "flv":
            return s
    # 最后选最低分辨率里的第一个
    return lowest_streams[0]


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

        # 按日期创建子目录
        date_str = datetime.now().strftime("%Y%m%d")
        daily_dir = os.path.join(output_dir, date_str)
        os.makedirs(daily_dir, exist_ok=True)

        # 生成分段文件名（时间部分保留时分秒）
        now = datetime.now().strftime("%H%M%S")
        ext = fmt
        if split_minutes > 0:
            segment_index += 1
            filename = f"{safe_title}_{now}_p{segment_index:04d}.{ext}"
        else:
            filename = f"{safe_title}_{now}.{ext}"

        output_path = os.path.join(daily_dir, filename)
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
