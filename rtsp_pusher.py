#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtsp_pusher.py — 把本地视频文件变成 RTSP 拉流源（测试用小工具）

原理：
  MediaMTX（开源 RTSP 服务器）在本机起一个 RTSP 服务，
  FFmpeg 把本地视频循环推流到 MediaMTX，
  分析服务器在同一网段内即可通过 rtsp://<本机IP>:8554/<路径> 拉流。

用法示例：
  # 1. 推单个/多个视频（自动命名为 video1 / video2 ...）
  python rtsp_pusher.py --videos D:\\test\\a.mp4 D:\\test\\b.mp4

  # 2. 推一个目录下的所有视频
  python rtsp_pusher.py --dir D:\\test\\videos

  # 3. 自定义端口和路径名（--paths 数量需与视频数量一致）
  python rtsp_pusher.py --dir D:\\test\\videos --port 8554 --paths cam1 cam2

  # 4. 指定本机已有的 mediamtx 可执行文件（跳过自动下载）
  python rtsp_pusher.py --dir D:\\test\\videos --mediamtx-path D:\\tools\\mediamtx\\mediamtx.exe

  # 5. 非循环播放（播完即停该路流）
  python rtsp_pusher.py --dir D:\\test\\videos --no-loop

  # 6. 只推视频流，丢弃音频（分析服务器一般只吃视频）
  python rtsp_pusher.py --dir D:\\test\\videos --no-audio

  # 7. 【推荐】高分辨率/高码率视频先离线预转码为 H.264（一次性，几分钟到几十分钟）
  #    预转码后推流走 copy 模式，CPU 几乎零占用，最稳定
  python rtsp_pusher.py --dir D:\\test\\videos --prepare
  python rtsp_pusher.py --dir D:\\test\\videos --prepare --scale 2992x2160

依赖：
  ffmpeg / ffprobe 必须在 PATH 中（或用 --ffmpeg-path 指定 ffmpeg 所在目录）。
  MediaMTX 会在首次运行时自动下载（也可用 --mediamtx-path 指定已有程序）。

Ctrl+C 停止所有推流。
"""

import argparse
import atexit
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from rtsp_sdp_proxy import PublisherBridge

# 输出被管道接管时（GUI 调用、重定向到文件）强制 UTF-8：
# Windows 下冻结程序默认按 GBK 输出，与读取端（UTF-8）不一致会全变乱码
try:
    if getattr(sys, "stdout", None) is not None and hasattr(sys.stdout, "reconfigure") \
            and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 常量

SUPPORTED_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v", ".h264", ".265", ".webm"}
MEDIAMTX_VERSION = "v1.11.3"
# 打包成 exe（PyInstaller）后 __file__ 指向临时解压目录，
# 数据（mediamtx/、ffmpeg 日志等）必须放在 exe 所在目录
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
DOWNLOAD_DIR = os.path.join(APP_DIR, "mediamtx")

DOWNLOAD_URLS = {
    ("windows", "amd64"): "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_windows_amd64.zip",
    ("windows", "arm64"): "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_windows_arm64.zip",
    ("linux", "amd64"):   "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_linux_amd64.tar.gz",
    ("linux", "arm64"):   "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_linux_arm64v8.tar.gz",
    ("darwin", "amd64"):  "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_macos_amd64.zip",
    ("darwin", "arm64"):  "https://github.com/bluenviron/mediamtx/releases/download/{v}/mediamtx_{v}_macos_arm64.zip",
}


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 工具函数

def find_tool(name: str, extra_dir: str = "") -> str:
    """在 PATH 和 extra_dir 中查找可执行文件，找不到返回空串。"""
    if extra_dir:
        cand = os.path.join(extra_dir, name + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(cand):
            return cand
    path = shutil.which(name)
    return path or ""


def get_lan_ip() -> str:
    """探测本机局域网 IP（UDP trick，不真正发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _port_available(port: int, udp: bool = False) -> bool:
    """检查一个 TCP/UDP 端口是否能被本工具绑定。"""
    family = socket.AF_INET
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    with socket.socket(family, kind) as s:
        try:
            # MediaMTX 配置使用 :port，会绑定所有 IPv4 地址；这里也必须
            # 用 0.0.0.0 检测，否则某个 LAN 地址上的监听会漏检。
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def choose_rtsp_ports(requested: int) -> tuple[int, int]:
    """为 MediaMTX 选择一组完整可用的 RTSP + RTP/RTCP 端口。

    MediaMTX 不只监听 RTSP TCP 端口，还会绑定 RTP/RTCP UDP 端口；只检查
    8554 会漏掉后一类冲突，最终导致“RTSP 端口没占用但服务仍启动失败”。
    """
    if not 1 <= requested <= 65535:
        raise ValueError("RTSP 端口必须在 1~65535 之间")
    candidate = requested
    while candidate <= 65535 - 52:
        rtp = candidate + 50
        if (candidate % 2 == 0 and _port_available(candidate)
                and _port_available(rtp, udp=True)
                and _port_available(rtp + 1, udp=True)):
            return candidate, rtp
        candidate += 1
    raise OSError("找不到可用的 RTSP/RTP/RTCP 端口组合")


