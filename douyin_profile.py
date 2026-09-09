#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_profile.py — 抖音用户主页采集：进入主页，滚动加载，拦截作品列表接口，
产出该账号的视频链接清单（先出清单人工过目，确认后再走现有批量整理流水线）。

原理（与 douyin_auto.py 同一套路，从"拦单条 detail"扩到"拦列表 post"）：
  yt-dlp / 静态请求都过不了抖音前端签名（msToken/X-Bogus），
  playwright 用登录 cookie 打开主页，让抖音前端自己发
  aweme/v1/web/aweme/post/ 列表请求，我们拦响应，滚动翻页持续收集。

输入支持：
  - https://www.douyin.com/user/MS4wLjABAAAAxxxx?...（主页链接，带参数也行）
  - https://v.douyin.com/xxxx/（分享短链，自动跟随重定向拿真实地址）
输出：{"nickname": 昵称, "videos": [{"id","title","url"}...]}
"""
import os
import re
import time
import random
import urllib.request

from douyin_auto import (UA, _no_proxy_env, resolve_cookie_file, parse_netscape)

SEC_UID_RE = re.compile(r"/user/([A-Za-z0-9_-]{20,})")


def resolve_profile_url(arg: str) -> str:
    """把输入规整成 https://www.douyin.com/user/<sec_uid> 形式。
    支持 v.douyin.com 短链（跟随重定向）。解析不出 sec_uid 抛 RuntimeError。
    """
    u = (arg or "").strip()
    m = SEC_UID_RE.search(u)
    if m:
        return f"https://www.douyin.com/user/{m.group(1)}"
    if "v.douyin.com" in u:
        # 短链：跟随 30x 拿最终地址（国内 CDN，去代理）
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                final = resp.geturl()
            m = SEC_UID_RE.search(final)
            if m:
                return f"https://www.douyin.com/user/{m.group(1)}"
            raise RuntimeError(f"短链跳转后不是用户主页: {final[:120]}")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"短链解析失败: {type(e).__name__}: {e}")
    raise RuntimeError(
        "无法识别抖音主页链接：需要形如 https://www.douyin.com/user/MS4wLjAB... 的主页地址"
        "（抖音 App 里进对方主页 → 右上角分享 → 复制链接，可直接粘贴分享文本）")


def fetch_profile_videos(profile_arg, cookie_file=None, verbose=True,
                         max_count=50, max_scrolls=60, deadline_secs=240):
    """主页链接 -> {"nickname", "videos":[{"id","title","url"}]}。
    失败抛 RuntimeError（含原因与已收集数量）。
    """
    profile_url = resolve_profile_url(profile_arg)

    cf = resolve_cookie_file(cookie_file)
    cookies = parse_netscape(cf) if cf else []
    if verbose:
        print(f"[profile] 目标: {profile_url} | cookie: "
              f"{len(cookies)} 个" + ("" if cf else "（未找到 cookies.txt，无登录态，大概率抓不全）"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(f"playwright 未安装：{e}")

    videos = {}          # aweme_id -> {"id","title","url"}
    nickname = {"v": ""}
    list_state = {"has_more": 1, "cursor": -1}
    req_log = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge", headless=True,
            args=["--no-proxy-server", "--no-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=UA, ignore_https_errors=True,
                                      viewport={"width": 1380, "height": 900})
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()

        def on_response(resp):
            u = resp.url
            if "aweme/post/" not in u and "aweme/favorite/" not in u:
                return
            req_log.append(u[:140])
            try:
                j = resp.json()
            except Exception:
                return
            aw_list = j.get("aweme_list") or []
            for aw in aw_list:
                vid = str(aw.get("aweme_id") or "").strip()
                if not vid:
                    continue
                if vid not in videos:
                    title = (aw.get("desc") or "").strip()
                    videos[vid] = {
                        "id": vid,
                        "title": title,
                        "url": f"https://www.douyin.com/video/{vid}",
                    }
            list_state["has_more"] = int(j.get("has_more") or 0)
            list_state["cursor"] = j.get("max_cursor", -1)

        def on_dom():
            try:
                t = page.title() or ""
                if t:
                    nickname["v"] = re.sub(r"的主页.*|的抖音.*| - 抖音.*", "", t).strip()
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if verbose:
                print(f"[profile] 页面打开异常(继续等拦截): {e}")

        t0 = time.time()
        empty_rounds = 0
        scrolls = 0
        # 首屏等待：列表接口一般 3~8s 内发出
        while time.time() - t0 < deadline_secs:
            on_dom()
            if videos:
                break
            if time.time() - t0 > 25:
                break
            time.sleep(1)

        while time.time() - t0 < deadline_secs and scrolls < max_scrolls:
            if max_count and len(videos) >= max_count:
                break
            if list_state["has_more"] == 0 and videos:
                break
            before = len(videos)
            # 模拟真人滚动：鼠标滚轮 + 随机步长
            try:
                page.mouse.move(random.uniform(300, 900), random.uniform(200, 600))
                page.mouse.wheel(0, random.randint(2400, 4200))
            except Exception:
                try:
                    page.evaluate("window.scrollBy(0, 3200)")
                except Exception:
                    pass
            scrolls += 1
            # 等本页新列表响应落地
            for _ in range(14):
                time.sleep(0.5)
                if len(videos) > before:
                    break
            gained = len(videos) - before
            empty_rounds = empty_rounds + 1 if gained == 0 else 0
            if verbose:
                print(f"[profile] 滚动#{scrolls} +{gained} 共{len(videos)} "
                      f"has_more={list_state['has_more']} 空转{empty_rounds}")
            if empty_rounds >= 6:
                break

        on_dom()
        browser.close()

    if not videos:
        hint = (f"共观察到 {len(req_log)} 条列表请求" if req_log
                else "一条作品列表请求都没拦到（页面可能被登录墙/验证码拦截）")
        raise RuntimeError(
            f"未采集到任何视频（{hint}）。"
            "请确认 cookies.txt 来自已登录抖音的浏览器且未过期，稍后重试。")

    items = list(videos.values())
    for it in items:
        if not it["title"]:
            it["title"] = f"视频 {it['id']}"
    if max_count:
        items = items[:max_count]
    return {"nickname": nickname["v"] or "未知昵称",
            "profile": profile_url,
            "count": len(items), "videos": items}


if __name__ == "__main__":
    import sys as _sys
    import json as _json
    if len(_sys.argv) < 2:
        print("用法: python douyin_profile.py <主页链接或短链> [max_count]")
        _sys.exit(1)
    n = int(_sys.argv[2]) if len(_sys.argv) > 2 else 50
    out = fetch_profile_videos(_sys.argv[1], verbose=True, max_count=n)
    print(_json.dumps(out, ensure_ascii=False, indent=2))
