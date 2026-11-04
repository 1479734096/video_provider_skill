"""提供媒体文件与媒体事实，模型输入和理解由调用方负责。"""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib import error, parse, request


VERSION = "1.0.1"
TESTED_YTDLP_VERSION = "2026.10.26"
REMOTE_CLIP_PROFILE = "h264-aac-v2"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def safe_text(value: object) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: m[0].split("?", 1)[0], text)
    text = re.sub(r"(?i)(SESSDATA|bili_jct|access_token|refresh_token|authorization|api_key)\s*[:=]\s*[^\s;,]+", r"\1=<已省略>", text)
    return text[-3000:]


def milliseconds(value: object) -> int:
    try:
        parts = str(value).strip().split(":")
        if len(parts) > 3 or any(not part for part in parts):
            raise ValueError()
        seconds = Decimal(0)
        for index, part in enumerate(parts):
            component = Decimal(part)
            if not component.is_finite() or component < 0 or (index and component >= 60):
                raise ValueError()
            seconds = seconds * 60 + component
        return int((seconds * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (ValueError, InvalidOperation, OverflowError):
        raise ProviderError("INVALID_TIME", "时间必须是非负有限秒数，或 HH:MM:SS 格式") from None


def seconds(value: int) -> str:
    return f"{value // 1000}.{value % 1000:03d}"


def srt_time(value: int) -> str:
    hours, rest = divmod(value, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    whole, fraction = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{fraction:03d}"


def component(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_.+-]", "_", str(value))[:100] or "unknown"


def load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".part", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def crop_srt(text: str, start: int, end: int) -> tuple[str, int]:
    output = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            if block.strip():
                raise ProviderError("INVALID_SUBTITLE", "SRT 字幕块没有时间轴")
            continue
        left, right = lines[timing_index].split("-->", 1)
        cue_start = milliseconds(left.strip().replace(",", "."))
        cue_end = milliseconds(right.strip().split()[0].replace(",", "."))
        if cue_end <= cue_start:
            raise ProviderError("INVALID_SUBTITLE", "SRT 字幕结束时间未晚于起始时间")
        clipped_start, clipped_end = max(cue_start, start), min(cue_end, end)
        if clipped_start >= clipped_end:
            continue
        output.append(f"{len(output) + 1}\n{srt_time(clipped_start - start)} --> {srt_time(clipped_end - start)}\n" + "\n".join(lines[timing_index + 1:]))
    return "\n\n".join(output) + ("\n" if output else ""), len(output)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cache = Path(args.cache_dir).expanduser().resolve()
        self.temporary = self.cache / "tmp"
        self.temporary.mkdir(parents=True, exist_ok=True)
        self.stack = contextlib.ExitStack()
        self.warnings: list[str] = []
        self.tools: dict[str, str] = {}
        self.env = os.environ.copy()
        self.env.update(TEMP=str(self.temporary), TMP=str(self.temporary), TMPDIR=str(self.temporary))
        self.cookie_file: Path | None = None
        if getattr(args, "cookies", None):
            original = Path(args.cookies).expanduser().resolve()
            if not original.is_file():
                raise ProviderError("COOKIE_FILE_MISSING", "指定的 Cookie 文件不存在")
            directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="credentials-", dir=self.temporary)))
            self.cookie_file = directory / "cookies.txt"
            shutil.copyfile(original, self.cookie_file)
        self.opener = request.build_opener()
        if self.cookie_file:
            jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
            try:
                jar.load(ignore_discard=True, ignore_expires=False)
            except (OSError, http.cookiejar.LoadError):
                self.stack.close()
                raise ProviderError("INVALID_COOKIE_FILE", "Cookie 文件必须采用 Netscape 格式") from None
            self.opener = request.build_opener(request.HTTPCookieProcessor(jar))

    def close(self) -> None:
        self.stack.close()

    def warn(self, message: object) -> None:
        cleaned = safe_text(message).strip()
        if cleaned and cleaned not in self.warnings:
            self.warnings.append(cleaned)

    def tool(self, name: str) -> str:
        if name in self.tools:
            return self.tools[name]
        supplied = getattr(self.args, name.replace("-", "_"), None)
        env_name = "VIDEO_PROVIDER_" + name.upper().replace("-", "_")
        candidates = [supplied, os.environ.get(env_name)]
        if name == "ffprobe":
            ffmpeg_path = getattr(self.args, "ffmpeg", None) or os.environ.get("VIDEO_PROVIDER_FFMPEG")
            if ffmpeg_path:
                directory = Path(ffmpeg_path).expanduser()
                if not directory.is_dir():
                    directory = directory.parent
                candidates.append(str(directory / ("ffprobe.exe" if os.name == "nt" else "ffprobe")))
        candidates.append(str(self.cache / "tools" / (name + (".exe" if os.name == "nt" else ""))))
        candidates.append(shutil.which(name))
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate).expanduser()
            if path.is_dir():
                path = path / (name + (".exe" if os.name == "nt" else ""))
            if path.is_file():
                self.tools[name] = str(path.resolve())
                return self.tools[name]
            if candidate == supplied:
                raise ProviderError("DEPENDENCY_MISSING", f"指定的 {name} 不存在")
        raise ProviderError("DEPENDENCY_MISSING", f"找不到 {name}，请通过参数、{env_name}、项目 tools 目录或 PATH 提供；工具不会自动安装依赖")

    def stop_process_tree(self, process: subprocess.Popen) -> str:
        detail = ""
        try:
            if os.name == "nt":
                system_root = os.environ.get("SystemRoot")
                taskkill = str(Path(system_root) / "System32" / "taskkill.exe") if system_root else shutil.which("taskkill")
                if not taskkill:
                    raise OSError("找不到 Windows 进程树终止工具 taskkill")
                stopped = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                if stopped.returncode and process.poll() is None:
                    detail = f"系统进程树清理退出码 {stopped.returncode}"
            else:
                # 下载器会派生 FFmpeg；同一独立进程组必须一起停止，才能清理临时目录。
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = safe_text(exc)
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                detail = detail or "进程输出管道未能及时关闭"
        return f"；子进程清理信息：{detail}" if detail else ""

    def run(self, command: list[str], *, warnings: bool = False) -> str:
        process = None
        # 单文件下载器会在 TEMP 解包运行库，强制终止后也由本次调用统一回收。
        temporary = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="tool-", dir=self.temporary)))
        env = self.env | {"TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary)}
        try:
            process = subprocess.Popen(command, cwd=self.cache, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt")
            stdout, stderr = process.communicate(timeout=self.args.timeout)
        except subprocess.TimeoutExpired:
            detail = self.stop_process_tree(process) if process else ""
            raise ProviderError("TOOL_TIMEOUT", f"外部工具超过 {self.args.timeout} 秒，请缩短片段或调整 --timeout{detail}") from None
        except KeyboardInterrupt:
            detail = self.stop_process_tree(process) if process else ""
            if detail:
                raise ProviderError("CANCELED", "操作已中止" + detail) from None
            raise
        except OSError as exc:
            raise ProviderError("TOOL_START_FAILED", safe_text(exc)) from None
        if process.returncode:
            raise ProviderError("TOOL_FAILED", safe_text(stderr or stdout or f"退出码 {process.returncode}"))
        if warnings:
            for line in stderr.splitlines():
                if "WARNING:" in line or "ERROR:" in line:
                    self.warn(line)
        return stdout

    def doctor(self) -> dict:
        tools = {}
        for name, options in (("yt-dlp", ["--ignore-config", "--version"]), ("ffmpeg", ["-version"]), ("ffprobe", ["-version"])):
            item = {}
            try:
                item["path"] = self.tool(name)
                output = self.run([item["path"]] + options)
                item.update(status="available", version=output.strip().splitlines()[0])
                if name == "ffmpeg":
                    listing = self.run([item["path"], "-hide_banner", "-encoders"])
                    encoders = {fields[1] for line in listing.splitlines() if len(fields := line.split()) >= 2 and len(fields[0]) == 6 and fields[0][0] in "VAS"}
                    item["required_encoders"] = {encoder: encoder in encoders for encoder in ("libx264", "aac", "mjpeg")}
                    if not all(item["required_encoders"].values()):
                        item.update(status="unsupported", code="ENCODER_MISSING", message="FFmpeg 需要 libx264、aac 和 mjpeg 编码器")
                if name == "yt-dlp" and item["version"] != TESTED_YTDLP_VERSION:
                    self.warn(f"当前 yt-dlp 为 {item['version']}；已实测基线为 {TESTED_YTDLP_VERSION}，本工具不自动更改版本")
            except (ProviderError, OSError, ValueError, IndexError) as exc:
                item.update(status="unavailable", code=exc.code if isinstance(exc, ProviderError) else "DEPENDENCY_CHECK_FAILED", message=safe_text(exc))
            tools[name] = item
        ready = all(item["status"] == "available" for item in tools.values())
        return {"status": "ready" if ready else "dependencies_missing", "version": VERSION, "python": sys.version.split()[0], "platform": sys.platform, "cache_dir": str(self.cache), "tested_yt_dlp": TESTED_YTDLP_VERSION, "tools": tools, "warnings": self.warnings}

    def ytdlp(self) -> list[str]:
        command = [self.tool("yt-dlp"), "--ignore-config", "--no-playlist", "--no-cookies-from-browser", "--cache-dir", str(self.cache / "extractor"), "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2", "--encoding", "utf-8", "--no-progress"]
        if self.cookie_file:
            command += ["--cookies", str(self.cookie_file)]
        return command

    def get_json(self, url: str) -> dict:
        req = request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
        with self.opener.open(req, timeout=min(20, self.args.timeout)) as response:
            return json.load(response)

    def normalize(self, value: str) -> tuple[str, int]:
        value = value.strip()
        if re.fullmatch(r"BV[0-9A-Za-z]{10}|[aA][vV][0-9]+", value):
            value = "https://www.bilibili.com/video/" + value
        if value.startswith("b23.tv/"):
            value = "https://" + value
        short_opener = request.build_opener(NoRedirect())
        for _ in range(6):
            parsed = parse.urlsplit(value)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https"):
                raise ProviderError("UNSUPPORTED_INPUT", "输入应为本地媒体文件、B 站普通视频链接或 BV/AV 编号")
            if host != "b23.tv":
                break
            req = request.Request(value, headers={"User-Agent": USER_AGENT})
            try:
                response = short_opener.open(req, timeout=min(20, self.args.timeout))
            except error.HTTPError as exc:
                response = exc
            with response:
                location = response.headers.get("Location")
                if not location:
                    try:
                        reply = json.loads(response.read(4096).decode("utf-8", "replace"))
                        detail = f"，上游 code={reply.get('code')}" if isinstance(reply, dict) else ""
                    except ValueError:
                        detail = ""
                    raise ProviderError("SHORT_LINK_FAILED", f"b23 链接未返回有效重定向（HTTP {response.status}{detail}）")
                value = parse.urljoin(value, location)
        parsed = parse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        if host != "bilibili.com" and not host.endswith(".bilibili.com"):
            raise ProviderError("UNSUPPORTED_INPUT", "首版仅支持 B 站普通视频链接，短链也须指向该类视频")
        match = re.search(r"/(?:video|s/video)/(BV[0-9A-Za-z]{10}|[aA][vV][0-9]+)(?:/|$)", parsed.path)
        if not match:
            raise ProviderError("UNSUPPORTED_INPUT", "链接没有有效 BV/AV 编号；番剧、直播和合集不在首版范围内")
        try:
            page = self.args.page if self.args.page is not None else int(parse.parse_qs(parsed.query).get("p", ["1"])[-1])
        except ValueError:
            raise ProviderError("INVALID_PAGE", "分 P 必须是正整数") from None
        if page < 1:
            raise ProviderError("INVALID_PAGE", "分 P 必须从 1 开始")
        return f"https://www.bilibili.com/video/{match[1]}?p={page}", page

    def pages(self, url: str, page: int) -> tuple[list[dict], int | None]:
        identifier = parse.urlsplit(url).path.rsplit("/", 1)[-1]
        query = {"bvid": identifier} if identifier.startswith("BV") else {"aid": identifier[2:]}
        try:
            reply = self.get_json("https://api.bilibili.com/x/player/pagelist?" + parse.urlencode(query))
            if reply.get("code") != 0 or not isinstance(reply.get("data"), list):
                raise ValueError()
            pages = [{key: item.get(key) for key in ("page", "cid", "part", "duration")} for item in reply["data"]]
            if not 1 <= page <= len(pages):
                raise ProviderError("INVALID_PAGE", f"请求第 {page} P，但视频只有 {len(pages)} P")
            return pages, pages[page - 1]["cid"]
        except ProviderError:
            raise
        except (OSError, ValueError, KeyError):
            self.warn("上游未返回完整分 P 列表，使用下载器解析到的视频分 P 身份作为缓存标识")
            return [], None

    def probe(self, path: Path, *, require_video: bool = True) -> dict:
        data = json.loads(self.run([self.tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
        videos = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
        audios = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"]
        video = videos[0] if videos else {}
        try:
            duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
            numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/")
            fps = float(numerator) / float(denominator) if float(denominator) else 0
        except (ValueError, ZeroDivisionError):
            raise ProviderError("INVALID_MEDIA", "媒体时间或帧率信息无效") from None
        if not math.isfinite(duration) or duration <= 0 or (require_video and (not video or not video.get("width") or not video.get("height"))) or (not require_video and not audios):
            raise ProviderError("INVALID_MEDIA", "媒体没有有效视频轨道、尺寸或正时长")
        return {"duration": duration, "width": video.get("width"), "height": video.get("height"), "video_codec": video.get("codec_name"), "pixel_format": video.get("pix_fmt"), "color_range": video.get("color_range"), "fps": fps, "has_audio": bool(audios), "audio_codecs": [stream.get("codec_name") for stream in audios], "container": data.get("format", {}).get("format_name"), "size_bytes": path.stat().st_size}

    def describe(self, value: str, *, subtitles: bool = True) -> dict:
        path = Path(value).expanduser()
        if path.is_file():
            path = path.resolve()
            media = self.probe(path)
            stat = path.stat()
            origin = load_json(path.parent / "result.json") or {}
            offset = origin.get("actual_range", {}).get("source_start_ms", 0) if origin.get("video_path") == str(path) else 0
            tracks = []
            own_result = origin.get("video_path") == str(path)
            if own_result:
                for item in origin.get("subtitles", []):
                    if item.get("status") == "available" and item.get("path"):
                        tracks.append({key: item.get(key) for key in ("language", "source", "automatic")} | {"local_path": item["path"]})
            else:
                for subtitle in sorted(path.parent.glob(path.stem + "*.srt")):
                    if subtitle.stem != path.stem and not subtitle.stem.startswith(path.stem + "."):
                        continue
                    tracks.append({"language": subtitle.stem[len(path.stem):].lstrip(".") or "und", "source": "local", "automatic": None, "local_path": str(subtitle)})
            return {"source": {"kind": "local", "id": f"{stat.st_dev:x}-{stat.st_ino:x}-{stat.st_mtime_ns}-{stat.st_size}", "path": str(path), "timeline_offset_ms": offset}, "duration_ms": milliseconds(media["duration"]), "media": media, "tracks": tracks, "info": {}, "formats": [], "selected_formats": [], "pages": []}
        url, page = self.normalize(value)
        pages, cid = self.pages(url, page)
        selector = "bv*+ba/bv*/b"
        if self.args.max_height:
            selector = f"bv*[height<={self.args.max_height}]+ba/bv*[height<={self.args.max_height}]/b[height<={self.args.max_height}]"
        base = self.ytdlp() + ["--simulate", "--dump-single-json", "-f", selector]
        subtitle_options = ["--write-subs", "--write-auto-subs", "--sub-langs", "all,-danmaku"] if subtitles else []
        try:
            info = json.loads(self.run(base + subtitle_options + ["--", url], warnings=True))
        except ProviderError as exc:
            if not subtitles:
                raise
            info = json.loads(self.run(base + ["--", url], warnings=True))
            self.warn("字幕查询未完成，继续提供视频：" + str(exc))
        if info.get("_type") == "playlist" or info.get("is_live") or not info.get("duration"):
            raise ProviderError("UNSUPPORTED_SOURCE", "需要一个具有确定时长的普通视频分 P")
        identifier = re.search(r"BV[0-9A-Za-z]{10}", str(info.get("id", "")))
        bvid = identifier[0] if identifier else str(info.get("id", ""))
        tracks = []
        for category in ("subtitles", "automatic_captions"):
            for language, entries in (info.get(category) or {}).items():
                if language == "danmaku":
                    continue
                entry = next((item for item in entries if item.get("ext") == "srt"), entries[0] if entries else None)
                if entry:
                    tracks.append({"language": language, "source": "platform", "automatic": True if category == "automatic_captions" or language.startswith("ai-") else None, "entry": entry})
        format_keys = ("format_id", "ext", "width", "height", "fps", "vcodec", "acodec", "tbr", "format_note", "dynamic_range")
        formats = [{key: fmt.get(key) for key in format_keys} for fmt in info.get("formats", [])]
        selected = [{key: fmt.get(key) for key in format_keys} for fmt in info.get("requested_formats", [info])]
        return {"source": {"kind": "bilibili", "id": str(info["id"]), "bvid": bvid, "cid": cid, "page": page, "url": url, "title": info.get("title"), "credential_mode": "supplied_cookie_file" if self.cookie_file else "guest", "timeline_offset_ms": 0}, "duration_ms": milliseconds(info["duration"]), "info": info, "formats": formats, "selected_formats": selected, "tracks": tracks, "pages": pages}

    def inspect(self) -> dict:
        description = self.describe(self.args.input)
        return {"status": "inspected", "source": description["source"], "duration": description["duration_ms"] / 1000, "pages": description["pages"], "formats": description["formats"], "selected_formats": description["selected_formats"], "media": description.get("media"), "subtitles": [{key: track[key] for key in ("language", "source", "automatic")} for track in description["tracks"]], "warnings": self.warnings}

    def interval(self, description: dict) -> tuple[int, int, dict]:
        start = self.args.start_ms
        duration = self.args.duration_ms if self.args.duration_ms is not None else 120_000
        total = description["duration_ms"]
        if start >= total:
            raise ProviderError("RANGE_OUT_OF_BOUNDS", f"起点 {seconds(start)} 秒已达到或超过视频时长 {seconds(total)} 秒")
        requested_end = total if self.args.full else start + duration
        return start, min(requested_end, total), {"start_ms": start, "end_ms": requested_end, "full": self.args.full}

    def valid_cached(self, path: Path, expected: int, require_audio: bool = False, *, require_video: bool = True, compatible: bool = False) -> dict | None:
        if not path.is_file() or not path.stat().st_size:
            return None
        try:
            media = self.probe(path, require_video=require_video)
        except (ProviderError, ValueError, OSError):
            return None
        tolerance = max(0.5, 2 / (media["fps"] or 25))
        if abs(media["duration"] - expected / 1000) > tolerance or (require_audio and not media["has_audio"]):
            return None
        if compatible:
            # yuvj420p 是全范围 8 位 4:2:0 的旧名称，不应当作不兼容的像素格式。
            if media["video_codec"] != "h264" or media["pixel_format"] not in ("yuv420p", "yuvj420p") or "mp4" not in (media["container"] or "").split(","):
                return None
            if media["width"] % 2 or media["height"] % 2 or any(codec != "aac" for codec in media["audio_codecs"]):
                return None
            if self.args.max_height and media["height"] > self.args.max_height:
                return None
        return media

    def encoding_args(self, height: int | None) -> list[str]:
        scale = f"scale=-2:'trunc(min(ih,{height})/2)*2'" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        return ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-vf", scale]

    def convert(self, source: Path, output: Path, start: int, duration: int, *, height: int | None = None) -> None:
        command = [self.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", seconds(start), "-i", str(source), "-t", seconds(duration), "-map", "0:v:0", "-map", "0:a?"]
        command += self.encoding_args(height) + [str(output)]
        self.run(command)

    def subtitles(self, description: dict, start: int, end: int, directory: Path) -> list[dict]:
        results = []
        for index, track in enumerate(description["tracks"]):
            result = {key: track[key] for key in ("language", "source", "automatic")}
            destination = directory / f"video.{component(track['language'])}.{index}.srt"
            try:
                if "local_path" in track:
                    text = Path(track["local_path"]).read_text(encoding="utf-8-sig")
                else:
                    entry = track["entry"]
                    if entry.get("ext") != "srt":
                        raise ProviderError("UNSUPPORTED_SUBTITLE", "上游没有提供 SRT；该字幕未转换")
                    if "data" in entry:
                        text = entry["data"]
                    elif entry.get("url"):
                        req = request.Request(parse.urljoin("https://www.bilibili.com/", entry["url"]), headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
                        with self.opener.open(req, timeout=min(20, self.args.timeout)) as response:
                            text = response.read().decode("utf-8-sig")
                    else:
                        raise ProviderError("SUBTITLE_NOT_RETURNED", "上游没有返回字幕内容或地址")
                cropped, count = crop_srt(text, start, end)
                if cropped:
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, suffix=".part", delete=False) as stream:
                        staged = Path(stream.name)
                        stream.write(cropped)
                    os.replace(staged, destination)
                    result.update(status="available", path=str(destination), cue_count=count)
                else:
                    result.update(status="outside_range", path=None, cue_count=0)
            except (ProviderError, OSError, ValueError, UnicodeError) as exc:
                result.update(status="failed", path=None, message=safe_text(exc))
                self.warn(f"字幕 {track['language']} 未完成：{safe_text(exc)}")
            results.append(result)
        return results

    def fetch(self) -> dict:
        description = self.describe(self.args.input)
        start, end, requested_range = self.interval(description)
        source = description["source"]
        format_id = description["info"].get("format_id", "local")
        identity = component(source.get("bvid", source["id"]))
        part = component(source.get("cid") or source["id"])
        format_directory = self.cache / "media" / source["kind"] / identity / part / component(format_id)
        full_name = f"full-h{self.args.max_height}" if source["kind"] == "local" and self.args.max_height else "full"
        profile = "h264-aac-v1" if source["kind"] == "local" else REMOTE_CLIP_PROFILE
        directory = format_directory / (full_name if self.args.full else f"{start}-{end}-h{self.args.max_height or 'source'}-{profile}")
        directory.mkdir(parents=True, exist_ok=True)
        existing = load_json(directory / "result.json")
        require_audio = any(fmt.get("acodec") not in (None, "none") for fmt in description["selected_formats"]) if source["kind"] != "local" else description["media"]["has_audio"]
        video = Path(existing["video_path"]) if existing and existing.get("video_path") else directory / "video.mp4"
        resize_full = self.args.full and source["kind"] == "local" and self.args.max_height and description["media"]["height"] > self.args.max_height
        compatible = not self.args.full or bool(resize_full)
        media = self.valid_cached(video, end - start, require_audio, compatible=compatible) if existing else None
        cached = media is not None
        acquisition = "cached_output" if cached else "local_file"
        transformations = existing.get("transformations", []) if cached else []
        if not cached:
            with tempfile.TemporaryDirectory(prefix="media-", dir=self.temporary) as temporary:
                work = Path(temporary)
                input_path = Path(source["path"]) if source["kind"] == "local" else None
                input_start = start
                if input_path is None and not self.args.full:
                    full_result = load_json(format_directory / "full" / "result.json")
                    if full_result and self.valid_cached(Path(full_result["video_path"]), description["duration_ms"], require_audio):
                        input_path = Path(full_result["video_path"])
                        acquisition = "cached_full_source"
                if input_path is None:
                    acquisition = "downloaded_full" if self.args.full else "downloaded_range"
                    # 当次元信息含短期播放地址，仅放在临时目录，交给下载器复用并处理失效回退。
                    info_path = work / "source.info.json"
                    info_path.write_text(json.dumps(description["info"], ensure_ascii=False), encoding="utf-8")
                    command = self.ytdlp() + ["--quiet", "--no-simulate", "-f", format_id, "--ffmpeg-location", str(Path(self.tool("ffmpeg")).parent), "--merge-output-format", "mkv" if self.args.full else "mp4", "-o", str(work / "source.%(ext)s"), "--print", "after_move:%(filepath)j"]
                    if not self.args.full:
                        command += ["--download-sections", f"*{seconds(start)}-{seconds(end)}", "--force-keyframes-at-cuts"]
                        output_args = self.encoding_args(self.args.max_height) + ["-t", seconds(end - start), "-f", "mp4"]
                        command += ["--downloader-args", "ffmpeg_o:" + shlex.join(output_args)]
                    output = self.run(command + ["--load-info-json", str(info_path)], warnings=True)
                    paths = []
                    for line in output.splitlines():
                        try:
                            item = json.loads(line)
                            if isinstance(item, str) and Path(item).is_file():
                                paths.append(Path(item))
                        except ValueError:
                            continue
                    if len(paths) != 1:
                        raise ProviderError("DOWNLOAD_OUTPUT_MISSING", "下载器未返回唯一的已完成媒体文件")
                    input_path = paths[0].resolve()
                    if not input_path.is_relative_to(work.resolve()):
                        raise ProviderError("DOWNLOAD_OUTPUT_INVALID", "下载结果不在本次临时目录中")
                    input_start = 0
                if resize_full:
                    staged = work / "complete.mp4"
                    self.convert(input_path, staged, 0, end, height=self.args.max_height)
                    video = directory / "video.mp4"
                    transformations = [f"按显式高度限制对完整视频转码，输出高度不超过 {self.args.max_height}"]
                elif self.args.full:
                    staged = input_path
                    if source["kind"] == "local":
                        staged = work / ("complete" + input_path.suffix.lower())
                        shutil.copyfile(input_path, staged)
                    video = directory / ("video" + staged.suffix)
                    transformations = ["完整文件保留来源编码"]
                else:
                    staged = input_path
                    if acquisition != "downloaded_range":
                        staged = work / "clip.mp4"
                        self.convert(input_path, staged, input_start, end - start, height=self.args.max_height)
                    video = directory / "video.mp4"
                    transformations = ["按请求时间区间裁剪", "H.264/8 位 4:2:0 视频与 AAC 音轨，清除来源描述元数据"]
                    if acquisition == "downloaded_range":
                        transformations.append("区间下载与兼容编码在同一轮 FFmpeg 完成")
                    if self.args.max_height:
                        transformations.append(f"输出高度不超过 {self.args.max_height}，不放大来源")
                media = self.valid_cached(staged, end - start, require_audio, compatible=compatible)
                if media is None:
                    raise ProviderError("INVALID_MEDIA", "输出轨道、时长、编码或尺寸不符合请求")
                os.replace(staged, video)
        audio_path = None
        audio_media = None
        audio_status = "not_requested"
        if self.args.audio and media["has_audio"]:
            try:
                audio = directory / ("audio.wav" if self.args.audio_format == "wav" else "audio.mka")
                audio_media = self.valid_cached(audio, end - start, require_video=False)
                if audio_media is None:
                    with tempfile.TemporaryDirectory(prefix="audio-", dir=self.temporary) as temporary:
                        staged_audio = Path(temporary) / audio.name
                        codec = "pcm_s16le" if self.args.audio_format == "wav" else "copy"
                        self.run([self.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-map", "0:a:0", "-vn", "-c:a", codec, str(staged_audio)])
                        audio_media = self.valid_cached(staged_audio, end - start, require_video=False)
                        if audio_media is None:
                            raise ProviderError("INVALID_AUDIO", "独立音频轨道或时长无效")
                        os.replace(staged_audio, audio)
                audio_path, audio_status = str(audio), "available"
            except (ProviderError, OSError, ValueError) as exc:
                audio_status = "failed"
                self.warn("独立音频未完成：" + safe_text(exc))
        elif self.args.audio:
            audio_status = "not_present"
            self.warn("来源没有音轨，未生成独立音频")
        subtitle_results = self.subtitles(description, start, end, directory)
        offset = source.get("timeline_offset_ms", 0)
        result = {"status": "provided", "cached": cached, "acquisition": acquisition, "source": source, "requested_range": requested_range, "actual_range": {"start_ms": start, "end_ms": end, "source_start_ms": offset + start, "source_end_ms": offset + end}, "video_path": str(video), "audio_path": audio_path, "audio_status": audio_status, "audio_media": audio_media, "media": media, "selected_formats": description["selected_formats"], "subtitles": subtitle_results, "subtitle_status": "returned" if description["tracks"] else "not_returned", "transformations": transformations, "warnings": self.warnings}
        atomic_json(directory / "result.json", result)
        return result

    def frames(self) -> dict:
        path = Path(self.args.input).expanduser().resolve()
        if not path.is_file():
            raise ProviderError("LOCAL_FILE_REQUIRED", "frames 只接收已存在的本地视频文件")
        description = self.describe(str(path))
        start = self.args.start_ms
        end = description["duration_ms"] if self.args.duration_ms is None else min(description["duration_ms"], start + self.args.duration_ms)
        if start >= end:
            raise ProviderError("RANGE_OUT_OF_BOUNDS", "采样区间必须位于视频内并具有正时长")
        count = min(self.args.count, max(1, math.ceil((end - start) / 1000 * (description["media"]["fps"] or 25))))
        # 向上取整帧间隔，避免毫秒舍入把末次采样推到最后一帧之后。
        last = max(start, end - max(1, math.ceil(1000 / (description["media"]["fps"] or 25))))
        times = [round((start + last) / 2)] if count == 1 else [round(start + (last - start) * index / (count - 1)) for index in range(count)]
        directory = self.cache / "frames" / component(description["source"]["id"]) / f"{start}-{end}-n{count}"
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        for timestamp in times:
            destination = directory / f"frame-{timestamp:012d}ms.jpg"
            if not destination.is_file() or not destination.stat().st_size:
                with tempfile.TemporaryDirectory(prefix="frame-", dir=self.temporary) as temporary:
                    staged = Path(temporary) / "frame.jpg"
                    self.run([self.tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-ss", seconds(timestamp), "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=in_range=auto:out_range=full", "-pix_fmt", "yuvj420p", "-q:v", "2", "-update", "1", str(staged)])
                    if not staged.is_file() or not staged.stat().st_size:
                        raise ProviderError("FRAME_NOT_RETURNED", "解码器没有生成请求的采样画面")
                    os.replace(staged, destination)
            frames.append({"path": str(destination), "input_time_ms": timestamp, "source_time_ms": timestamp + description["source"].get("timeline_offset_ms", 0)})
        result = {"status": "frames_provided", "source": description["source"], "sampling": "uniform", "requested_count": self.args.count, "actual_count": len(frames), "frames": frames, "warnings": self.warnings}
        atomic_json(directory / "result.json", result)
        return result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        if "--json" in sys.argv:
            print(json.dumps({"status": "error", "code": "INVALID_ARGUMENT", "message": safe_text(message)}, ensure_ascii=False))
            raise SystemExit(2)
        super().error(message)


def parser() -> argparse.ArgumentParser:
    root = Parser(description="提供视频、音频、字幕或显式采样图片；不调用理解模型")
    root.add_argument("--version", action="version", version=VERSION)
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (("doctor", "查询依赖与必要编码器，不联网或安装工具"), ("inspect", "查询媒体事实与可用格式"), ("fetch", "获取或裁剪媒体"), ("frames", "显式采样本地视频画面")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--cache-dir", default=str(Path.cwd() / ".cache" / "video-provider"), help="缓存及工具目录")
        if name != "doctor":
            command.add_argument("input", help="B 站链接、BV/AV 编号或本地视频路径")
            command.add_argument("--cookies", help="显式提供 Netscape Cookie 文件，仅使用临时副本")
            command.add_argument("--page", type=int, help="覆盖链接中的分 P")
            command.add_argument("--max-height", type=int, help="最高输出高度，不放大来源")
        command.add_argument("--yt-dlp", dest="yt_dlp", help="yt-dlp 可执行文件或所在目录")
        command.add_argument("--ffmpeg", help="FFmpeg 可执行文件或所在目录")
        command.add_argument("--ffprobe", help="ffprobe 可执行文件或所在目录")
        command.add_argument("--timeout", type=float, default=600, help="每次外部工具调用的超时秒数")
        command.add_argument("--json", action="store_true", help="输出 JSON，进度和诊断不混入标准输出")
        if name in ("fetch", "frames"):
            command.add_argument("--start", default="0", help="起始秒数或 HH:MM:SS")
            command.add_argument("--duration", help="片段或采样区间时长，fetch 默认 120 秒")
        if name == "fetch":
            command.add_argument("--full", action="store_true", help="获取完整视频并保留来源编码")
            command.add_argument("--audio", action="store_true", help="额外提取保留编码的独立 MKA 音频")
            command.add_argument("--audio-format", choices=("source", "wav"), default="source", help="独立音频格式，wav 表示提取 PCM WAV，兼容原生音频工具")
        if name == "frames":
            command.add_argument("--count", type=int, default=12, help="均匀采样数量，默认 12")
    return root


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    if args.command == "fetch" and args.audio_format == "wav":
        args.audio = True
    provider = None
    try:
        height = getattr(args, "max_height", None)
        if not math.isfinite(args.timeout) or args.timeout <= 0 or (height is not None and height < 2) or (args.command == "frames" and args.count < 1):
            raise ProviderError("INVALID_ARGUMENT", "超时、采样数量必须为正数，最大高度至少为 2")
        if getattr(args, "page", None) is not None and args.page < 1:
            raise ProviderError("INVALID_PAGE", "分 P 必须从 1 开始")
        if args.command in ("fetch", "frames"):
            args.start_ms = milliseconds(args.start)
            args.duration_ms = milliseconds(args.duration) if args.duration is not None else None
            if args.duration_ms is not None and args.duration_ms <= 0:
                raise ProviderError("INVALID_TIME", "片段时长必须大于零")
            if args.command == "fetch" and args.full and (args.start_ms or args.duration_ms is not None):
                raise ProviderError("INVALID_ARGUMENT", "--full 不能与非零 --start 或 --duration 同时使用")
        provider = Provider(args)
        result = getattr(provider, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["status"] == "dependencies_missing" else 0
    except (ProviderError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "code": exc.code if isinstance(exc, ProviderError) else "LOCAL_OPERATION_FAILED", "message": safe_text(exc)}, ensure_ascii=False, indent=2))
        return 130 if isinstance(exc, ProviderError) and exc.code == "CANCELED" else 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "code": "CANCELED", "message": "操作已中止，未完成文件不会作为缓存使用"}, ensure_ascii=False))
        return 130
    finally:
        if provider:
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
