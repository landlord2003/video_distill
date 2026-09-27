#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_auto.py — 用 playwright 复用已登录 cookie 抓取抖音视频直链并下载

原理：
  yt-dlp 对抖音的 DouyinIE 调 aweme/v1/web/aweme/detail/ 拿播放元数据，
  该接口现在强制要前端实时生成的 msToken（不写 cookie 库、导不出），
  所以任何静态 cookie 方案都失败。
  本脚本改用 playwright 打开视频页（注入本机导出的登录 cookie），
  由抖音前端自行生成 msToken 并完成 detail 请求，我们拦截该响应
  直接拿到 aweme_detail 里的视频直链，再用 ffmpeg 下载。
不依赖 yt-dlp 的抖音提取器，也不锁用户浏览器（用独立临时 profile，
channel="msedge" 直接复用系统已装的 Edge，无需下载 chromium）。

双用法：
  1) 命令行：python douyin_auto.py <douyin_url_or_id> [out_dir]
  2) 被导入：from douyin_auto import download_douyin_playwright
     path = download_douyin_playwright(url_or_id, out_dir)  # 失败抛 RuntimeError
"""
import os
import re
import json
import sys
import shutil
import time
import subprocess

PROJ = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(PROJ, "cookies.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0")


def _no_proxy_env():
    """返回去掉代理变量的环境副本（抖音是国内 CDN，走代理反而失败）。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    return env