def collect_videos(args) -> list:
    """收集视频文件列表。"""
    videos = []
    if args.videos:
        for v in args.videos:
            if os.path.isfile(v):
                videos.append(os.path.abspath(v))
            else:
                log(f"警告：文件不存在，已跳过 -> {v}")
    if args.dir:
        if not os.path.isdir(args.dir):
            log(f"错误：目录不存在 -> {args.dir}")
            sys.exit(1)
        for name in sorted(os.listdir(args.dir)):
            f = os.path.join(args.dir, name)
            if os.path.isfile(f) and os.path.splitext(name)[1].lower() in SUPPORTED_EXT:
                videos.append(os.path.abspath(f))
    # 去重保序
    seen = set()
    uniq = []
    for v in videos:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def probe_codec(ffprobe: str, video: str) -> dict:
    """用 ffprobe 查视频编码信息（含码率、帧率、时长，供云端推流决策用）。"""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "stream=index,codec_type,codec_name,width,height,bit_rate,avg_frame_rate",
             "-show_entries", "format=duration,bit_rate",
             "-of", "json", video],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(out.stdout or "{}")
        streams = data.get("streams") or []
        info = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        info["audio_codec_name"] = audio.get("codec_name", "")
        fmt = data.get("format") or {}
        if not info.get("bit_rate"):
            info["bit_rate"] = fmt.get("bit_rate")
        info["duration"] = fmt.get("duration")
        return info
    except Exception as e:
        log(f"ffprobe 解析失败 {os.path.basename(video)}: {e}")
        return {}


