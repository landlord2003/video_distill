#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_comments.py — 抖音单视频评论采集（借鉴 MediaCrawler 的评论接口设计）

接口：aweme/v1/web/comment/list/（一级评论，cursor 分页，has_more 标记）
     二级回复（comment/list/reply）本次只记录 reply_comment_total 计数，
     需要时可按同骨架扩展。

采集策略（与 douyin_auto / douyin_profile 同一血脉）：
  1. playwright 打开视频页，让抖音前端自己带签名（msToken/X-Bogus）发评论请求，
     拦截响应收集评论 + 记录首屏签名 URL 模板；
  2. 页内 fetch 重放签名 URL 翻页（cursor 链，服务端不校验旧签名，
     已在合集/主页采集实测可行的同一突破）；
  3. 无头失败转 CDP 真实浏览器兜底（douyin_cdp，真实指纹+持久登录态）。

输出：{"video_id","title","count","comments":[
        {"cid","user","text","likes","time","replies"}]}
评论缓存落盘 comments/<vid>.json（运行时产物，不入 git）。
"""
import os
import re
import json
import time
from datetime import datetime

from douyin_auto import resolve_aweme_id

PROJ = os.path.dirname(os.path.abspath(__file__))
COMMENTS_DIR = os.path.join(PROJ, "comments")


def _fmt_time(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _parse_comment(c):
    """评论 API 单条 -> 精简结构。"""
    user = c.get("user") or {}
    return {
        "cid": str(c.get("cid") or ""),
        "user": (user.get("nickname") or "").strip(),
        "text": (c.get("text") or "").strip(),
        "likes": int(c.get("digg_count") or 0),
        "time": _fmt_time(c.get("create_time")),
        "replies": int(c.get("reply_comment_total") or 0),
    }


def fetch_comments(video_arg, cookie_file=None, verbose=True, max_count=200,
                   wait_secs=30, cdp_fallback=True):
    """抖音视频链接/ID -> 评论列表 dict。失败抛 RuntimeError。

    max_count：最多采集条数（一级评论；API 单页 ~20 条）。
    """
    vid = resolve_aweme_id(video_arg)
    if not vid:
        raise RuntimeError(
            f"无法从参数解析出作品ID: {video_arg!r}。"
            "支持 /video/<id>、modal_id=<id>、纯数字 id、v.douyin.com 短链")
    try:
        return _fetch_impl(vid, cookie_file, verbose, max_count, wait_secs,
                           cdp=False)
    except RuntimeError as e:
        # 精确判决不重试；只有「一条评论请求都没拦到」类降级才换真浏览器
        if not cdp_fallback or "明确拒绝" in str(e):
            raise
        if verbose:
            print("[comments] 无头未取到评论，疑似风控降级 -> 转 CDP 真实浏览器重试")
        return _fetch_impl(vid, cookie_file, verbose, max_count, wait_secs,
                           cdp=True)


def _fetch_impl(vid, cookie_file=None, verbose=True, max_count=200,
                wait_secs=30, cdp=False):
    from douyin_auto import UA, resolve_cookie_file, parse_netscape

    cf = resolve_cookie_file(cookie_file)
    cookies = parse_netscape(cf) if cf else []
    if verbose:
        print(f"[comments] 目标: {vid} | 模式: "
              f"{'CDP真实浏览器' if cdp else '无头'} | cookie: {len(cookies)} 个")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(f"playwright 未安装：{e}")

    comments = {}        # cid -> 精简结构（保 API 返回顺序）
    title = {"v": ""}
    state = {"has_more": 1, "cursor": 0}
    req_log = []
    template_url = {"v": ""}   # 首屏评论接口完整 URL（含签名）
    url = f"https://www.douyin.com/video/{vid}"

    with sync_playwright() as p:
        if cdp:
            import douyin_cdp
            browser = douyin_cdp.connect(p, verbose=verbose)
            context = douyin_cdp.default_context(browser)
        else:
            browser = p.chromium.launch(
                channel="msedge", headless=True,
                args=["--no-proxy-server", "--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(user_agent=UA,
                                          ignore_https_errors=True,
                                          viewport={"width": 1380, "height": 900})
            if cookies:
                context.add_cookies(cookies)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
        page = context.new_page()

        def on_response(resp):
            u = resp.url
            # 一级评论接口；reply 子接口另算（本次不递归抓）
            if "/comment/list/" not in u or "/reply/" in u:
                return
            req_log.append(u[:140])
            if not template_url["v"]:
                template_url["v"] = u
            try:
                j = resp.json()
            except Exception:
                return
            for c in j.get("comments") or []:
                item = _parse_comment(c)
                cid = item["cid"]
                if cid and cid not in comments:
                    comments[cid] = item
            state["has_more"] = int(j.get("has_more") or 0)
            if j.get("cursor") is not None:
                state["cursor"] = j.get("cursor")

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            if verbose:
                print(f"[comments] 页面打开异常(继续等拦截): {e}")

        t0 = time.time()
        # 等首屏评论请求落地（评论区在页面下方，偶尔要点一下播放器触发渲染）
        for i in range(wait_secs):
            if comments:
                break
            if i == 10:
                try:
                    page.mouse.wheel(0, 1600)
                except Exception:
                    pass
            time.sleep(1)
        try:
            t = page.title() or ""
            if t:
                title["v"] = re.sub(r" - 抖音.*|的主页.*|的抖音.*", "", t).strip()
        except Exception:
            pass

        # ---------- 页内 fetch 重放翻页（cursor 链） ----------
        fetch_err = ""
        if template_url["v"] and len(comments) < max_count and state["has_more"]:
            budget_ms = 90_000
            try:
                items_js = page.evaluate(
                    """async ([u0, maxCount, timeBudgetMs]) => {
                        const out = [];
                        const seen = new Set();
                        const t0 = Date.now();
                        let cursor = '0', hasMore = true, guard = 0;
                        const u = new URL(u0);
                        while (hasMore && guard < 100) {
                            guard++;
                            if (Date.now() - t0 > timeBudgetMs) break;
                            u.searchParams.set('cursor', cursor);
                            let j;
                            try {
                                const resp = await fetch(u.toString(), {credentials: 'include'});
                                j = await resp.json();
                            } catch (e) { break; }
                            const list = j.comments || [];
                            for (const c of list) {
                                const cid = String(c.cid || '').trim();
                                if (cid && !seen.has(cid)) {
                                    seen.add(cid);
                                    const usr = c.user || {};
                                    out.push({cid: cid,
                                              user: (usr.nickname || '').trim(),
                                              text: (c.text || '').trim(),
                                              likes: c.digg_count || 0,
                                              ts: c.create_time || 0,
                                              replies: c.reply_comment_total || 0});
                                }
                            }
                            hasMore = !!j.has_more;
                            const mc = j.cursor;
                            if (!list.length || mc === null || mc === undefined) break;
                            cursor = String(mc);
                            if (maxCount && out.length >= maxCount) break;
                            await new Promise(r => setTimeout(r, 1000 + Math.random() * 600));
                        }
                        return out;
                    }""",
                    [template_url["v"], max_count, budget_ms])
                for it in items_js or []:
                    if it["cid"] not in comments:
                        comments[it["cid"]] = {
                            "cid": it["cid"], "user": it["user"],
                            "text": it["text"], "likes": int(it["likes"] or 0),
                            "time": _fmt_time(it["ts"]),
                            "replies": int(it["replies"] or 0),
                        }
                if verbose:
                    print(f"[comments] 页内翻页采集 {len(comments)} 条")
            except Exception as e:
                fetch_err = f"{type(e).__name__}: {str(e)[:120]}"
                if verbose:
                    print(f"[comments] 页内翻页异常: {fetch_err}")

        try:
            page.close()
        except Exception:
            pass
        if not cdp:
            try:
                browser.close()
            except Exception:
                pass

    if not comments:
        hint = (f"共观察到 {len(req_log)} 条评论请求" if req_log
                else "一条评论请求都没拦到（可能被登录墙/风控拦截，或作品无评论区）")
        raise RuntimeError(f"未采集到任何评论（{hint}）。"
                           "请确认 cookies.txt 有效，稍后重试。")

    items = list(comments.values())[:max_count]
    out = {"video_id": vid,
           "title": title["v"] or f"抖音视频{vid}",
           "count": len(items), "comments": items}
    # 缓存落盘（运行时产物）
    try:
        os.makedirs(COMMENTS_DIR, exist_ok=True)
        with open(os.path.join(COMMENTS_DIR, f"{vid}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        if verbose:
            print(f"[comments] 缓存落盘失败(不影响返回): {e}")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python douyin_comments.py <抖音链接或视频ID> [max_count]")
        sys.exit(1)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    out = fetch_comments(sys.argv[1], verbose=True, max_count=n)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])
