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
不依赖 yt-dlp 的抖音提取器，也不锁用户浏览器（用独立临时 profile）。

用法：
  python douyin_auto.py <douyin_url_or_id> [out_dir]
"""
import os, sys, time, re, json, subprocess

PROJ = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(PROJ, "cookies.txt")
FF_BIN = os.path.join(PROJ, "venv", "Lib", "site-packages",
                      "imageio_ffmpeg", "binaries",
                      "ffmpeg-win-x86_64-v7.1.exe")

# 清掉继承的代理，避免 chromium / ffmpeg 走死代理
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)


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


def download(url, out_path):
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0")
    headers = f"Referer: https://www.douyin.com/\r\nUser-Agent: {ua}"
    # 优先带 Referer 下载（douyinvod 直链需要）
    cmd = [FF_BIN, "-y", "-headers", headers, "-i", url, "-c", "copy", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    if ok:
        return True
    # 兜底：不带 header 重试
    cmd2 = [FF_BIN, "-y", "-i", url, "-c", "copy", out_path]
    r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
    return r2.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def main():
    if len(sys.argv) < 2:
        print("用法: python douyin_auto.py <douyin_url_or_id> [out_dir]")
        sys.exit(1)
    arg = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(PROJ, "downloads")
    os.makedirs(out_dir, exist_ok=True)

    # 解析 aweme_id
    m = re.search(r"(?:video/|modal_id=)(\d{5,})", arg)
    if m:
        vid = m.group(1)
    else:
        vid = arg.strip() if arg.strip().isdigit() else None
    if not vid:
        print("无法从参数解析出视频ID:", arg)
        sys.exit(1)
    url = f"https://www.douyin.com/video/{vid}"
    print("目标视频ID:", vid, "| 打开:", url)

    if not os.path.exists(COOKIE_FILE):
        print("缺少 cookies.txt，请先用已登录抖音的浏览器导出 Netscape 格式 cookie")
        sys.exit(1)
    cookies = parse_netscape(COOKIE_FILE)
    print(f"已加载 {len(cookies)} 个 cookie")

    detail = {}
    req_log = []

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--no-proxy-server", "--no-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"),
            ignore_https_errors=True,
        )
        # 反检测：抹掉 webdriver 标志
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        context.add_cookies(cookies)
        page = context.new_page()

        def on_request(req):
            u = req.url
            if "aweme" in u or "douyin" in u:
                req_log.append(u)
                if "aweme/v1/web/aweme/detail" in u:
                    print("  [req] detail 请求已发出:", u[:140])

        def on_response(resp):
            u = resp.url
            if "aweme/v1/web/aweme/detail" in u:
                print(f"  [resp] detail 状态={resp.status} {u[:120]}")
                if not detail:
                    try:
                        j = resp.json()
                        aw = j.get("aweme_detail")
                        if aw:
                            detail["data"] = aw
                            print("  [拦截] 抓到 aweme_detail")
                        else:
                            print("  [resp] 无 aweme_detail 字段, body前160:",
                                  str(j)[:160])
                    except Exception as e:
                        print("  [resp] json解析失败:", e)

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("  页面打开异常(继续等拦截):", e)
        # 等待拦截（最多 60s），期间探查登录态
        for i in range(60):
            if detail:
                break
            if i % 10 == 9:
                try:
                    curl = page.url
                    txt = page.inner_text("body")[:80].replace("\n", " ")
                    has_login = page.evaluate(
                        "document.cookie.includes('sessionid') || "
                        "document.querySelector('[data-e2e=login]')===null")
                    print(f"  [等待{i+1}s] url={curl[:60]} login~={has_login} text='{txt}'")
                except Exception:
                    pass
            time.sleep(1)
        if not detail:
            print("  调试: 共观察到", len(req_log), "条 aweme/douyin 相关请求")
            for r in req_log[:15]:
                print("    -", r[:140])
        browser.close()

    if not detail:
        print("✗ 未拦截到 aweme_detail（可能页面被验证拦截或未登录）")
        sys.exit(1)

    aw = detail["data"]
    title = (aw.get("desc") or "").strip() or vid
    vurl, src = extract_video_url(aw)
    print(f"标题: {title}")
    print(f"视频直链来源字段: {src}")
    if not vurl:
        print("✗ aweme_detail 中无可用视频地址")
        sys.exit(1)
    print("直链(截断):", vurl[:120], "...")

    safe = re.sub(r'[\\/:*?"<>|]', "_", title)[:60]
    out_path = os.path.join(out_dir, f"{vid}_{safe}.mp4")
    print("开始下载 ->", out_path)
    if download(vurl, out_path):
        print("✓ 下载成功:", out_path, f"({os.path.getsize(out_path)//1024} KB)")
    else:
        print("✗ 下载失败（直链可能过期或需登录）")


if __name__ == "__main__":
    main()
