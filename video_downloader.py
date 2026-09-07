#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频获取层（capture）：抖音/抖音视频号分享链接(yt-dlp) + 视频号直链(ffmpeg) + 本地文件
- 纯本地运行；抖音获取需联网访问抖音服务器（不可避免），整理环节全本地、零 API 费
- 支持「链接清单批量下载」：传入 url 列表，逐个下载，返回 [(url, 本地路径|错误)]

依赖：yt-dlp（已装）、imageio-ffmpeg（已装，提供 ffmpeg 二进制）
关键修复：
  - yt-dlp 用 `sys.executable -m yt_dlp` 调用，避免 venv 内可执行文件不在 PATH 导致
    FileNotFoundError: [WinError 2] 系统找不到指定的文件
  - 抖音「搜索页 / 分享页」URL 形如
      https://www.douyin.com/jingxuan/search/...?...&modal_id=7681253710374112538&type=general
    其中 modal_id 即视频 id，自动改写为直链 https://www.douyin.com/video/{id}
    （yt-dlp 的 DouyinIE 只认直链 / v.douyin.com 短链）
"""
import os
import re
import sys
import time
import subprocess
from pathlib import Path


def get_yt_dlp_cmd():
    """用当前解释器跑 yt-dlp 模块，无需 yt-dlp.exe 在 PATH。"""
    return [sys.executable, "-m", "yt_dlp"]


def get_ffmpeg_exe():
    """从 imageio-ffmpeg 拿 ffmpeg 可执行路径（无需系统安装）。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe().replace("\\", "/")
    except Exception:
        return "ffmpeg"


def normalize_douyin_url(url: str) -> str:
    """把抖音搜索页/分享页 URL 改写为可下载的直链。

    支持：
      - 含 modal_id 的页面 URL -> https://www.douyin.com/video/{modal_id}
      - 已是 /video/<id> 或 v.douyin.com 短链 -> 原样返回
    """
    u = (url or "").strip()
    m = re.search(r"modal_id=(\d+)", u)
    if m:
        return f"https://www.douyin.com/video/{m.group(1)}"
    return u


def download_douyin(url: str, out_dir: str) -> str:
    """抖音/抖音视频号分享链接 -> 本地 mp4。返回文件路径，失败抛异常。
    抖音 App/网页 -> 视频'分享' -> '复制链接' 得到 v.douyin.com/xxxx 传入；
    或把搜索页/分享页链接（含 modal_id）直接传入，本函数会自动改写。
    注：默认带水印；抖音现需浏览器 cookie（反爬），本函数会在失败后自动用
        --cookies-from-browser 依次尝试 chrome/edge/chromium（需你在已登录
        抖音的浏览器环境下运行）。
    """
    url = normalize_douyin_url(url)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "%(id)s.%(ext)s")
    base = get_yt_dlp_cmd() + [
        "-f", "bestvideo+bestaudio/best",
        "--no-playlist", "--merge-output-format", "mp4",
        "--no-warnings",
        "-o", tmpl,
    ]
    # 依次尝试：无 cookie -> 浏览器 cookie（抖音反爬常需）
    browsers = ["", "chrome", "edge", "chromium", "brave"]
    last_err = None
    produced = []
    for br in browsers:
        cmd = list(base)
        if br:
            cmd += ["--cookies-from-browser", br]
        try:
            subprocess.run(cmd + [url], check=True, timeout=300, env=dict(os.environ))
        except subprocess.TimeoutExpired:
            last_err = "下载超时（抖音响应慢/被拦截），请重试或检查网络/代理"
            continue
        except FileNotFoundError as e:
            last_err = "找不到 yt-dlp：请用 venv 的 python 运行 `pip install yt-dlp`"
            break
        except subprocess.CalledProcessError as e:
            last_err = f"yt-dlp 退出码 {e.returncode}（可能需登录抖音的浏览器 cookie）"
            continue
        # 检查产出
        files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return str(files[0])
        alt = sorted(out_dir.glob("*.mkv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if alt:
            return str(alt[0])
        last_err = "yt-dlp 未产出视频文件，可能链接失效 / 需登录 cookie / 被风控"
    raise FileNotFoundError(
        f"抖音下载失败：{last_err}。\n"
        f"解决：在已登录抖音的 Chrome/Edge 浏览器会话下运行本服务，"
        f"或导出 cookies.txt 后改用直链下载。"
    )


def download_direct(url: str, out_dir: str) -> str:
    """m3u8 / mp4 直链（抓包得到的视频号直链）-> 本地 mp4（ffmpeg 合并）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = str(out_dir / "direct_%d.mp4" % int(time.time()))
    ff = get_ffmpeg_exe()
    subprocess.run([ff, "-y", "-i", url, "-c", "copy", out], check=True)
    return out


def detect_source(url: str) -> str:
    u = (url or "").lower().strip()
    if not u:
        return "unknown"
    if "douyin.com" in u or "v.douyin" in u or "iesdouyin" in u:
        return "douyin"
    if u.endswith(".m3u8") or "m3u8" in u or u.endswith(".mp4") or u.endswith(".mov") or "mp4" in u or "video" in u:
        return "direct"
    return "unknown"


def download_one(url: str, out_dir: str):
    """下载单条链接，返回 (本地路径, 错误)。"""
    src = detect_source(url)
    try:
        if src == "douyin":
            return download_douyin(url, out_dir), None
        if src == "direct":
            return download_direct(url, out_dir), None
        return None, "无法识别来源（非抖音/直链）：" + url
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def download_batch(urls, out_dir: str):
    """链接清单批量下载。urls: 可迭代的链接字符串（每行一个）。
    返回 [{"url":..., "path":本地路径或None, "error":错误或无}, ...]
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for raw in urls:
        url = (raw or "").strip()
        if not url or url.startswith("#"):
            continue
        path, err = download_one(url, str(out_dir))
        results.append({"url": url, "path": path, "error": err})
    return results


if __name__ == "__main__":
    import sys as _sys
    urls = [u for u in _sys.argv[1:] if u]
    if not urls:
        print("用法: video_downloader.py <url1> [url2 ...]")
        _sys.exit(1)
    for r in download_batch(urls, "_batch_out"):
        print(r)
