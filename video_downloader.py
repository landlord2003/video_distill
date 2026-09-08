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
import shutil
import subprocess
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))



def _resolve(module, exe_names):
    """多重兜底定位一个命令行工具，返回可执行的 argv 前缀或 None。

    - 优先：当前解释器能 `import <module>` -> [sys.executable, "-m", module]
    - 其次：在 venv 的 Scripts 目录下找 <exe>（跨平台自动补 .exe）
    - 再次：PATH 里 which <exe>
    """
    # 1) 模块方式（最稳：用跑着这个进程的同一个 python）
    try:
        __import__(module)
        return [sys.executable, "-m", module]
    except Exception:
        pass
    # 2) venv Scripts 目录下的可执行文件（Linux 无后缀、Windows 有 .exe）
    exe_dir = os.path.dirname(sys.executable)
    for name in exe_names:
        for cand in (os.path.join(exe_dir, name),
                    os.path.join(exe_dir, name + ".exe")):
            if os.path.isfile(cand):
                return [cand]
    # 3) PATH 查找
    for name in exe_names:
        found = shutil.which(name) or shutil.which(name + ".exe")
        if found:
            return [found]
    return None


def get_yt_dlp_cmd():
    """返回 yt-dlp 的可执行 argv 前缀；找不到则抛出带诊断信息的异常。"""
    cmd = _resolve("yt_dlp", ["yt-dlp", "yt_dlp"])
    if cmd:
        return cmd
    raise RuntimeError(
        "找不到 yt-dlp：请在该 Python 环境执行 `pip install yt-dlp` 后重启服务。"
        f"\n(sys.executable={sys.executable})"
    )


def get_ffmpeg_exe():
    """返回 ffmpeg 可执行路径；优先 imageio-ffmpeg 自带的，其次 PATH，最后裸名。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe().replace("\\", "/")
    except Exception:
        pass
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found.replace("\\", "/")
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


def get_cookies_arg():
    """返回 cookies 参数列表（最稳：cookie 文件优先，不依赖浏览器实时状态）。

    优先级：
      1. 环境变量 VIDEO_COOKIES 指向的 cookies.txt（Netscape 格式）
      2. 项目根目录下 cookies.txt
    找到则返回 ["--cookies", 路径]，否则返回 []（交给浏览器实时提取兜底）。
    """
    cands = []
    env = os.environ.get("VIDEO_COOKIES")
    if env:
        cands.append(env)
    cands.append(os.path.join(BASE, "cookies.txt"))
    for c in cands:
        if c and os.path.isfile(c):
            return ["--cookies", c]
    return []


def download_douyin(url: str, out_dir: str) -> str:
    """抖音/抖音视频号分享链接 -> 本地 mp4。返回文件路径，失败抛异常。
    抖音 App/网页 -> 视频'分享' -> '复制链接' 得到 v.douyin.com/xxxx 传入；
    或把搜索页/分享页链接（含 modal_id）直接传入，本函数会自动改写。
    抖音反爬需登录态 cookie，按优先级尝试：
      1. cookies.txt 文件（VIDEO_COOKIES 环境变量 或 项目内 cookies.txt）——最稳
      2. 浏览器实时 cookie（chrome/edge/chromium/brave，需该浏览器已登录抖音）
    若都失败，抛出带明确指引的异常。
    """
    url = normalize_douyin_url(url)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "%(id)s.%(ext)s")
    cookie_file = get_cookies_arg()
    tried = []  # 完整记录尝试过的每一种方式，便于反馈

    def build(base_extra):
        return get_yt_dlp_cmd() + [
            "-f", "bestvideo+bestaudio/best",
            "--no-playlist", "--merge-output-format", "mp4",
            "--no-warnings",
            # 抖音现强制浏览器指纹：必须带 UA + Referer，否则即便有 cookie 也报
            # "Fresh cookies (not necessarily logged in) are needed"
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
            "--referer", "https://www.douyin.com/",
            "-o", tmpl,
        ] + base_extra + [url]

    last_err = None
    # 1) 优先用 cookies.txt（不需要 Chrome 在跑，最稳）
    if cookie_file:
        tried.append("cookies.txt(项目内)")
        try:
            subprocess.run(build(cookie_file), check=True, timeout=300, env=dict(os.environ))
            return _pick(out_dir, last_err)
        except subprocess.TimeoutExpired:
            last_err = "下载超时（抖音响应慢/被拦截），请重试或检查网络/代理"
        except subprocess.CalledProcessError as e:
            last_err = (f"用 cookies.txt 下载失败（退出码 {e.returncode}）："
                        "请确认 cookies.txt 来自已登录抖音的浏览器且未过期")
        except FileNotFoundError as e:
            last_err = "找不到 yt-dlp：请用 venv 的 python 运行 `pip install yt-dlp`"
    else:
        tried.append("cookies.txt(未找到)")
    # 2) 退化：浏览器实时 cookie
    for br in ("chrome", "edge", "chromium", "brave"):
        tried.append(f"浏览器({br})实时cookie")
        try:
            subprocess.run(build(["--cookies-from-browser", br]), check=True,
                          timeout=300, env=dict(os.environ))
            return _pick(out_dir, last_err)
        except subprocess.TimeoutExpired:
            last_err = "下载超时（抖音响应慢/被拦截），请重试或检查网络/代理"
        except subprocess.CalledProcessError as e:
            last_err = (f"浏览器({br}) cookie 提取失败（退出码 {e.returncode}）："
                        "该浏览器未登录抖音，或浏览器正运行占用 cookie 库——"
                        "请关掉浏览器后重试，或改用 cookies.txt")
        except FileNotFoundError as e:
            last_err = "找不到 yt-dlp：请用 venv 的 python 运行 `pip install yt-dlp`"
            break
    # 3) 兜底：playwright 拦截直链（见 douyin_auto.py）
    #    yt-dlp 的 DouyinIE 要前端实时生成的 msToken，静态 cookie 拿不到；
    #    playwright 打开页面让抖音前端自己发 detail 请求，我们拦截响应拿直链。
    try:
        import douyin_auto
        tried.append("playwright拦截(Edge直链)")
        try:
            return douyin_auto.download_douyin_playwright(url, str(out_dir))
        except Exception as pe:
            last_err = f"playwright 拦截方案失败：{pe}"
    except ImportError:
        tried.append("playwright拦截(未安装 playwright，跳过)")

    raise FileNotFoundError(
        f"抖音下载失败：已依次尝试 [{', '.join(tried)}] 均失败。\n"
        f"最后错误：{last_err}\n\n"
        f"两条路选一：\n"
        f"  (A) 最稳：用浏览器插件(如 Get cookies.txt LOCALLY)导出已登录抖音的 "
        f"cookies.txt，放到本项目目录或设置 VIDEO_COOKIES 环境变量后重试（Chrome 关着也能用）。\n"
        f"  (B) 绕过下载：抖音 App 里视频→分享→「保存本地」→手机相册→传到电脑→"
        f"在工具里『选择本地视频』上传（走本地通道，完全不依赖抖音下载/cookie）。"
    )


def _pick(out_dir, last_err):
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if files:
        return str(files[0])
    alt = sorted(out_dir.glob("*.mkv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if alt:
        return str(alt[0])
    raise FileNotFoundError(
        "yt-dlp 未产出视频文件，可能链接失效 / 需登录 cookie / 被风控"
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