def download_mediamtx() -> str:
    """自动下载 MediaMTX，返回可执行文件路径。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        machine = "amd64"
    elif machine in ("aarch64", "arm64"):
        machine = "arm64"
    key = (system, machine)
    if key not in DOWNLOAD_URLS:
        log(f"不支持的平台 {system}/{machine}，请手动下载 MediaMTX 并用 --mediamtx-path 指定")
        sys.exit(1)

    url = DOWNLOAD_URLS[key].format(v=MEDIAMTX_VERSION)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    archive = os.path.join(DOWNLOAD_DIR, url.rsplit("/", 1)[-1])

    # 已有解压好的程序就直接用，避免运行中的进程锁定文件导致覆盖失败
    exe_name = "mediamtx.exe" if system == "windows" else "mediamtx"
    exe_path = os.path.join(DOWNLOAD_DIR, exe_name)
    if os.path.isfile(exe_path):
        return exe_path

    if not os.path.isfile(archive):
        log(f"正在下载 MediaMTX {MEDIAMTX_VERSION} ...")
        log(f"  {url}")
        try:
            urllib.request.urlretrieve(url, archive)
        except Exception as e:
            log(f"下载失败：{e}")
            log("请手动下载后解压，并用 --mediamtx-path 指定可执行文件路径")
            sys.exit(1)

    log("正在解压 ...")
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(DOWNLOAD_DIR)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            t.extractall(DOWNLOAD_DIR)

    exe = "mediamtx.exe" if system == "windows" else "mediamtx"
    for root, _, files in os.walk(DOWNLOAD_DIR):
        if exe in files:
            full = os.path.join(root, exe)
            if system != "windows":
                os.chmod(full, 0o755)
            return full
    log("解压后未找到 mediamtx 可执行文件")
    sys.exit(1)


# ---------------------------------------------------------------- 预转码

def prepare_videos(ffmpeg, stream_params, args):
    """离线预转码：把非 H.264 的视频一次性转成 H.264（不推流）。"""
    todo = []
    for video, need_transcode in stream_params:
        if not need_transcode:
            log(f"跳过（已是 H.264）: {os.path.basename(video)}")
            continue
        root, ext = os.path.splitext(video)
        out = root + "_h264" + ".mp4"
        todo.append((video, out))

    if not todo:
        log("所有视频都已是 H.264，无需预转码")
        return

    for i, (video, out) in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] 离线转码（保持原始帧率与分辨率，只换编码格式）：")
        log(f"  {os.path.basename(video)}")
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", video]
        # 不加 fps 滤镜：时间戳原样保留，帧率与源视频一致
        # 仅在用户显式要求缩放时加 scale 滤镜
        if args.scale:
            cmd += ["-vf", f"scale={args.scale.replace(':', 'x').replace('X', 'x')}"]
        # 像素格式规范化用 -pix_fmt 参数（避免 yuvj420p 触发老版 ffmpeg 的滤镜问题）
        cmd += ["-pix_fmt", "yuv420p"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast",
                "-profile:v", "high", "-g", str(args.fps * 2)]
        if args.no_audio:
            cmd += ["-an"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        cmd += [out]

        t0 = time.time()
        ret = subprocess.run(cmd).returncode
        if ret != 0:
            log(f"  转码失败（退出码 {ret}），请把上面 FFmpeg 报错信息发给助手分析")
            sys.exit(1)
        log(f"  完成 -> {out}  耗时 {time.time() - t0:.0f} 秒")

    log("\n预转码全部完成！现在用正常命令推流（会自动走 copy 模式）：")
    vids = " ".join(f'"{out}"' for _, out in todo)
    others = " ".join(f'"{v}"' for v, t in stream_params if not t)
    all_vids = " ".join(x for x in [vids, others] if x)
    log(f"  python {os.path.basename(sys.argv[0])} --videos {all_vids}")


# ---------------------------------------------------------------- 云端转发

def mask_url(url: str) -> str:
    """日志里隐藏鉴权串，避免泄露（auth_key=xxx / token=xxx）。"""
    return re.sub(r"((?:auth_key|token|key|secret)=)[^&\s]+", r"\1***", url, flags=re.I)


def url_scheme(url: str) -> str:
    m = re.match(r"^\s*([a-zA-Z0-9+]+)://", url)
    return m.group(1).lower() if m else ""


def parse_offset(value: str) -> float:
    """接受秒、分:秒、时:分:秒。"""
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("时间格式应为秒、分:秒或时:分:秒")
    try:
        numbers = [float(x) for x in parts]
    except ValueError as exc:
        raise ValueError("时间必须是数字") from exc
    if any(x < 0 for x in numbers) or any(x >= 60 for x in numbers[1:]):
        raise ValueError("时间不能为负，分/秒部分必须小于 60")
    return sum(n * 60 ** i for i, n in enumerate(reversed(numbers)))


def net_precheck(url: str) -> bool:
    """拉流前的网络预检：DNS 解析 + 端口连通性。
    能区分"地址填错"和"DNS 把域名指向了不可达节点"这两种完全不同的故障。"""
    try:
        m = re.match(r"^\s*([a-zA-Z0-9+]+)://(?:[^@/]*@)?([^:/?]+)(?::(\d+))?", url)
        if not m:
            return True
        scheme, host, port = m.group(1).lower(), m.group(2), m.group(3)
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):     # 已是 IP，不用解析
            ip_list = [host]
        else:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            ip_list = sorted({i[4][0] for i in infos})
        default_ports = {"rtmp": 1935, "rtmps": 443, "rtsp": 554, "rtsps": 322,
                         "http": 80, "https": 443, "srt": 1105}
        port = int(port) if port else default_ports.get(scheme, 1935)

        log(f"域名解析: {host} -> {', '.join(ip_list)}")
        good = []
        for ip in ip_list:
            with socket.socket() as s:
                s.settimeout(5)
                try:
                    s.connect((ip, port))
                    good.append(ip)
                except OSError:
                    pass
        if good:
            log(f"网络连通: {', '.join(good)}:{port} 可连接 ✓")
            return True

        # 解析到了，但一个都连不上 —— 典型的 DNS/CDN 节点问题
        log(f"网络连通: 解析到的节点 {', '.join(ip_list)} 全部无法连接 {port} 端口 ✗")
        log("")
        log("诊断：这通常不是地址填错，而是 DNS 把这个域名指向了不可用的节点。")
        log("      同一域名在不同 DNS 下会解析到不同 IP，本机 DNS 给的这个节点可能已故障。")
        log("      处理方法（任选其一）：")
        log("        1) 把电脑/路由器的 DNS 改成 223.5.5.5 或 119.29.29.29，再重试")
        log("        2) 临时改 hosts（需管理员）：在 C:\\Windows\\System32\\drivers\\etc\\hosts 里加")
        log(f"           <可用IP>  {host}")
        log("        3) 找云端运维要一个可用的固定 IP")
        log("      可用 IP 的找法：用其它 DNS 查同一域名，例如")
        log(f'           nslookup {host} 223.5.5.5')
        return False
    except Exception as e:
        log(f"网络预检跳过（{type(e).__name__}: {e}）")
        return True


def probe_stream(ffprobe: str, url: str) -> int:
    """检测拉流地址是否可用，打印编码信息。返回退出码。"""
    url = url.strip()
    scheme = url_scheme(url)
    log(f"检测拉流地址: {mask_url(url)}")
    if scheme not in ("rtsp", "rtsps", "rtmp", "rtmps", "http", "https", "srt", "udp"):
        log(f"错误：不支持的地址格式（{scheme or '未知'}），"
            "支持 rtsp/rtmp/flv/m3u8/srt/http")
        return 1

    # TCP/UDP 先探一次：连都连不上就不用等 ffprobe 超时了
    if scheme != "srt" and not net_precheck(url):
        return 1

    cmd = [ffprobe, "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate",
           "-show_entries", "format=duration,bit_rate,format_name", "-of", "json"]
    if scheme in ("rtsp", "rtsps"):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += [url]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        log("结果：连接超时（地址不可达，或 25 秒内没有数据）")
        return 1
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        err = (r.stderr or "").strip().splitlines()
        log("结果：无法拉流")
        for line in err[-3:]:
            log("  " + line)
        log("  常见原因：地址过期（auth_key 有时效）、流未在推、网络/防火墙不通")
        return 1
    try:
        data = json.loads(out)
    except Exception:
        log("结果：拉到了数据，但解析失败")
        return 1
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    if not streams:
        log("结果：地址可连接，但没有音视频流（可能推流端还没开始推）")
        return 1
    log("结果：拉流成功 ✅")
    for s in streams:
        if s.get("codec_type") == "video":
            fps = s.get("avg_frame_rate", "")
            if "/" in str(fps):
                try:
                    a, b = str(fps).split("/")
                    fps = round(float(a) / float(b), 2) if float(b) else fps
                except Exception:
                    pass
            log(f"  视频: {s.get('codec_name')}  {s.get('width')}x{s.get('height')}  {fps}fps")
        elif s.get("codec_type") == "audio":
            log(f"  音频: {s.get('codec_name')}")
    if fmt.get("format_name"):
        log(f"  封装: {fmt.get('format_name')}  时长: {fmt.get('duration', '未知')}")
    return 0


def run_push(ffmpeg: str, ffprobe: str, videos: list, args) -> int:
    """把本地视频推送到云端流媒体服务（RTMP/SRT）。

    默认与原样直推（--copy）一致：视频一律 -c:v copy，
    分辨率、帧率、编码格式全部保持原样，ffmpeg 只做"读文件 → 按原速发送"，
    CPU 占用极低。只有显式加 --transcode 才会重新编码。
    """
    url = args.push_url.strip()
    scheme = url_scheme(url)
    if scheme not in ("rtmp", "rtmps", "srt"):
        log(f"错误：推流地址格式不支持（{scheme or '未知'}）")
        log("      RTMP 推流填 rtmp://…，SRT 推流填 srt://…")
        return 1
    if len(videos) > 1:
        log("提示：云端推流一次只推一路，已只使用第一个视频。")
        videos = videos[:1]

    video = videos[0]
    info = probe_codec(ffprobe, video)
    codec = info.get("codec_name", "")
    audio_codec = info.get("audio_codec_name", "")
    w = info.get("width", "?")
    h = info.get("height", "?")
    try:
        kbps = int(info.get("bit_rate") or 0) // 1000
    except Exception:
        kbps = 0
    fps = info.get("avg_frame_rate") or ""
    if "/" in str(fps):
        try:
            a, b = str(fps).split("/")
            fps = round(float(a) / float(b), 2) if float(b) else fps
        except Exception:
            pass
    log(f"视频: {os.path.basename(video)}  编码={codec}  {w}x{h}  {fps}fps"
        + (f"  {kbps/1000:.1f} Mbps" if kbps else ""))
    log(f"音频编码: {audio_codec or '无/未知'}")

    if kbps:
        log(f"带宽要求: 上行需稳定 {kbps/1000:.0f} Mbps 以上（当前素材码率），"
            "家宽上行不足会出现卡顿或云端断开。")

    # 实测：主流云转发 RTMP 通道在 30 Mbps 左右可用，50 Mbps 会被断开；SRT 能扛住 8K/50Mbps
    if scheme in ("rtmp", "rtmps") and kbps > 35000:
        log("")
        log(f"⚠ 强烈建议改用 SRT：当前素材 {kbps/1000:.0f} Mbps，"
            "RTMP 通道通常撑不住这个码率（实测 30 Mbps 可推、50 Mbps 会被云端断开）。")
        log("  云端控制台里找 SRT 推流地址（srt://…m=publish），SRT 能原样推 8K，同样 copy 不转码。")
    # 超出 4K 的大画幅素材，即使码率不高，RTMP 也容易被云端在 1 分钟左右踢断（实测 8K/21Mbps 必断）
    try:
        if scheme in ("rtmp", "rtmps") and w * h > 4096 * 2400:
            log("")
            log(f"⚠ 当前是 {w}x{h}（超过 4K）素材：RTMP 通道实测会在推流约 60 秒后被云端断开，")
            log("  而同样素材走 SRT 可长时间稳定推流。建议在云端控制台取 SRT 推流地址。")
    except Exception:
        pass

    # RTMP 播放链路的兼容性要求比 RTSP 严格：多数平台只会把 H.264/AAC
    # 识别成可播放流。之前无条件 copy H.265/非 AAC，常见表现就是“推上去了但拉不到”。
    rtmp_compatible = scheme not in ("rtmp", "rtmps") or codec.lower() == "h264"
    if scheme in ("rtmp", "rtmps") and not rtmp_compatible:
        log(f"输入视频是 {codec or '未知'}，保持原编码；RTMP 服务端和播放器需支持该编码。")
    if scheme in ("rtmp", "rtmps") and codec.lower() == "h264":
        log("RTMP 视频编码兼容：H.264")

    fmt_args = ["-f", "flv"] if scheme in ("rtmp", "rtmps") else ["-f", "mpegts"]

    # SRT 抗弱网：附加 200ms 缓冲（丢包靠重传补，比走 TCP 的 RTMP 稳得多）
    if scheme == "srt" and "latency=" not in url:
        url = url + ("&" if "?" in url else "?") + "latency=200000"
        log("SRT 已自动附加 latency=200ms（提升弱网抗抖动能力）")

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info"]
    if not args.no_loop and not args.clip_duration:
        cmd += ["-stream_loop", "-1"]
    if args.clip_start:
        cmd += ["-ss", str(args.clip_start)]
    cmd += ["-re", "-i", video]
    if args.clip_duration:
        cmd += ["-t", str(args.clip_duration)]

    transcode_video = args.transcode
    if transcode_video:
        # 显式要求转码才转；不加 --scale 就保持原分辨率
        log("注意: 已启用 --transcode（重新编码），高分辨率下速度可能跟不上实时。")
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-g", str(args.fps * 2)]
        if args.scale:
            cmd += ["-vf", f"scale={args.scale.replace('x', ':')}"]
    else:
        cmd += ["-c:v", "copy"]

    if args.no_audio:
        cmd += ["-an"]
    elif args.audio_aac or scheme == "srt" or scheme in ("rtmp", "rtmps"):
        # MPEG-TS（SRT）里的 AAC 必须是 ADTS 格式，直接 copy 会报
        # "AAC bitstream not in ADTS format and extradata missing"，所以 SRT 一律重编码音频
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c:a", "copy"]

    cmd += fmt_args
    if scheme in ("rtmp", "rtmps"):
        cmd += ["-flvflags", "no_duration_filesize"]
    cmd += [url]

    log("")
    print("=" * 62, flush=True)
    log("开始推流到云端：")
    print(f"  -> {mask_url(url)}", flush=True)
    print("=" * 62, flush=True)
    print("推流成功后，用云端控制台给出的拉流地址（FLV / M3U8 / RTMP / SRT）", flush=True)
    print("填到分析服务器上即可。可用「检测拉流地址」确认云端是否已收到流。", flush=True)
    print("断线会自动重连（Ctrl+C 停止）\n", flush=True)

    log_file = os.path.join(APP_DIR, "ffmpeg_push.log")
    try:
        open(log_file, "w").close()      # 每次启动清空，之后各次尝试追加，保留完整现场
    except OSError:
        pass

    def last_stats(text):
        """从 ffmpeg 进度里取最后一次 (内容秒数, 平均码率)。"""
        m = re.findall(r"time=(\d+):(\d+):(\d+)\.\d+.*?bitrate=\s*([\d.]+)kbits/s", text)
        if not m:
            return None, None
        hh, mm, ss, br = m[-1]
        return int(hh) * 3600 + int(mm) * 60 + int(ss), float(br)

    # ---- 推流主循环：断线自动重连。
    # 云端/网络偶发踢连接（推几十秒后被断开）时，重连能保证分析服务器那边持续有流。
    attempt = 0
    rc = 0
    while True:
        attempt += 1
        if attempt > 1:
            log(f"【第 {attempt} 次推流】重新连接云端 …")
        logf = open(log_file, "a", encoding="utf-8")
        logf.write(f"\n===== 第 {attempt} 次推流  {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        logf.flush()
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        try:
            proc.wait()
        except KeyboardInterrupt:
            log("正在停止 ...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logf.close()
            log("已停止推流")
            return 0
        logf.close()
        rc = proc.returncode
        if rc in (0, None):
            if args.clip_duration and not args.no_loop:
                log("选定片段播完，重新从片段起点推流")
                continue
            log("推流正常结束（视频播完且未循环）")
            return 0

        # ---- 失败诊断 + 重连判定
        detail = ""
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                detail = f.read()
        except Exception:
            pass
        low = detail.lower()

        secs, br = last_stats(detail)
        if secs:
            log(f"本次已推送 {secs} 秒（平均 {br/1000:.1f} Mbps）后被断开。")

        # auth/地址类错误不重试（重试也没用）
        if "401" in detail or "403" in detail or "unauthorized" in low:
            log("推流失败：鉴权错误 —— 推流地址/auth_key 无效或已过期，请在云端重新生成。")
            return 1
        if "connection refused" in low or "no route to host" in low:
            log("推流失败：连不上云端 —— 检查网络、防火墙或推流地址域名。")
            return 1

        # 其余（对端断开/带宽抖动/超时）一律自动重连
        last = [l for l in detail.strip().splitlines() if l.strip()][-1:] 
        log(f"云端断开了连接（第 {attempt} 次）。最近一行输出：{last[0][:120] if last else '无'}")
        if args.no_reconnect:
            log("已启用 --no-reconnect，停止推流。详情见 ffmpeg_push.log")
            return 1
        wait = min(2 + attempt, 10)
        log(f"{wait} 秒后自动重连（保持服务器端有流）…")
        time.sleep(wait)



# ---------------------------------------------------------------- 主逻辑

def main():
    parser = argparse.ArgumentParser(
        description="把本地视频文件变成 RTSP 拉流源（MediaMTX + FFmpeg）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例")[1] if "用法示例" in __doc__ else "",
    )
    parser.add_argument("--videos", nargs="*", default=[], help="视频文件列表（空格分隔）")
    parser.add_argument("--dir", default="", help="视频目录（会扫描目录下所有支持格式的视频）")
    parser.add_argument("--port", type=int, default=8554, help="RTSP 服务端口，默认 8554")
    parser.add_argument("--paths", nargs="*", default=None,
                        help="每路流的名字，默认 video1/video2/...（数量需与视频一致）")
    parser.add_argument("--rtsp-user", default="", help="RTSP 鉴权用户名（可选）")
    parser.add_argument("--rtsp-pass", default="", help="RTSP 鉴权密码（可选）")
    parser.add_argument("--no-loop", action="store_true", help="不循环播放，播完即停")
    parser.add_argument("--start", default="", help="片段起点：秒或 分:秒")
    parser.add_argument("--end", default="", help="片段终点：秒或 分:秒；必须晚于起点")
    parser.add_argument("--no-audio", action="store_true", help="丢弃音频，只推视频流")
    parser.add_argument("--audio-aac", action="store_true",
                        help="音频重编码为 AAC（默认原样 copy；某些云端对音频格式挑剔时用）")
    parser.add_argument("--no-reconnect", action="store_true",
                        help="云端推流断线后不自动重连（默认会自动重连，保证服务器端持续有流）")
    parser.add_argument("--fps", type=int, default=25, help="转码时的目标帧率，默认 25")
    parser.add_argument("--transcode", action="store_true",
                        help="强制转码为 H.264（默认：检测到非 H.264 编码时自动转码）")
    parser.add_argument("--copy", action="store_true",
                        help="原样直推模式：不转码、不改帧率、不改分辨率，编码码流原样转发"
                             "（要求服务器支持对应编码，如 H.265 over RTSP）")
    parser.add_argument("--prepare", action="store_true",
                        help="离线预转码模式：把非 H.264 视频一次性转成 H.264（不推流）。"
                             "转码完成后正常推流会自动走 copy 模式。推荐高分辨率视频使用")
    parser.add_argument("--scale", default="",
                        help="预转码时缩放分辨率，格式如 2992x2160（默认保持原分辨率）")
    parser.add_argument("--ffmpeg-path", default="", help="ffmpeg/ffprobe 所在目录（不在 PATH 时指定）")
    parser.add_argument("--mediamtx-path", default="", help="已有 mediamtx 可执行文件路径（跳过自动下载）")
    # ---- 云端转发相关（异地测试用）
    parser.add_argument("--push-url", default="",
                        help="推流到外部流媒体服务（云端中转）。支持 rtmp:// rtmps:// srt:// 地址，"
                             "如 rtmp://push.xxx.cn/live/stream?auth_key=xxx")
    parser.add_argument("--relay-url", default="",
                        help="从外部地址拉流并转成本地 RTSP（支持 rtsp/rtmp/flv/m3u8/srt），"
                             "用于公司服务器只能拉 RTSP 的场景")
    parser.add_argument("--probe-url", default="",
                        help="仅检测某个拉流地址是否可用（支持 rtsp/rtmp/flv/m3u8/srt），不推流")
    args = parser.parse_args()
    if args.relay_url and (args.start or args.end):
        parser.error("实时云端转发不支持视频片段时间")
    try:
        args.clip_start = parse_offset(args.start) if args.start else 0.0
        clip_end = parse_offset(args.end) if args.end else None
        if clip_end is not None and clip_end <= args.clip_start:
            raise ValueError("结束时间必须晚于开始时间")
        args.clip_duration = clip_end - args.clip_start if clip_end is not None else None
    except ValueError as exc:
        parser.error(str(exc))

    # ---- 0. 定位 ffmpeg / ffprobe（提前，各模式都要用）
    bundled_ffmpeg_dir = os.path.join(APP_DIR, "ffmpeg")
    ff_dir = args.ffmpeg_path or bundled_ffmpeg_dir
    ffmpeg = find_tool("ffmpeg", ff_dir)
    ffprobe = find_tool("ffprobe", ff_dir)
    if not ffmpeg or not ffprobe:
        log("错误：未找到 ffmpeg / ffprobe，请先安装并加入 PATH，")
        log("      或用 --ffmpeg-path 指定所在目录。下载地址：https://ffmpeg.org/download.html")
        sys.exit(1)
    log(f"ffmpeg: {ffmpeg}")

    # ---- 0.1 仅检测拉流地址
    if args.probe_url:
        sys.exit(probe_stream(ffprobe, args.probe_url))

    # ---- 0.2 云端拉流 → 转本地 RTSP（异地测试桥接）
    if args.relay_url:
        args.relay_url = args.relay_url.strip()
        log(f"拉流源: {mask_url(args.relay_url)}")

    # ---- 1. 收集视频（拉流转发模式不需要本地视频）
    if args.relay_url:
        videos = [args.relay_url]
    else:
        videos = collect_videos(args)
        if not videos:
            log("错误：没有找到任何视频文件。用 --videos 指定文件，或 --dir 指定目录。")
            sys.exit(1)

    # ---- 1.5 推流到云端（RTMP/SRT）模式：不需要本地 RTSP 服务
    if args.push_url:
        sys.exit(run_push(ffmpeg, ffprobe, videos, args))

    paths = args.paths or [f"video{i + 1}" for i in range(len(videos))]
    if len(paths) != len(videos):
        log(f"错误：--paths 数量({len(paths)}) 与视频数量({len(videos)}) 不一致")
        sys.exit(1)

    # ---- 3. 检测编码，决定转码参数
    stream_params = []
    for v in videos:
        if args.relay_url:
            # 网络流不做本地探测（会阻塞且无意义），一律原样转发
            log("模式: 云端拉流 -> 本地 RTSP 转发（不转码）")
            stream_params.append((v, False))
            continue
        info = probe_codec(ffprobe, v)
        codec = info.get("codec_name", "")
        res = f"{info.get('width', '?')}x{info.get('height', '?')}"
        # copy 必须保留原视频编码，包括 H.265，不得静默转码。
        if args.copy:
            need_transcode = False
            log(f"视频: {os.path.basename(v)}  编码={codec or '未知'}  分辨率={res}"
                f"  -> 原样直推（copy，编码/帧率/分辨率均不变）")
        else:
            need_transcode = args.transcode or codec.lower() != "h264"
            log(f"视频: {os.path.basename(v)}  编码={codec or '未知'}  分辨率={res}"
                + ("  -> 自动转码为 H.264（RTSP 兼容性要求）" if need_transcode
                   else "  -> 直接复用编码（copy）"))
        stream_params.append((v, need_transcode))

    # ---- 3.5 离线预转码模式（--prepare）：不推流，只转码
    if args.prepare:
        prepare_videos(ffmpeg, stream_params, args)
        return

    # ---- 4. 准备 MediaMTX
    mediamtx = args.mediamtx_path
    if mediamtx and os.path.isfile(mediamtx):
        log(f"mediamtx: {mediamtx}")
    elif mediamtx:
        log(f"错误：--mediamtx-path 指定的文件不存在: {mediamtx}")
        sys.exit(1)
    else:
        mediamtx = download_mediamtx()
        log(f"mediamtx: {mediamtx}")

    # 生成 MediaMTX 配置（只开 RTSP，其余协议关掉，避免与已有实例抢 1935/8888 端口）
    # RTSP 主端口、RTP、RTCP 必须作为一组检查，否则仍会出现“端口占用”。
    requested_port = args.port
    try:
        args.port, rtp_port = choose_rtsp_ports(requested_port)
    except (ValueError, OSError) as e:
        log(f"错误：无法为 RTSP 找到可用端口：{e}")
        sys.exit(1)
    if args.port != requested_port:
        log(f"端口 {requested_port} 不可用，已自动切换到 {args.port}（RTP {rtp_port}/{rtp_port + 1}）")
    conf_lines = [
        "rtsp: yes",
        f"rtspAddress: :{args.port}",
        # RTP/RTCP 端口随 RTSP 端口偏移，保证同时开多个实例不冲突
        f"rtpAddress: :{rtp_port}",
        f"rtcpAddress: :{rtp_port + 1}",
        "rtmp: no",
        "hls: no",
        "webrtc: no",
        "srt: no",
        "api: no",
    ]
    if args.rtsp_user and args.rtsp_pass:
        conf_lines += [
            "pathDefaults:",
            "  publishUser: " + args.rtsp_user,
            "  publishPass: " + args.rtsp_pass,
            "  readUser: " + args.rtsp_user,
            "  readPass: " + args.rtsp_pass,
        ]
    conf_lines += [f"paths:\n  {p}:" for p in paths]
    # 配置文件按端口命名：同时开多个窗口/多路互不覆盖（原来固定文件名会互相冲掉）
    conf_file = os.path.join(DOWNLOAD_DIR, f"pusher_rtsp_{args.port}.yml")
    with open(conf_file, "w", encoding="utf-8") as f:
        f.write("\n".join(conf_lines) + "\n")

    # ---- 5. 启动 MediaMTX
    log(f"启动 RTSP 服务 :{args.port} ...")
    # MediaMTX 的输出必须落盘：它绑不上端口时报的错误是排查 400 的关键线索
    mtx_log = os.path.join(DOWNLOAD_DIR, f"mediamtx_{args.port}.log")
    mtxf = open(mtx_log, "w", encoding="utf-8", errors="replace")
    mtx = subprocess.Popen([mediamtx, conf_file], stdout=mtxf, stderr=subprocess.STDOUT)

    def mtx_tail(n=8):
        try:
            mtxf.flush()
            with open(mtx_log, encoding="utf-8", errors="replace") as f:
                lines = [l for l in f.read().splitlines() if l.strip()]
            return lines[-n:]
        except Exception:
            return []

    # 就绪判定：必须看到"自己这个实例"真正监听成功，不能只靠端口能连上
    # （端口被别的 mediamtx 占着时，能连上但推流会 400 Bad Request）
    ready = False
    for _ in range(40):
        if mtx.poll() is not None:
            break
        time.sleep(0.5)
        try:
            with open(mtx_log, encoding="utf-8", errors="replace") as f:
                if f"listener opened on :{args.port}" in f.read():
                    ready = True
                    break
        except Exception:
            pass

    if not ready:
        log("错误：RTSP 服务启动失败，MediaMTX 输出如下：")
        for line in mtx_tail():
            log("  " + line)
        log("")
        log("常见原因与处理：")
        log("  · 端口被别的 mediamtx / 另一个工具窗口占用 → 关掉多余窗口，或换端口")
        log("  · 有残留进程没退干净 → 任务管理器结束 mediamtx.exe、ffmpeg.exe 后重试")
        try:
            mtx.kill()
            mtxf.close()
        except Exception:
            pass
        sys.exit(1)

    # ---- 6. 启动 FFmpeg 推流
    procs = []
    publisher_bridge = PublisherBridge(args.port)
    atexit.register(publisher_bridge.close)
    lan_ip = get_lan_ip()

    def push_cmd(index):
        """构造第 index 路的 ffmpeg 命令。"""
        video, need_transcode = stream_params[index]
        name = paths[index]
        dst = f"rtsp://127.0.0.1:{publisher_bridge.port}/{name}"
        is_net_src = args.relay_url != ""      # 输入是网络流（拉流转发模式）
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
        if is_net_src:
            # 网络流：不要 -re/-stream_loop（源头本身就是实时的）
            if url_scheme(video) in ("rtsp", "rtsps"):
                cmd += ["-rtsp_transport", "tcp"]
            cmd += ["-i", video]
        else:
            if not args.no_loop and not args.clip_duration:
                cmd += ["-stream_loop", "-1"]      # 无限循环
            if args.clip_start:
                cmd += ["-ss", str(args.clip_start)]
            cmd += ["-re", "-i", video]            # -re 按原始帧率推
            if args.clip_duration:
                cmd += ["-t", str(args.clip_duration)]
        if need_transcode:
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-profile:v", "baseline", "-g", str(args.fps * 2),
                "-vf", f"fps={args.fps},format=yuv420p",
            ]
        else:
            cmd += ["-c:v", "copy"]
        if args.no_audio:
            cmd += ["-an"]
        elif args.copy or is_net_src:
            # 注意：AAC 音频经 copy 直接转发到 RTSP 会报
            # "AAC with no global headers is currently not supported"，
            # 导致整个推流会话被服务端拒绝。音频重编码开销极小，不影响视频原样直推。
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100"]
        cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", dst]
        return cmd

    def spawn(index):
        """启动第 index 路推流进程（不记录到 procs），ffmpeg 报错写入日志文件。"""
        log_file = os.path.join(APP_DIR, f"ffmpeg_{paths[index]}.log")
        logf = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(push_cmd(index),
                                stdout=logf, stderr=subprocess.STDOUT)
        proc._logf = logf   # 保存句柄，防止被垃圾回收提前关闭
        return proc

    # 注意：这些 print 必须带 flush=True。输出接管道（GUI 调用）时 Python 是块缓冲，
    # 不 flush 的话地址要等缓冲区攒满才出现，界面会一直显示空白。
    print("\n" + "=" * 62, flush=True)
    if args.relay_url:
        log("拉流转发已启动！公司服务器用下面的地址拉流（本地 RTSP）：")
    else:
        log("推流已启动！在分析服务器上使用以下地址拉流：")
    print("=" * 62, flush=True)
    for name, (video, _) in zip(paths, stream_params):
        auth = f"{args.rtsp_user}:{args.rtsp_pass}@" if args.rtsp_user else ""
        label = f"云端流源 {mask_url(video)}" if args.relay_url else os.path.basename(video)
        print(f"  {label}", flush=True)
        # 这一行是 GUI 解析拉流地址的依据，格式勿改（-> 后必须是完整 URL）
        print(f"    -> rtsp://{auth}{lan_ip}:{args.port}/{name}", flush=True)
    print("=" * 62, flush=True)
    print("按 Ctrl+C 停止所有推流\n", flush=True)

    for i in range(len(videos)):
        procs.append(spawn(i))

    # ---- 7. 监控 + 退出处理
    stopping = {"flag": False}
    fail_count = [0] * len(videos)   # 每路连续失败次数

    def shutdown(signum=None, frame=None):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        log("正在停止 ...")
        for p in procs:
            p.terminate()
        mtx.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        try:
            mtx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mtx.kill()
        log("已全部停止")
        sys.exit(0)

    # GUI 被关闭、父进程异常退出时也尽量回收 MediaMTX，避免下次启动残留占端口。
    def emergency_cleanup():
        """父进程异常退出时回收所有子进程，避免下次启动被残留进程占端口。"""
        for child in procs:
            try:
                if child.poll() is None:
                    child.kill()
            except Exception:
                pass
        try:
            if mtx.poll() is None:
                mtx.kill()
        except Exception:
            pass
    atexit.register(emergency_cleanup)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    # 主循环：某一路 ffmpeg 意外退出时自动重启（循环模式下）
    dead = [False] * len(videos)     # 熔断后不再重试的路径
    while True:
        time.sleep(3)
        if stopping["flag"]:
            break
        for i, p in enumerate(procs):
            if dead[i]:
                continue
            if p.poll() is None:
                fail_count[i] = 0      # 运行正常，重置连续失败计数
                continue
            if args.no_loop:
                log(f"路径 {paths[i]}：视频已播完，该路流结束")
                dead[i] = True
            elif p.returncode == 0 and args.clip_duration:
                log(f"路径 {paths[i]}：片段播完，重新从起点播放")
                procs[i] = spawn(i)
            else:
                fail_count[i] += 1
                # 读 ffmpeg 日志判断具体原因，给出可操作提示
                why = ""
                try:
                    with open(os.path.join(APP_DIR, f"ffmpeg_{paths[i]}.log"),
                              encoding="utf-8", errors="replace") as lf:
                        txt = lf.read()
                    if "400 Bad Request" in txt:
                        why = "原因：RTSP 服务端拒绝发布（400），需查看 MediaMTX 原始错误，不能据此判定端口占用。"
                        for server_line in mtx_tail():
                            log("MediaMTX: " + server_line)
                    elif "Connection refused" in txt or "Connection reset" in txt:
                        why = "原因：连不上本地 RTSP 服务，可能是 mediamtx 未启动成功。"
                except Exception:
                    pass
                if fail_count[i] >= 5:
                    dead[i] = True
                    log(f"路径 {paths[i]}：连续 {fail_count[i]} 次启动失败，停止该路推流")
                    if why:
                        log(f"  {why}")
                    log(f"（失败原因见日志文件：ffmpeg_{paths[i]}.log，可发给助手分析）")
                    continue
                log(f"路径 {paths[i]}：推流进程退出(第 {fail_count[i]} 次)，3 秒后重启 ...")
                if why:
                    log(f"  {why}")
                procs[i] = spawn(i)


if __name__ == "__main__":
    main()
