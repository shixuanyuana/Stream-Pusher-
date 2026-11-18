#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RTSP 推流测试工具 - 图形界面版

把本地视频文件伪装成 RTSP 摄像头，供同网段的视频分析服务器拉流测试。
界面操作：选视频 → 点「开始推流」→ 把打印的 rtsp 地址填到分析服务器。

底层调用同目录下的 rtsp_pusher.exe（命令行版），依赖内置的 ffmpeg/ 和 mediamtx/。
"""

import os
import re
import sys
import queue
import threading
import subprocess
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SUPPORTED_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".ts", ".m4v", ".h264", ".265", ".webm"}
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_cli():
    """定位命令行版主程序。"""
    exe = os.path.join(APP_DIR, "rtsp_pusher.exe")
    if os.path.isfile(exe):
        return exe
    # 开发环境：同目录下的 py 脚本
    py = os.path.join(APP_DIR, "rtsp_pusher.py")
    if os.path.isfile(py):
        return py
    return ""


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def decode_line(b):
    """子进程输出解码：优先 UTF-8，失败退回 GBK（兼容旧版/控制台默认编码）。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc).rstrip()
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace").rstrip()


def get_lan_ip():
    """探测本机局域网 IP（UDP trick，不真正发包）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def build_cmd(base, mode, sel, url, port, names, copy_mode=True, no_audio=False, loop=True, reconnect=True, start="", end=""):
    """按模式拼出命令行参数（纯函数，便于测试）。"""
    cmd = list(base)
    if mode == "relay":
        cmd += ["--relay-url", url, "--port", str(port), "--paths", names[0]]
        if no_audio:
            cmd += ["--no-audio"]
    elif mode == "push":
        cmd += ["--videos", sel[0], "--push-url", url]
        if no_audio:
            cmd += ["--no-audio"]
        if not reconnect:
            cmd += ["--no-reconnect"]
        if not loop:
            cmd += ["--no-loop"]
    else:
        cmd += ["--videos", *sel, "--paths", *names, "--port", str(port)]
        if copy_mode:
            cmd += ["--copy"]
        if no_audio:
            cmd += ["--no-audio"]
        if not loop:
            cmd += ["--no-loop"]
    if mode != "relay":
        if start:
            cmd += ["--start", start]
        if end:
            cmd += ["--end", end]
    return cmd


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        self.videos = []          # 当前目录下的视频（绝对路径）
        self.cli = find_cli()
        self._live_addrs = []     # 从日志解析到的拉流地址

        root.title("RTSP 推流测试工具  —  把视频变成 RTSP / 云端直播流")
        root.geometry("1000x960")
        root.minsize(860, 780)

        self._build_ui()
        self.on_mode()
        self._poll_queue()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.cli:
            self.log("错误：找不到 rtsp_pusher.exe（命令行主程序）。请确认安装目录完整。")

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # ===== 视频来源 =====
        src = ttk.LabelFrame(self.root, text=" 第一步：选择视频 ")
        src.pack(fill="x", **pad)

        row1 = ttk.Frame(src)
        row1.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(row1, text="视频目录：").pack(side="left")
        self.dir_var = tk.StringVar()
        ent = ttk.Entry(row1, textvariable=self.dir_var)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row1, text="浏览目录…", command=self.choose_dir, width=12).pack(side="left")
        ttk.Button(row1, text="选择文件…", command=self.choose_files, width=12).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(src)
        row2.pack(fill="x", padx=10, pady=(0, 8))
        cols = ("name", "size", "res")
        self.tree = ttk.Treeview(row2, columns=cols, show="headings", height=5, selectmode="extended")
        self.tree.heading("name", text="视频文件（可多选，Ctrl/Shift）")
        self.tree.heading("size", text="大小")
        self.tree.heading("res", text="分辨率")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("res", width=110, anchor="center")
        sb = ttk.Scrollbar(row2, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.start())

        row3 = ttk.Frame(src)
        row3.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(row3, text="全选", width=8, command=self.select_all).pack(side="left")
        ttk.Button(row3, text="清空列表", width=10, command=self.clear_list).pack(side="left", padx=(6, 0))
        self.count_var = tk.StringVar(value="已选中 0 个视频")
        ttk.Label(row3, textvariable=self.count_var, foreground="#666").pack(side="left", padx=12)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_count())

        # ===== 推流方式 =====
        mode = ttk.LabelFrame(self.root, text=" 第二步：推流方式 ")
        mode.pack(fill="x", **pad)

        mr = ttk.Frame(mode)
        mr.pack(fill="x", padx=10, pady=(8, 2))
        self.mode = tk.StringVar(value="local")
        ttk.Radiobutton(mr, text="本地 RTSP（服务器同网段）", value="local",
                        variable=self.mode, command=self.on_mode).pack(side="left")
        ttk.Radiobutton(mr, text="推送到云端（异地测试，RTMP/SRT）", value="push",
                        variable=self.mode, command=self.on_mode).pack(side="left", padx=18)
        ttk.Radiobutton(mr, text="云端流转本地 RTSP", value="relay",
                        variable=self.mode, command=self.on_mode).pack(side="left")

        ur = ttk.Frame(mode)
        ur.pack(fill="x", padx=10, pady=(4, 2))
        self.url_label = ttk.Label(ur, text="地址：", width=10)
        self.url_label.pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(ur, textvariable=self.url_var, state="disabled")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_probe = ttk.Button(ur, text="检测拉流", width=10, state="disabled",
                                    command=self.probe_url)
        self.btn_probe.pack(side="left")

        self.mode_hint = ttk.Label(mode, text="把本地视频推成 RTSP 流，供同网段的分析服务器拉流。",
                                   foreground="#888")
        self.mode_hint.pack(fill="x", padx=12, pady=(0, 8))

        # ===== 参数 =====
        opt = ttk.LabelFrame(self.root, text=" 第三步：参数 ")
        opt.pack(fill="x", **pad)

        r = ttk.Frame(opt)
        r.pack(fill="x", padx=10, pady=8)
        ttk.Label(r, text="端口：").pack(side="left")
        self.port_var = tk.StringVar(value="8554")
        ttk.Entry(r, textvariable=self.port_var, width=8).pack(side="left")
        ttk.Label(r, text="  流名前缀：").pack(side="left")
        self.path_var = tk.StringVar(value="video")
        ttk.Entry(r, textvariable=self.path_var, width=10).pack(side="left")
        ttk.Label(r, text="（多路时自动命名为 video1、video2…）",
                  foreground="#888").pack(side="left", padx=(4, 0))

        r2 = ttk.Frame(opt)
        r2.pack(fill="x", padx=10, pady=(0, 8))
        self.copy_var = tk.BooleanVar(value=True)
        self.noaudio_var = tk.BooleanVar(value=False)
        self.loop_var = tk.BooleanVar(value=True)
        self.reconnect_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="原视频直推（含 H.265，不转码）", variable=self.copy_var).pack(side="left")
        ttk.Checkbutton(r2, text="只推视频（丢弃音频）", variable=self.noaudio_var).pack(side="left", padx=16)
        ttk.Checkbutton(r2, text="循环播放", variable=self.loop_var).pack(side="left", padx=16)
        ttk.Checkbutton(r2, text="断线自动重连（云端）", variable=self.reconnect_var).pack(side="left", padx=16)

        clip = ttk.Frame(opt)
        clip.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(clip, text="片段开始：").pack(side="left")
        self.start_var = tk.StringVar()
        ttk.Entry(clip, textvariable=self.start_var, width=10).pack(side="left")
        ttk.Label(clip, text="  片段结束：").pack(side="left")
        self.end_var = tk.StringVar()
        ttk.Entry(clip, textvariable=self.end_var, width=10).pack(side="left")
        ttk.Label(clip, text="  格式 分:秒，如 10:00 = 10 分钟；长视频可填 1:10:00 = 1 小时 10 分钟", foreground="#666").pack(side="left", padx=10)
        ttk.Label(opt, text="开始留空 = 从头播；结束留空 = 播到结尾。原视频直推按关键帧切入，时间可能略有偏差。",
                  foreground="#666").pack(anchor="w", padx=10, pady=(0, 8))

        # ===== 操作 =====
        act = ttk.Frame(self.root)
        act.pack(fill="x", **pad)
        self.btn_start = ttk.Button(act, text="▶  开始推流", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="■  停止推流", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(act, textvariable=self.status_var, foreground="#0a6").pack(side="left", padx=12)

        # ===== 拉流地址 =====
        addr = ttk.LabelFrame(self.root, text=" 拉流地址（填到分析服务器） ")
        addr.pack(fill="x", **pad)
        arow = ttk.Frame(addr)
        arow.pack(fill="x", padx=10, pady=8)
        self.addr_text = tk.Text(arow, height=3, wrap="none", font=("Consolas", 10))
        self.addr_text.pack(side="left", fill="x", expand=True)
        self.addr_text.configure(state="disabled")
        ttk.Button(arow, text="复制", width=8, command=self.copy_addr).pack(side="left", padx=(6, 0))

        # ===== 日志（占满剩余空间）=====
        lg = ttk.LabelFrame(self.root, text=" 运行日志 ")
        lg.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(lg, height=18, wrap="word", font=("Consolas", 10))
        lsc = ttk.Scrollbar(lg, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsc.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
        lsc.pack(side="left", fill="y", padx=(0, 10), pady=8)

    # ------------------------------------------------------------ 事件
    def log(self, msg):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def choose_dir(self):
        d = filedialog.askdirectory(title="选择存放视频的目录")
        if d:
            self.dir_var.set(d)
            self.load_dir(d)

    def choose_files(self):
        types = [("视频文件", " ".join("*" + e for e in sorted(SUPPORTED_EXT))), ("所有文件", "*.*")]
        files = filedialog.askopenfilenames(title="选择视频文件（可多选）", filetypes=types)
        if not files:
            return
        self.tree.delete(*self.tree.get_children())
        self.videos = list(files)
        for f in self.videos:
            self._insert_row(f)
        self.dir_var.set(os.path.dirname(files[0]))
        self.update_count()
        self.log(f"已选择 {len(self.videos)} 个视频文件")

    def load_dir(self, path):
        self.tree.delete(*self.tree.get_children())
        self.videos = []
        try:
            names = sorted(os.listdir(path))
        except OSError as e:
            messagebox.showerror("无法读取目录", str(e))
            return
        for n in names:
            full = os.path.join(path, n)
            if os.path.isfile(full) and os.path.splitext(n)[1].lower() in SUPPORTED_EXT:
                self.videos.append(full)
                self._insert_row(full)
        if self.videos:
            self.log(f"目录下找到 {len(self.videos)} 个视频：{path}")
        else:
            self.log(f"该目录下没有支持的视频文件：{path}")
        self.update_count()

    def _insert_row(self, f):
        size = human_size(os.path.getsize(f)) if os.path.isfile(f) else "?"
        self.tree.insert("", "end", values=(os.path.basename(f), size, "…"), iid=f)
        # 后台探测分辨率（不阻塞界面）
        threading.Thread(target=self._probe_res, args=(f,), daemon=True).start()

    def _probe_res(self, f):
        probe = os.path.join(APP_DIR, "ffmpeg", "ffprobe.exe")
        if not os.path.isfile(probe):
            import shutil
            probe = shutil.which("ffprobe") or ""
        if not probe:
            return
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", f],
                capture_output=True, text=True, timeout=15,
                creationflags=CREATE_NO_WINDOW,
            ).stdout.strip().strip(",")
            if "x" not in out and "," in out:
                out = "x".join(out.split(",")[:2])
            if out and "," in out:
                out = out.split(",")[0] + "x" + out.split(",")[1]
            if out:
                self.root.after(0, lambda: self._set_res(f, out.replace(",", "x")))
        except Exception:
            pass

    def _set_res(self, f, res):
        if self.tree.exists(f):
            vals = list(self.tree.item(f, "values"))
            vals[2] = res
            self.tree.item(f, values=vals)

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())
        self.update_count()

    def clear_list(self):
        self.tree.delete(*self.tree.get_children())
        self.videos = []
        self.dir_var.set("")
        self.update_count()

    def update_count(self):
        n = len(self.tree.selection())
        self.count_var.set(f"已选中 {n} 个视频" if n else "已选中 0 个视频")

    def copy_addr(self):
        txt = self.addr_text.get("1.0", "end").strip()
        if not txt:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.status_var.set("地址已复制到剪贴板")

    def _set_addr(self, txt, append=False):
        self.addr_text.configure(state="normal")
        if not append:
            self.addr_text.delete("1.0", "end")
        self.addr_text.insert("end", txt + "\n")
        self.addr_text.configure(state="disabled")

    # ------------------------------------------------------------ 模式切换
    def on_mode(self):
        m = self.mode.get()
        if m == "local":
            self.url_label.configure(text="地址：")
            self.url_entry.configure(state="disabled")
            self.btn_probe.configure(state="disabled")
            self.mode_hint.configure(
                text="把本地视频推成 RTSP 流，供同网段的分析服务器拉流（最常用）。")
        elif m == "push":
            self.url_label.configure(text="推流地址：")
            self.url_entry.configure(state="normal")
            self.btn_probe.configure(state="disabled")
            self.mode_hint.configure(
                text="把视频推送到云端中转（rtmp://… 或 srt://…）。两种推流方式都是原样直推（copy），"
                     "不改分辨率帧率。\n提示：8K 等超大码率素材请用 SRT 推流地址（RTMP 通道通常撑不住 30 Mbps 以上）。",
                wraplength=940)
        else:
            self.url_label.configure(text="拉流地址：")
            self.url_entry.configure(state="normal")
            self.btn_probe.configure(state="normal")
            self.mode_hint.configure(
                text="把云端拉流地址（rtmp/flv/m3u8/srt/rtsp）拉下来，转成本地 RTSP 流，"
                     "供只能拉 RTSP 的服务器使用。",
                wraplength=820)

    def probe_url(self):
        """检测输入的拉流地址是否可用（不推流）。"""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("地址为空", "请先粘贴要检测的拉流地址（FLV/M3U8/RTMP/SRT/RTSP）。")
            return
        if not self.cli:
            return
        cmd = self._base_cmd() + ["--probe-url", url]
        self.log(f"检测地址：{self._mask(url)}")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=CREATE_NO_WINDOW, bufsize=0)
        except Exception as e:
            messagebox.showerror("检测失败", str(e))
            return
        threading.Thread(target=self._reader, args=(p,), daemon=True).start()

    def _base_cmd(self):
        if os.path.isfile(self.cli) and self.cli.lower().endswith(".exe"):
            return [self.cli]
        return [sys.executable if not getattr(sys, "frozen", False) else "python", self.cli]

    @staticmethod
    def _mask(url):
        return re.sub(r"((?:auth_key|token|key|secret)=)[^&\s]+", r"\1***", url, flags=re.I)

    # ------------------------------------------------------------ 推流控制
    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if not self.cli:
            messagebox.showerror("缺少主程序", "找不到 rtsp_pusher.exe，请重新安装工具。")
            return
        mode = self.mode.get()
        url = self.url_var.get().strip()
        sel = list(self.tree.selection())

        # 各模式的输入校验
        if mode == "relay":
            if not url:
                messagebox.showwarning("地址为空", "云端转 RTSP 模式需要填写【拉流地址】。")
                return
            if not re.match(r"^(rtsp|rtsps|rtmp|rtmps|http|https|srt)://", url, re.I):
                messagebox.showerror("地址格式不对",
                                     "拉流地址应以 rtsp:// rtmp:// http(s):// srt:// 开头。")
                return
        elif mode == "push":
            if not sel:
                messagebox.showwarning("未选择视频", "请先在列表中选中要推送的视频。")
                return
            if not re.match(r"^(rtmp|rtmps|srt)://", url, re.I):
                messagebox.showerror("推流地址不对",
                                     "推流地址应以 rtmp:// 或 srt:// 开头（如云端的 RTMP/SRT 推流地址）。")
                return
        else:
            if not sel:
                messagebox.showwarning("未选择视频", "请先在列表中选中要推流的视频（可多选）。")
                return

        try:
            port = int(self.port_var.get())
            assert 1 <= port <= 65535
        except Exception:
            messagebox.showerror("端口无效", "端口应为 1~65535 之间的整数。")
            return

        prefix = (self.path_var.get() or "video").strip()
        names = [f"{prefix}{i + 1}" for i in range(max(1, len(sel)))]

        start, end = self.start_var.get().strip(), self.end_var.get().strip()
        if mode == "relay":
            start = end = ""
        else:
            def seconds(value):
                parts = value.split(":")
                if len(parts) > 3 or not parts or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
                    raise ValueError("请输入秒数、分:秒或 时:分:秒，例如 10:00 表示 10 分钟")
                nums = [float(p) for p in parts]
                if any(p >= 60 for p in nums[1:]):
                    raise ValueError("分/秒部分必须小于 60")
                return sum(n * 60 ** i for i, n in enumerate(reversed(nums)))
            try:
                first = seconds(start) if start else 0
                last = seconds(end) if end else None
                if last is not None and last <= first:
                    raise ValueError("结束时间必须晚于开始时间")
            except ValueError as exc:
                messagebox.showerror("片段时间无效", str(exc))
                return

        cmd = build_cmd(self._base_cmd(), mode, sel, url, port, names,
                        self.copy_var.get(), self.noaudio_var.get(), self.loop_var.get(),
                        self.reconnect_var.get(), start, end)

        self.log_text.delete("1.0", "end")
        self._live_addrs = []
        if mode == "push":
            # 云端推流：显示推流地址（隐去鉴权）+ 提示用云端的拉流地址
            self._set_addr(f"推流中：{self._mask(url)}")
            self.log("提示：推流成功后，把云端控制台给出的 FLV / M3U8 / RTMP / SRT 拉流地址"
                     "填到分析服务器；也可切到「检测拉流」验证云端是否收到流。")
        else:
            # 本地/中转模式：先立即显示推算出的 RTSP 地址，避免空白
            lan_ip = get_lan_ip()
            self._set_addr("\n".join(f"rtsp://{lan_ip}:{port}/{n}" for n in names))
        self.log("启动参数：" + " ".join(f'"{a}"' if " " in a else a for a in cmd[1:]))
        self.log("")

        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, bufsize=0,
            )
        except Exception as e:
            messagebox.showerror("启动失败", f"无法启动推流进程：\n{e}")
            return

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status_var.set("推流中…")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        """逐行读取子进程输出（字节流，自适应解码），并把拉流地址提取出来。"""
        url_re = re.compile(r"(rtsp://[^\s\"]+)")
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = decode_line(raw)
                if not line:
                    continue
                self.q.put(("log", line))
                m = url_re.search(line)
                if m and ("->" in line or "rtsp://" in line):
                    self.q.put(("addr", m.group(1)))
        except Exception as e:
            self.q.put(("log", f"[读取输出异常] {e}"))
        proc.wait()
        self.q.put(("done", proc.returncode))

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.status_var.set("正在停止…")
        self.log("正在停止推流 …")
        try:
            # /T 连子进程（mediamtx、ffmpeg）一起结束
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "addr":
                    if payload not in self._live_addrs:
                        self._live_addrs.append(payload)
                        self._set_addr("\n".join(self._live_addrs))
                    self.status_var.set("推流中（地址已就绪）")
                elif kind == "done":
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status_var.set("已停止" if payload in (0, None) or payload >= 0 else f"已退出({payload})")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askokcancel("确认退出", "推流仍在运行，退出会停止推流。确定退出？"):
                return
            self.stop()
        self.root.destroy()


def log_exception(exc_type, exc_value, tb):
    """把未捕获异常写入 gui_error.log（打包成 --windowed 后没有控制台，只能落盘排障）。"""
    import traceback
    try:
        with open(os.path.join(APP_DIR, "gui_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write("".join(traceback.format_exception(exc_type, exc_value, tb)))
    except Exception:
        pass


def main():
    sys.excepthook = log_exception
    # 自检模式：验证打包后能否定位到依赖（无窗口输出，结果写入文件）
    if "--selftest" in sys.argv:
        lines = [
            f"APP_DIR={APP_DIR}",
            f"CLI={find_cli()}",
            f"FFMPEG={os.path.isfile(os.path.join(APP_DIR, 'ffmpeg', 'ffmpeg.exe'))}",
            f"MEDIAMTX={os.path.isfile(os.path.join(APP_DIR, 'mediamtx', 'mediamtx.exe'))}",
            f"FROZEN={getattr(sys, 'frozen', False)}",
        ]
        with open(os.path.join(APP_DIR, "selftest_result.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return
    root = tk.Tk()
    try:
        # 高 DPI 适配
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    # Tk 回调里的异常也要落盘，否则界面卡住时完全无线索
    root.report_callback_exception = log_exception
    try:
        App(root)
        root.mainloop()
    except Exception:
        import sys as _s
        log_exception(*_s.exc_info())
        raise


if __name__ == "__main__":
    main()
