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
    """返回 yt-dlp 的可执行 argv 前缀；找不到则抛出带诊断信息的异常。

    优先用项目内独立版 yt-dlp.exe（nightly，YouTube 接口月更，
    pip 装的版本常落后导致 'This video is unavailable' / 风控问题）。
    """
    pinned = os.path.join(BASE, "yt-dlp.exe")
    if os.path.isfile(pinned):
        return [pinned]
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


# ---------- 免 cookie 抖音下载（借鉴 chubbyskills/douyin-transcribe，MIT 参考） ----------
# 原理：iesdouyin.com/share/video/<id> 分享页内嵌 window._ROUTER_DATA，
# 其中 item_list[0].video.play_addr.url_list[0] 即去水印直链（playwm->play）。
# 无需登录态/yt-dlp/playwright——cookies.txt 过期时这条仍可用。

_DOUYIN_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)


def _direct_opener():
    """直连 opener：忽略环境代理。抖音是国内站，走外部代理反而可能被拦/超时。"""
    import urllib.request as _ur
    return _ur.build_opener(_ur.ProxyHandler({}))


def _extract_douyin_id(url: str) -> str:
    """从各类抖音链接提取视频 id：modal_id= / /video/<id> / v.douyin.com 短链(跟随重定向)。"""
    u = (url or "").strip()
    m = re.search(r"modal_id=(\d+)", u)
    if m:
        return m.group(1)
    m = re.search(r"/video/(\d+)", u)
    if m:
        return m.group(1)
    if "v.douyin.com" in u or "iesdouyin.com" in u:
        import urllib.request as _ur
        opener = _direct_opener()
        for method in ("HEAD", "GET"):
            try:
                req = _ur.Request(u, headers={"User-Agent": _DOUYIN_MOBILE_UA}, method=method)
                with opener.open(req, timeout=15) as resp:
                    final = resp.geturl()
                    if method == "GET":
                        resp.read(65536)
                m = re.search(r"/video/(\d+)", final)
                if m:
                    return m.group(1)
            except Exception:
                continue
    return ""


