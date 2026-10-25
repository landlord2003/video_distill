#!/usr/bin/env python3
"""回填 Twitter 笔记里的 twimg 外链图 → 本地化（vault/images/twitter/）。

背景：twitter_ingest 图片本地化依赖代理；代理不可用时回落外链，
而 pbs.twimg.com 被墙，笔记里就成了死图。本脚本事后补下载+改写链接。

下载通道（依次尝试）：
  ① 代理（video_downloader._get_proxy，需代理软件开着）
  ② wsrv.nl 公共图片代理（服务端代取，部分网络可达）
  ③ 直连（基本必挂，保留兜底）

用法：
  python backfill_tw_images.py            # 扫描 vault 全部文章笔记
  python backfill_tw_images.py <md路径>   # 只处理单个文件
"""
import os
import re
import sys
import io
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

VAULT = os.environ.get("VIDEO_VAULT", r"E:\Workbuddy\Claw")
NOTES_DIR = os.path.join(VAULT, "08-文章笔记")
IMG_DIR = os.path.join(VAULT, "images", "twitter")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
IMG_RE = re.compile(r"(!\[[^\]]*\]\()(https://pbs\.twimg\.com/media/[^)\s]+)(\))")


def _fetch(url, proxy=None, timeout=25):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://x.com/",
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _download(img_url):
    """返回 (bytes, 通道名) 或 (None, 失败原因)。"""
    # 统一成 twimg 规范参数形态
    if "?" not in img_url:
        img_url = img_url + "?format=jpg&name=large"
    # ① 代理
    proxy = ""
    try:
        import video_downloader as vdl
        proxy = vdl._get_proxy() or ""
    except Exception:
        proxy = ""
    if proxy:
        try:
            return _fetch(img_url, proxy=proxy), "proxy"
        except Exception:
            pass
    # ② wsrv.nl（内层 query 需 URL 编码）
    try:
        from urllib.parse import quote
        inner = quote(img_url, safe="")
        return _fetch("https://images.weserv.nl/?url=" + inner), "wsrv"
    except Exception:
        pass
    # ③ 直连
    try:
        return _fetch(img_url), "direct"
    except Exception:
        pass
    return None, "all-channels-failed"


def _media_id(url):
    m = re.search(r"/media/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else "img"


def process_md(path):
    with io.open(path, "r", encoding="utf-8") as f:
        md = f.read()
    links = IMG_RE.findall(md)
    if not links:
        return 0, 0
    os.makedirs(IMG_DIR, exist_ok=True)
    ok = fail = 0
    for alt, url, close in links:
        mid = _media_id(url)
        ext = ".jpg"
        m = re.search(r"\.(jpe?g|png|webp|gif)(?:[?!]|$)", url, re.I)
        if m:
            ext = "." + m.group(1).lower()
        dest = os.path.join(IMG_DIR, mid + ext)
        rel = os.path.relpath(dest, VAULT).replace("\\", "/")
        if os.path.isfile(dest) and os.path.getsize(dest) > 1024:
            new_link = f"{alt}{rel}{close}"   # 已有本地文件，只改写链接
        else:
            data, how = _download(url)
            if not data or len(data) <= 1024:
                print(f"  ✗ {mid} 下载失败（{how}）")
                fail += 1
                continue
            with open(dest, "wb") as f:
                f.write(data)
            print(f"  ✓ {mid} <- {how} ({len(data)//1024}KB)")
            new_link = f"{alt}{rel}{close}"
        md = md.replace(f"{alt}{url}{close}", new_link, 1)
        ok += 1
    if ok:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(md)
    return ok, fail


def main():
    targets = sys.argv[1:]
    if not targets:
        targets = [os.path.join(NOTES_DIR, f) for f in os.listdir(NOTES_DIR)
                   if f.endswith(".md")]
    total_ok = total_fail = 0
    for p in targets:
        if not os.path.isfile(p) or not p.endswith(".md"):
            continue
        with io.open(p, "r", encoding="utf-8") as f:
            if "pbs.twimg.com" not in f.read():
                continue
        print("==", os.path.basename(p)[:50])
        ok, fail = process_md(p)
        total_ok += ok
        total_fail += fail
    print(f"完成：本地化 {total_ok} 张，失败 {total_fail} 张（失败者保持外链，代理可用后重跑即可）")


if __name__ == "__main__":
    main()