def get_ffmpeg():
    """动态定位 ffmpeg：imageio-ffmpeg 自带的优先，其次 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe().replace("\\", "/")
    except Exception:
        return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or "ffmpeg"


def resolve_cookie_file(explicit=None):
    """cookie 文件解析：显式指定 > VIDEO_COOKIES 环境变量 > 项目内 cookies.txt。"""
    for c in (explicit, os.environ.get("VIDEO_COOKIES"), COOKIE_FILE):
        if c and os.path.isfile(c):
            return c
    return None


def parse_netscape(path):
    """把 Netscape cookies.txt 解析成 playwright add_cookies 需要的 dict 列表。"""
    cookies = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path_, secure, expiry, name, value = parts[:7]
            try:
                exp = int(expiry)
            except ValueError:
                exp = -1
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path_ or "/",
                "secure": secure.upper() == "TRUE",
                "expires": exp,
            })
    return cookies


def extract_video_url(aweme):
    """从 aweme_detail 里挑一条最清晰、且能直接下的视频地址。"""
    video = aweme.get("video") or {}
    # 候选字段优先级：play_addr(通常无水印) > download_addr(可能带水印) > 低码率
    for key in ("play_addr", "download_addr", "play_addr_lowbr", "play_addr_h264"):
        node = video.get(key) or {}
        urls = node.get("url_list") or []
        if urls:
            return urls[0], key
    # bit_rate 列表里也可能有
    for br in video.get("bit_rate") or []:
        urls = (br.get("play_addr") or {}).get("url_list") or []
        if urls:
            return urls[0], "bit_rate"
    return None, None


def _log(verbose, msg):
    if verbose:
        print(msg)


def _find_aweme(obj):
    """在任意嵌套结构（如 window._ROUTER_DATA）中递归查找 aweme 详情对象。"""
    if isinstance(obj, dict):
        if "aweme_id" in obj and ("video" in obj or "images" in obj
                                  or "music" in obj):
            return obj
        for v in obj.values():
            r = _find_aweme(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_aweme(v)
            if r is not None:
                return r
    return None


def fetch_direct_url(arg, cookie_file=None, verbose=True, wait_secs=60,
                     return_full=False):
    """playwright 打开视频页并拦截 aweme_detail，返回 (vid, 标题, 视频直链)。

    return_full=True 时返回完整 aweme_detail dict（供抖音音乐等场景提取
    music.play_url / 封面 / 作者 / 时长等字段）。
    失败抛 RuntimeError（含原因）。
    """
    m = re.search(r"(?:video/|note/|modal_id=)(\d{5,})", arg or "")
    if m:
        vid = m.group(1)
    else:
        vid = arg.strip() if (arg or "").strip().isdigit() else None
    if not vid:
        raise RuntimeError(f"无法从参数解析出视频ID: {arg!r}")

    cf = resolve_cookie_file(cookie_file)
    if not cf:
        raise RuntimeError(
            "缺少 cookies.txt：请先用已登录抖音的浏览器导出 Netscape 格式 cookie"
            "（放到项目目录或设 VIDEO_COOKIES 环境变量）")
    cookies = parse_netscape(cf)
    _log(verbose, f"目标视频ID: {vid} | 已加载 {len(cookies)} 个 cookie")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(f"playwright 未安装：{e}（pip install playwright）")

    detail = {}
    detail_url = {}   # 记录最近一次 detail 请求的完整 URL（含签名），供重放兜底
    retried = {"done": False}
    req_log = []
    url = f"https://www.douyin.com/video/{vid}"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--no-proxy-server", "--no-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=UA,
            ignore_https_errors=True,
        )
        # 反检测：抹掉 webdriver 标志
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        context.add_cookies(cookies)
        page = context.new_page()

        # route 代理拦截：route.fetch() 由我们主动发起，响应体一定可读，
        # 彻底避开 on_response 事后取 body 的 "navigated away" 竞态
        def on_detail_route(route):
            try:
                resp = route.fetch()
                body = resp.text()
                _log(verbose, f"  [route] detail 响应体 {len(body)} bytes")
                try:
                    j = json.loads(body)
                    aw = j.get("aweme_detail")
                    if aw:
                        detail["data"] = aw
                        _log(verbose, "  [route] 抓到 aweme_detail")
                except Exception as e:
                    _log(verbose, f"  [route] body 解析失败: {e}")
                route.fulfill(response=resp)
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        page.route("**/aweme/v1/web/aweme/detail*", on_detail_route)

        def on_request(req):
            u = req.url
            if "aweme" in u or "douyin" in u:
                req_log.append(u)
                if "aweme/v1/web/aweme/detail" in u:
                    detail_url["u"] = u
                    _log(verbose, "  [req] detail 请求已发出: " + u[:140])

        def on_response(resp):
            u = resp.url
            if "aweme/v1/web/aweme/detail" in u:
                _log(verbose, f"  [resp] detail 状态={resp.status} {u[:120]}")
                if not detail:
                    try:
                        j = resp.json()
                        aw = j.get("aweme_detail")
                        if aw:
                            detail["data"] = aw
                            _log(verbose, "  [拦截] 抓到 aweme_detail")
                        else:
                            _log(verbose, "  [resp] 无 aweme_detail 字段, body前160: "
                                 + str(j)[:160])
                    except Exception as e:
                        _log(verbose, f"  [resp] json解析失败: {e}")

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            _log(verbose, f"  页面打开异常(继续等拦截): {e}")
        # 等待拦截，期间探查登录态
        for i in range(wait_secs):
            if detail:
                break
            # SSR 兜底：页面 window._ROUTER_DATA 由服务端注入了本作品完整详情，
            # 不依赖网络拦截（note 链接会经历 /video → /note 内部导航，易竞态）
            if not detail and not retried["done"] and i >= 3:
                retried["done"] = True
                try:
                    raw = page.evaluate(
                        "() => { const d = window._ROUTER_DATA;"
                        " return d ? JSON.stringify(d) : null }")
                    if raw:
                        aw = _find_aweme(json.loads(raw))
                        if aw:
                            detail["data"] = aw
                            _log(verbose, "  [SSR] 从 _ROUTER_DATA 抓到 aweme 详情")
                        else:
                            _log(verbose, "  [SSR] _ROUTER_DATA 中未找到 aweme 详情")
                except Exception as e:
                    _log(verbose, f"  [SSR] 读取失败: {e}")
            if verbose and i % 10 == 9:
                try:
                    curl = page.url
                    txt = page.inner_text("body")[:80].replace("\n", " ")
                    has_login = page.evaluate(
                        "document.cookie.includes('sessionid') || "
                        "document.querySelector('[data-e2e=login]')===null")
                    _log(verbose, f"  [等待{i+1}s] url={curl[:60]} "
                         f"login~={has_login} text='{txt}'")
                except Exception:
                    pass
            time.sleep(1)
        browser.close()

    if not detail:
        if verbose:
            print("  调试: 共观察到", len(req_log), "条 aweme/douyin 相关请求")
            for r in req_log[:15]:
                print("    -", r[:140])
        raise RuntimeError("未拦截到 aweme_detail（可能页面被验证拦截或未登录）")

    aw = detail["data"]
    if return_full:
        return aw
    title = (aw.get("desc") or "").strip() or vid
    vurl, src = extract_video_url(aw)
    _log(verbose, f"标题: {title} | 直链来源字段: {src}")
    if not vurl:
        raise RuntimeError("aweme_detail 中无可用视频地址")
    return vid, title, vurl


def download(vurl, out_path):
    """ffmpeg 下载直链：优先带 Referer（douyinvod 需要），失败去 header 重试。"""
    env = _no_proxy_env()
    headers = f"Referer: https://www.douyin.com/\r\nUser-Agent: {UA}"
    cmd = [get_ffmpeg(), "-y", "-headers", headers, "-i", vurl, "-c", "copy", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        # 兜底：不带 header 重试
        cmd2 = [get_ffmpeg(), "-y", "-i", vurl, "-c", "copy", out_path]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300, env=env)
        return r2.returncode == 0 and os.path.exists(out_path) \
            and os.path.getsize(out_path) > 0
    except Exception:
        return False


def download_douyin_playwright(arg, out_dir, cookie_file=None, verbose=True):
    """对外主接口：抖音链接/ID -> 本地 mp4。成功返回文件路径，失败抛 RuntimeError。"""
    os.makedirs(out_dir, exist_ok=True)
    vid, title, vurl = fetch_direct_url(arg, cookie_file=cookie_file, verbose=verbose)
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:60]
    out_path = os.path.join(out_dir, f"{vid}_{safe}.mp4")
    _log(verbose, f"开始下载 -> {out_path}")
    if download(vurl, out_path):
        _log(verbose, f"✓ 下载成功: {out_path} ({os.path.getsize(out_path)//1024} KB)")
        return out_path
    raise RuntimeError("下载失败（直链可能过期或需登录）")


def main():
    if len(sys.argv) < 2:
        print("用法: python douyin_auto.py <douyin_url_or_id> [out_dir]")
        sys.exit(1)
    arg = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(PROJ, "downloads")
    try:
        download_douyin_playwright(arg, out_dir)
    except RuntimeError as e:
        print("✗", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