def download_douyin_nocookie(url: str, out_dir: str):
    """免 cookie 抖音下载。返回 (本地文件路径, 视频标题)；失败抛异常，
    由 download_douyin 回落到 cookie 链路（本路径零外部依赖，优先级最高）。"""
    import json as _json
    import urllib.request as _ur
    vid = _extract_douyin_id(url)
    if not vid:
        raise ValueError("无法从链接提取抖音视频 id")
    share = f"https://www.iesdouyin.com/share/video/{vid}"
    opener = _direct_opener()
    req = _ur.Request(share, headers={"User-Agent": _DOUYIN_MOBILE_UA})
    with opener.open(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", html, re.DOTALL)
    if not m:
        raise RuntimeError("分享页未找到 _ROUTER_DATA（抖音接口可能已变更）")
    data = _json.loads(m.group(1).strip())
    # loaderData 的 key 形如 "video_(id)/page"，做宽容匹配防 key 变体
    item = None
    for v in (data.get("loaderData") or {}).values():
        if isinstance(v, dict) and isinstance(v.get("videoInfoRes"), dict):
            lst = v["videoInfoRes"].get("item_list") or []
            if lst:
                item = lst[0]
                break
    if not item or not isinstance(item.get("video"), dict):
        raise RuntimeError("分享页数据中没有视频项（可能是图文笔记/已下架）")
    urls = (item["video"].get("play_addr") or {}).get("url_list") or []
    if not urls:
        raise RuntimeError("未取到播放直链")
    vurl = urls[0].replace("playwm", "play")
    title = (item.get("desc") or "").strip() or f"抖音视频{vid}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"dy_{vid}.mp4"
    dreq = _ur.Request(vurl, headers={
        "User-Agent": _DOUYIN_MOBILE_UA,
        "Referer": "https://www.douyin.com/",
    })
    with opener.open(dreq, timeout=120) as resp, open(out, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    size = out.stat().st_size
    if size < 100 * 1024:
        try:
            out.unlink()
        except Exception:
            pass
        raise RuntimeError(f"下载内容过小({size}B)，疑似被风控拦截")
    return str(out), title


def download_douyin(url: str, out_dir: str) -> str:
    """抖音/抖音视频号分享链接 -> 本地 mp4。返回文件路径，失败抛异常。
    抖音 App/网页 -> 视频'分享' -> '复制链接' 得到 v.douyin.com/xxxx 传入；
    或把搜索页/分享页链接（含 modal_id）直接传入，本函数会自动改写。
    抖音反爬按优先级尝试：
      0. 免cookie分享页直链（iesdouyin _ROUTER_DATA，零依赖，优先）
      1. cookies.txt 文件（VIDEO_COOKIES 环境变量 或 项目内 cookies.txt）
      2. 浏览器实时 cookie（chrome/edge/chromium/brave，需该浏览器已登录抖音）
      3. playwright 拦截直链（douyin_auto.py，终极兜底）
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
    # 0) 免 cookie 快速通道（借鉴 chubbyskills）：iesdouyin 分享页直取去水印直链，
    #    零登录态依赖——cookies.txt 过期/浏览器没登录时这条仍然能用
    try:
        tried.append("免cookie分享页直链")
        p, _t = download_douyin_nocookie(url, str(out_dir))
        return p
    except Exception as e:
        last_err = f"免cookie直链失败：{e}"
    # 1) 退化到 cookies.txt（不需要 Chrome 在跑，最稳）
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


def _get_proxy():
    """YouTube 下载代理来源：VIDEO_PROXY > HTTPS_PROXY/HTTP_PROXY > 项目内 proxy.txt。"""
    for k in ("VIDEO_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    pf = os.path.join(BASE, "proxy.txt")
    if os.path.isfile(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt:
                return txt.splitlines()[0].strip()
        except Exception:
            pass
    return ""


def download_youtube(url: str, out_dir: str) -> str:
    """YouTube 链接 -> 本地 mp4（yt-dlp 原生支持）。

    国内网络必须走代理：代理来源优先级
      1. 环境变量 VIDEO_PROXY（专用于本工具，如 http://127.0.0.1:18081）
      2. 环境变量 HTTPS_PROXY / HTTP_PROXY（系统/会话级代理）
      3. 项目目录下 proxy.txt（第一行写代理地址，如 http://127.0.0.1:7890）
    都没有时仍会直连尝试一次（透明代理/VPN 全局模式下可用）。
    """
    url = (url or "").strip()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "yt_%(id)s.%(ext)s")
    extra = []
    proxy = _get_proxy()
    if proxy:
        extra += ["--proxy", proxy]
    # 显式给 yt-dlp 指定 ffmpeg：否则（服务 PATH 里没有 ffmpeg 时）yt-dlp 会
    # 静默跳过 bestvideo+bestaudio 的合并——产物是没有音轨的 fNNN.mp4 纯视频流，
    # 转写环节解码空音频直接报 tuple index out of range（--no-warnings 吞了警告）。
    try:
        from video_pipeline import get_ffmpeg
        extra += ["--ffmpeg-location", get_ffmpeg()]
    except Exception:
        pass
    # YouTube 对代理出口 IP 常触发机器人风控（playability ERROR / "video is
    # unavailable"）——带浏览器 cookie 可解。项目内 yt_cookies.txt 存在即自动启用。
    ck = os.path.join(BASE, "yt_cookies.txt")
    if os.path.isfile(ck):
        extra += ["--cookies", ck]
    # 新版 yt-dlp 需要 JS 运行时（默认 deno，未装则降级用本机 node）
    try:
        subprocess.run(["deno", "--version"], capture_output=True, timeout=10)
    except Exception:
        _node = r"C:\Users\Lenovo\.workbuddy\binaries\node\versions\v24.19.0\node.exe"
        if os.path.isfile(_node):
            extra += ["--js-runtimes", "node:" + _node]
    cmd = get_yt_dlp_cmd() + [
        "-f", "bestvideo+bestaudio/best",
        "--no-playlist", "--merge-output-format", "mp4",
        "--no-warnings",
        # 长视频（讲座/播客类）下载耗时较长，放宽超时
        "--socket-timeout", "30",
        "-o", tmpl,
    ] + extra + [url]
    subprocess.run(cmd, check=True, timeout=1800, env=dict(os.environ))
    picked = _pick(out_dir, None)
    # 清理 split 中间产物（yt_<id>.fNNN.mp4/.webm/.part），防止 inbox 无限膨胀。
    # 仅在已有合并成品（文件名不含 .fNNN）时才清，避免误删纯音轨兜底文件。
    vid = _video_id(url)
    merged_exists = any(
        not re.search(r"\.f\d+\.", f.name) and f.suffix.lower() in (".mp4", ".mkv")
        for f in Path(out_dir).glob(f"yt_{vid}.*") if f.name != ".part"
    ) and vid
    if merged_exists:
        for f in Path(out_dir).glob(f"yt_{vid}.*"):
            if f.name != os.path.basename(picked) and (
                    re.search(r"\.f\d+\.", f.name) or f.suffix == ".part"):
                try:
                    f.unlink()
                except Exception:
                    pass
    return picked


def _video_id(url):
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{6,})", url or "")
    return m.group(1) if m else ""


def _pick(out_dir, last_err):
    files = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    # 优先合并成品（yt_<id>.mp4），避免误选无音轨的 split（yt_<id>.fNNN.mp4）
    merged = [f for f in files if not re.search(r"\.f\d+\.", f.name)]
    files = merged + [f for f in files if re.search(r"\.f\d+\.", f.name)]
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
    subprocess.run([ff, "-y", "-i", url, "-c", "copy", out], check=True,
                   timeout=600, stdin=subprocess.DEVNULL)
    return out


def download_bilibili(url: str, out_dir: str) -> str:
    """B站链接 -> 本地 mp4（yt-dlp 原生支持，国内站不走代理）。

    - b23.tv 短链由 yt-dlp 自动跟随跳转
    - bilibili_cookies.txt 存在时自动带上（未登录也能下多数公开视频，
      登录 cookie 可解锁更高清晰度与官方字幕）
    """
    url = (url or "").strip()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "bili_%(id)s.%(ext)s")
    extra = []
    # ffmpeg 指定：否则静默跳过 bestvideo+bestaudio 合并（同 YouTube 的坑）
    try:
        from video_pipeline import get_ffmpeg
        extra += ["--ffmpeg-location", get_ffmpeg()]
    except Exception:
        pass
    ck = os.path.join(BASE, "bilibili_cookies.txt")
    if os.path.isfile(ck):
        extra += ["--cookies", ck]
    # B站为国内站，直连即可；环境代理已由 app.py 清空
    cmd = get_yt_dlp_cmd() + [
        "-f", "bestvideo+bestaudio/best",
        "--no-playlist", "--merge-output-format", "mp4",
        "--no-warnings", "--socket-timeout", "30",
        "-o", tmpl,
    ] + extra + [url]
    subprocess.run(cmd, check=True, timeout=1800, env=dict(os.environ))
    return _pick(out_dir, None)


def detect_source(url: str) -> str:
    u = (url or "").lower().strip()
    if not u:
        return "unknown"
    if "douyin.com" in u or "v.douyin" in u or "iesdouyin" in u:
        return "douyin"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if u.endswith(".m3u8") or "m3u8" in u or u.endswith(".mp4") or u.endswith(".mov") or "mp4" in u or "video" in u:
        return "direct"
    return "unknown"


def download_one(url: str, out_dir: str):
    """下载单条链接，返回 (本地路径, 错误)。"""
    src = detect_source(url)
    try:
        if src == "douyin":
            return download_douyin(url, out_dir), None
        if src == "youtube":
            return download_youtube(url, out_dir), None
        if src == "bilibili":
            return download_bilibili(url, out_dir), None
        if src == "direct":
            return download_direct(url, out_dir), None
        return None, "无法识别来源（支持：抖音链接 / YouTube链接 / B站链接 / m3u8·mp4直链 / 本地文件）：" + url
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
