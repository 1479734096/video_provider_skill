import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from typing import Dict, Any, Optional, Tuple

import requests

FFMPEG_PATH_DEFAULT = r"C:\Users\lenovo\AppData\Local\Programs\Python\Python312\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER = "https://www.bilibili.com"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

def resolve_ffmpeg_path(custom_path: Optional[str] = None) -> str:
    if custom_path and os.path.exists(custom_path):
        return custom_path
    if os.path.exists(FFMPEG_PATH_DEFAULT):
        return FFMPEG_PATH_DEFAULT
    return "ffmpeg"

def parse_time_str(time_val: Any) -> float:
    if isinstance(time_val, (int, float)):
        return float(time_val)
    time_str = str(time_val).strip()
    if ":" in time_str:
        parts = time_str.split(":")
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    return float(time_str)

def format_time_srt(seconds: float) -> str:
    millis = int(round((seconds - int(seconds)) * 1000))
    total_sec = int(seconds)
    secs = total_sec % 60
    total_min = total_sec // 60
    mins = total_min % 60
    hours = total_min // 60
    return f"{hours:02d}:{mins:02d}:{secs:02d},{millis:03d}"

def extract_bvid(target_url: str, session: requests.Session) -> Tuple[str, int]:
    url = target_url.strip()
    if "b23.tv" in url:
        resp = session.get(url, allow_redirects=True, timeout=10)
        url = resp.url
    match = re.search(r"BV[0-9a-zA-Z]{10}", url)
    if not match:
        raise ValueError(f"URL 缺少有效 BV 号: {target_url}")
    bvid = match.group(0)
    p = 1
    p_match = re.search(r"[?&]p=(\d+)", url)
    if p_match:
        p = int(p_match.group(1))
    return bvid, p

def get_wbi_keys(session: requests.Session) -> Tuple[str, str]:
    resp = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
    wbi_img = resp["data"]["wbi_img"]
    img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key

def enc_wbi(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
    raw_key = img_key + sub_key
    mixin_key = "".join([raw_key[i] for i in MIXIN_KEY_ENC_TAB])[:32]
    params["wts"] = round(time.time())
    sorted_params = dict(sorted(params.items()))
    cleaned_params = {
        k: "".join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in sorted_params.items()
    }
    query_str = urllib.parse.urlencode(cleaned_params)
    w_rid = hashlib.md5((query_str + mixin_key).encode("utf-8")).hexdigest()
    cleaned_params["w_rid"] = w_rid
    return cleaned_params

def fetch_video_info(bvid: str, session: requests.Session) -> Dict[str, Any]:
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    resp = session.get(url, timeout=10).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"获取视频信息失败: {resp.get('message')}")
    return resp["data"]

def fetch_play_url(bvid: str, cid: int, qn: int, img_key: str, sub_key: str, session: requests.Session) -> str:
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnval": 0,
        "fnver": 0,
        "fourk": 0
    }
    signed = enc_wbi(params, img_key, sub_key)
    url = "https://api.bilibili.com/x/player/wbi/playurl"
    resp = session.get(url, params=signed, timeout=10).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"获取播放地址失败: {resp.get('message')}")
    durl = resp.get("data", {}).get("durl", [])
    if not durl:
        raise RuntimeError("播放列表数据为空")
    return durl[0]["url"]

