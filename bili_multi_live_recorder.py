#!/usr/bin/env python3
"""
Bilibili Multi-Live Recorder
支持多直播间轮询监控，每5分钟检查一次，自动录制开播的直播间
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
import threading
from datetime import datetime
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed

# Request headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
}

# Quality code
QN_MAP = {
    "original": 10000,
    "blue-ray": 400,
    "super-clear": 250,
    "high-def": 150,
    "smooth": 80,
}

# Maximum concurrent recordings allowed
MAX_CONCURRENT = 3

# Default room IDs to monitor (embedded in code)
DEFAULT_ROOM_IDS = [
    "1880711165",
    "32375710",
    "1830092998",
    "1966815469",
    "1817659697",
    "1919958156",
    "1766746936",
    "1861788451",
    "1935932795",
    "1844045829",
]

# Global stop flag
STOP_FLAG = False

# Active recording tasks: {room_id: {"thread": Thread, "proc": Popen, "output_path": str, "start_time": float}}
ACTIVE_RECORDINGS = {}
ACTIVE_LOCK = threading.Lock()


def signal_handler(signum, frame):
    global STOP_FLAG
    print("\n[!] Received stop signal, exiting safely...")
    STOP_FLAG = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def extract_room_id(url_or_id: str) -> str:
    """Extract room ID from URL or ID"""
    if url_or_id.isdigit():
        return url_or_id
    m = re.search(r"live\.bilibili\.com/(\d+)", url_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"Cannot parse room ID: {url_or_id}")


def get_room_info(room_id: str):
    """Get basic live room info"""
    url = f"https://api.live.bilibili.com/room/v1/Room/get_info?id={room_id}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get room info: {data}")
    return data["data"]


def get_stream_urls(room_id: str, qn: int = 80):
    """Get live stream URLs"""
    params = {
        "room_id": room_id,
        "protocol": "0,1",
        "format": "0,1,2",
        "codec": "0,1,2",
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
        raise RuntimeError(f"Failed to get play info: {data}")

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
    """Select best stream: lowest resolution first, then preferred format"""
    if not streams:
        return None
    streams_sorted = sorted(streams, key=lambda s: s.get("qn", 99999))
    lowest_qn = streams_sorted[0].get("qn", 0)
    lowest_streams = [s for s in streams_sorted if s.get("qn", 0) == lowest_qn]

    for s in lowest_streams:
        if s["format"] == prefer_format:
            return s
    for s in lowest_streams:
        if s["format"] == "flv":
            return s
    return lowest_streams[0]


def _build_ffmpeg_cmd(stream_url: str, output_path: str, is_mkv: bool = True, audio_only: bool = False):
    """Build ffmpeg command"""
    headers = (
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36\r\n"
        "Referer: https://live.bilibili.com/\r\n"
    )
    if audio_only:
        # Audio-only: disable video, copy audio stream, use ADTS/AAC container
        fmt = "adts"  # AAC audio format
        cmd = [
            "ffmpeg",
            "-y",
            "-headers", headers,
            "-rw_timeout", "5000000",
            "-i", stream_url,
            "-vn",           # no video
            "-c:a", "copy",  # copy audio codec
            "-f", fmt,
            output_path,
        ]
    else:
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
    """Remux MKV to user-specified format"""
    if target_path == mkv_path:
        return True
    print(f"[*] Remuxing to {target_path} ...")
    cmd = [
        "ffmpeg", "-y", "-i", mkv_path,
        "-c", "copy",
        "-movflags", "+faststart",
        target_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0:
            print(f"[+] Remux completed: {target_path}")
            return True
        else:
            print(f"[!] Remux failed: {proc.stderr.decode('utf-8', errors='ignore')[:200]}")
            return False
    except Exception as e:
        print(f"[!] Remux exception: {e}")
        return False


def _record_worker(room_id: str, stream_url: str, output_path: str, final_ext: str, room_title: str, audio_only: bool = False):
    """Worker thread: record a single live stream until it ends or STOP_FLAG is set"""
    global STOP_FLAG, ACTIVE_RECORDINGS

    # Determine temp path and build ffmpeg command
    if audio_only:
        temp_path = output_path.rsplit(".", 1)[0] + ".aac"
        cmd = _build_ffmpeg_cmd(stream_url, temp_path, is_mkv=False, audio_only=True)
        display_name = os.path.basename(temp_path)
    else:
        temp_path = output_path
        if final_ext != "mkv":
            temp_path = output_path.rsplit(".", 1)[0] + ".mkv"
        cmd = _build_ffmpeg_cmd(stream_url, temp_path, is_mkv=True)
        display_name = os.path.basename(temp_path)

    print(f"[Room {room_id}] Start recording: {display_name}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )

    # Register process
    with ACTIVE_LOCK:
        if room_id in ACTIVE_RECORDINGS:
            ACTIVE_RECORDINGS[room_id]["proc"] = proc

    # Monitor ffmpeg output
    for line in proc.stdout:
        line = line.rstrip()
        if "error" in line.lower() or "Error" in line:
            print(f"    [ffmpeg][Room {room_id}] {line}")
        if STOP_FLAG:
            break

    # Cleanup process
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    ret = proc.wait() if proc.poll() is None else proc.returncode

    # Remux if needed (video mode only)
    if not audio_only and final_ext != "mkv":
        success = _remux_to_target(temp_path, output_path)
        if success and os.path.exists(temp_path):
            os.remove(temp_path)
        elif not success:
            print(f"[!][Room {room_id}] Remux failed, keeping MKV: {temp_path}")

    # Remove from active recordings
    with ACTIVE_LOCK:
        if room_id in ACTIVE_RECORDINGS:
            del ACTIVE_RECORDINGS[room_id]

    if STOP_FLAG:
        print(f"[Room {room_id}] Recording stopped by user. File: {output_path}")
    elif ret != 0:
        print(f"[Room {room_id}] ffmpeg exited abnormally (code: {ret}). File: {output_path}")
    else:
        print(f"[Room {room_id}] Recording finished normally. File: {output_path}")


def start_recording(room_id: str, qn: int, output_dir: str, fmt: str, room_info: dict, audio_only: bool = False):
    """Start a new recording thread for a live room"""
    global ACTIVE_RECORDINGS

    with ACTIVE_LOCK:
        if room_id in ACTIVE_RECORDINGS:
            print(f"[*] Room {room_id} already being recorded, skip")
            return False

        # Enforce max concurrent limit
        if len(ACTIVE_RECORDINGS) >= MAX_CONCURRENT:
            print(f"[!] Room {room_id} delayed: max concurrent recordings ({MAX_CONCURRENT}) reached")
            return False

    # Get stream URLs
    try:
        streams = get_stream_urls(room_id, qn=qn)
    except Exception as e:
        print(f"[!][Room {room_id}] Failed to get stream URL: {e}")
        return False

    if not streams:
        print(f"[!][Room {room_id}] No stream URL obtained")
        return False

    stream = choose_best_stream(streams, prefer_format="flv")
    stream_url = stream["url"]
    actual_qn = stream["qn"]
    print(f"[*][Room {room_id}] Stream: {stream['protocol']}/{stream['format']}, qn={actual_qn}")

    # Prepare output path
    title = room_info.get("title", "unknown")
    uname = room_info.get("anchor_name", "unknown")
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title)
    date_str = datetime.now().strftime("%Y%m%d")
    daily_dir = os.path.join(output_dir, date_str)
    os.makedirs(daily_dir, exist_ok=True)

    now = datetime.now().strftime("%H%M%S")
    if audio_only:
        ext = "aac"
        filename = f"{safe_title}_{room_id}_{now}_audio.{ext}"
    else:
        ext = fmt
        filename = f"{safe_title}_{room_id}_{now}.{ext}"
    output_path = os.path.join(daily_dir, filename)

    # Create and start thread
    thread = threading.Thread(
        target=_record_worker,
        args=(room_id, stream_url, output_path, fmt, title, audio_only),
        daemon=True,
    )

    with ACTIVE_LOCK:
        ACTIVE_RECORDINGS[room_id] = {
            "thread": thread,
            "proc": None,
            "output_path": output_path,
            "start_time": time.time(),
            "title": title,
            "uname": uname,
        }

    thread.start()
    print(f"[+] Room {room_id} ({uname}) recording started -> {filename}")
    return True


def check_and_record(room_id: str, qn: int, output_dir: str, fmt: str, audio_only: bool = False):
    """Check room status and start recording if live and not already recording"""
    global ACTIVE_RECORDINGS, STOP_FLAG

    if STOP_FLAG:
        return

    # Check if already recording
    with ACTIVE_LOCK:
        if room_id in ACTIVE_RECORDINGS:
            # Already recording, check if still live by probing the process
            rec = ACTIVE_RECORDINGS[room_id]
            proc = rec.get("proc")
            if proc is not None and proc.poll() is not None:
                # Process has exited, remove it (will be cleaned by worker thread, but ensure here)
                print(f"[*] Room {room_id} recording process ended, removing from active list")
                del ACTIVE_RECORDINGS[room_id]
            else:
                # Still recording
                return

    # Fetch room info
    try:
        info = get_room_info(room_id)
    except Exception as e:
        print(f"[!][Room {room_id}] Failed to get room info: {e}")
        return

    live_status = info.get("live_status")
    title = info.get("title", "unknown")
    uname = info.get("anchor_name", "unknown")

    if live_status == 1:
        print(f"[*] Room {room_id} ({uname}) is LIVE: {title}")
        start_recording(room_id, qn, output_dir, fmt, info, audio_only)
    else:
        print(f"[*] Room {room_id} ({uname}) not live (status={live_status})")


def monitor_loop(room_ids: list, qn_label: str, output_dir: str, fmt: str, poll_interval: int = 300, audio_only: bool = False):
    """
    Main monitoring loop
    room_ids: list of room IDs to monitor
    poll_interval: seconds between checks (default 300 = 5 minutes)
    audio_only: if True, record audio stream only
    """
    global STOP_FLAG, ACTIVE_RECORDINGS

    os.makedirs(output_dir, exist_ok=True)
    qn = QN_MAP.get(qn_label, 80)

    print("=" * 60)
    print("Bilibili Multi-Live Recorder")
    print(f"Monitoring rooms: {', '.join(room_ids)}")
    print(f"Poll interval: {poll_interval}s ({poll_interval//60} minutes)")
    print(f"Quality: {qn_label} (qn={qn}) [lowest resolution]")
    print(f"Mode: {'Audio-only' if audio_only else 'Video+audio'}")
    print(f"Max concurrent: {MAX_CONCURRENT}")
    print(f"Output dir: {output_dir}")
    print("=" * 60)

    # Initial check immediately
    print("\n[*] Initial check...")
    for room_id in room_ids:
        check_and_record(room_id, qn, output_dir, fmt, audio_only)
        time.sleep(1)  # Small delay between rooms to avoid rate limiting

    cycle = 0
    while not STOP_FLAG:
        cycle += 1
        print(f"\n[*] --- Poll cycle #{cycle} | Active recordings: {len(ACTIVE_RECORDINGS)} ---")
        if ACTIVE_RECORDINGS:
            with ACTIVE_LOCK:
                for rid, rec in list(ACTIVE_RECORDINGS.items()):
                    elapsed = time.time() - rec["start_time"]
                    print(f"    [Recording] Room {rid} ({rec['uname']}) -> {os.path.basename(rec['output_path'])} | elapsed: {int(elapsed)}s")

        # Check all rooms
        for room_id in room_ids:
            check_and_record(room_id, qn, output_dir, fmt, audio_only)
            if STOP_FLAG:
                break
            time.sleep(1)

        if STOP_FLAG:
            break

        # Wait for next poll cycle
        print(f"[*] Next poll in {poll_interval}s...")
        waited = 0
        while waited < poll_interval and not STOP_FLAG:
            time.sleep(5)
            waited += 5

    # Shutdown: wait for all recordings to finish
    print("\n[*] Stopping all recordings...")
    with ACTIVE_LOCK:
        active_copy = list(ACTIVE_RECORDINGS.items())

    for room_id, rec in active_copy:
        proc = rec.get("proc")
        if proc and proc.poll() is None:
            print(f"[*] Terminating recording for room {room_id}...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # Wait for threads to finish
    for room_id, rec in active_copy:
        thread = rec.get("thread")
        if thread and thread.is_alive():
            print(f"[*] Waiting for room {room_id} thread to finish...")
            thread.join(timeout=30)

    print("[+] All recordings stopped. Exiting.")


def main():
    parser = argparse.ArgumentParser(description="Bilibili Multi-Live Recorder")
    parser.add_argument("rooms", nargs="*", default=None,
                        help="Live room URLs or IDs (optional, uses embedded defaults if omitted). E.g.: https://live.bilibili.com/1919958156 1919958156")
    parser.add_argument("-q", "--quality", choices=list(QN_MAP.keys()), default="smooth",
                        help="Recording quality (default: smooth = lowest resolution)")
    parser.add_argument("-o", "--output", default="./recordings", help="Output directory (default: ./recordings)")
    parser.add_argument("-i", "--interval", type=int, default=300, metavar="SECONDS",
                        help="Poll interval in seconds (default: 300 = 5 minutes)")
    parser.add_argument("-f", "--format", choices=["mkv", "mp4", "flv"], default="mkv",
                        help="Container format: mkv (fault-tolerant, recommended), mp4 (compatible), flv (native). Default: mkv")
    parser.add_argument("--audio-only", action="store_true",
                        help="Record audio stream only (no video). Output will be AAC format.")

    args = parser.parse_args()

    if args.rooms:
        room_ids = [extract_room_id(r) for r in args.rooms]
    else:
        room_ids = DEFAULT_ROOM_IDS
        print(f"[*] No rooms specified, using default list: {', '.join(room_ids)}")

    monitor_loop(
        room_ids=room_ids,
        qn_label=args.quality,
        output_dir=args.output,
        fmt=args.format,
        poll_interval=args.interval,
        audio_only=args.audio_only,
    )


if __name__ == "__main__":
    main()