def fetch_and_convert_subtitle(
    aid: int,
    cid: int,
    start_sec: float,
    duration_sec: float,
    img_key: str,
    sub_key: str,
    session: requests.Session,
    output_srt_path: str
) -> Optional[str]:
    params = {"aid": aid, "cid": cid}
    signed = enc_wbi(params, img_key, sub_key)
    url = "https://api.bilibili.com/x/player/wbi/v2"
    resp = session.get(url, params=signed, timeout=10).json()
    subtitles = resp.get("data", {}).get("subtitle", {}).get("subtitles", [])
    if not subtitles:
        return None
    selected_sub = None
    for sub in subtitles:
        if "zh" in sub.get("lan", ""):
            selected_sub = sub
            break
    if not selected_sub:
        selected_sub = subtitles[0]
    sub_url = selected_sub.get("subtitle_url", "")
    if not sub_url:
        return None
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    sub_content = session.get(sub_url, timeout=10).json()
    body = sub_content.get("body", [])
    if not body:
        return None

    end_sec = start_sec + duration_sec
    srt_lines = []
    idx = 1
    for item in body:
        item_from = float(item["from"])
        item_to = float(item["to"])
        if item_to < start_sec or item_from > end_sec:
            continue
        slice_from = max(0.0, item_from - start_sec)
        slice_to = max(0.0, item_to - start_sec)
        srt_lines.append(str(idx))
        srt_lines.append(f"{format_time_srt(slice_from)} --> {format_time_srt(slice_to)}")
        srt_lines.append(item["content"])
        srt_lines.append("")
        idx += 1

    if not srt_lines:
        return None

    with open(output_srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    return output_srt_path

def cut_video(
    ffmpeg_bin: str,
    stream_url: str,
    start_sec: float,
    duration_sec: float,
    resolution: str,
    output_path: str
) -> None:
    headers_arg = f"User-Agent: {USER_AGENT}\r\nReferer: {REFERER}\r\n"
    scale_filter = "scale=-2:480" if resolution == "480p" else "scale=-2:360"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-headers", headers_arg,
        "-ss", str(start_sec),
        "-i", stream_url,
        "-t", str(duration_sec),
        "-vf", scale_filter,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        output_path
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg 切片处理失败: {proc.stderr}")

def provide_video(
    url_or_bvid: str,
    start_time: Any = 0,
    duration: float = 15.0,
    resolution: str = "480p",
    cache_dir: str = ".cache",
    force: bool = False,
    custom_ffmpeg: Optional[str] = None
) -> Dict[str, Any]:
    duration_val = float(duration)
    duration_val = max(15.0, min(60.0, duration_val))
    start_sec = parse_time_str(start_time)

    res_val = resolution.lower().strip()
    if res_val not in ["360p", "480p"]:
        res_val = "480p"
    qn_map = {"360p": 16, "480p": 32}
    target_qn = qn_map[res_val]

    os.makedirs(cache_dir, exist_ok=True)
    ffmpeg_bin = resolve_ffmpeg_path(custom_ffmpeg)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": REFERER})
    try:
        spi_resp = session.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=5).json()
        b_3 = spi_resp.get("data", {}).get("b_3")
        b_4 = spi_resp.get("data", {}).get("b_4")
        if b_3:
            session.cookies.set("buvid3", b_3, domain=".bilibili.com")
        if b_4:
            session.cookies.set("buvid4", b_4, domain=".bilibili.com")
    except Exception:
        pass

    bvid, p = extract_bvid(url_or_bvid, session)
    cache_base_name = f"{bvid}_p{p}_ss{int(start_sec)}_t{int(duration_val)}_{res_val}"
    target_mp4 = os.path.abspath(os.path.join(cache_dir, f"{cache_base_name}.mp4"))
    target_srt = os.path.abspath(os.path.join(cache_dir, f"{cache_base_name}.srt"))

    if not force and os.path.exists(target_mp4) and os.path.getsize(target_mp4) > 0:
        existing_srt = target_srt if os.path.exists(target_srt) and os.path.getsize(target_srt) > 0 else None
        return {
            "status": "success",
            "cached": True,
            "bvid": bvid,
            "p": p,
            "start": start_sec,
            "duration": duration_val,
            "resolution": res_val,
            "video_path": target_mp4,
            "subtitle_path": existing_srt
        }

    info = fetch_video_info(bvid, session)
    title = info.get("title", "")
    aid = info.get("aid", 0)
    pages = info.get("pages", [])
    if p - 1 < len(pages):
        cid = pages[p - 1]["cid"]
    else:
        cid = info.get("cid", 0)

    img_key, sub_key = get_wbi_keys(session)
    stream_url = fetch_play_url(bvid, cid, target_qn, img_key, sub_key, session)

    cut_video(ffmpeg_bin, stream_url, start_sec, duration_val, res_val, target_mp4)

    srt_path = fetch_and_convert_subtitle(
        aid, cid, start_sec, duration_val, img_key, sub_key, session, target_srt
    )

    return {
        "status": "success",
        "cached": False,
        "title": title,
        "bvid": bvid,
        "p": p,
        "start": start_sec,
        "duration": duration_val,
        "resolution": res_val,
        "video_path": target_mp4,
        "subtitle_path": srt_path
    }

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="视频切片获取工具")
    parser.add_argument("url", help="视频链接或 BV 号")
    parser.add_argument("--start", default="0", help="起始时间 (秒或 HH:MM:SS)")
    parser.add_argument("--duration", type=float, default=15.0, help="切片时长 (15 至 60 秒)")
    parser.add_argument("--resolution", default="480p", choices=["360p", "480p"], help="视频分辨率")
    parser.add_argument("--cache-dir", default=".cache", help="缓存目录")
    parser.add_argument("--force", action="store_true", help="强制更新缓存")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--ffmpeg", default=None, help="指定 FFmpeg 执行文件路径")

    args = parser.parse_args()
    try:
        res = provide_video(
            url_or_bvid=args.url,
            start_time=args.start,
            duration=args.duration,
            resolution=args.resolution,
            cache_dir=args.cache_dir,
            force=args.force,
            custom_ffmpeg=args.ffmpeg
        )
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"状态: {res['status']}")
            print(f"视频文件: {res['video_path']}")
            print(f"字幕文件: {res.get('subtitle_path')}")
            print(f"缓存状态: {res['cached']}")
    except Exception as e:
        err = {"status": "error", "message": str(e)}
        if args.json:
            print(json.dumps(err, ensure_ascii=False, indent=2))
        else:
            print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
