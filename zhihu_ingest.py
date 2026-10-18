#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知乎专栏文章采集（zhuanlan.zhihu.com/p/<id>）。

反爬现状（2026-10-18 实测，写死免得后人重踩）：
- 纯 requests GET 文章页 → 403 硬拦（650B 壳，无内容）
- API /api/v4/articles/<id>、/api/articles/<id> → 403（需 zse-96 签名）
- r.jina.ai 中转 → 本机网络代理拦截，不通
- 浏览器渲染（crawl4ai headless chromium）→ HTTP 状态仍报 403，但返回的
  SSR HTML（~432KB）内嵌 js-initialData，含标题/作者/赞同数/正文 HTML，
  正文完整可用。crawl4ai 会打印 "Blocked by anti-bot" 日志，属误报，忽略。

因此流程 = 浏览器渲染 → initialData 提取 → 自写 HTML→MD（知乎子集）
→ zhimg 图片本地化（md5 命名，Referer 伪装）。
"""
import asyncio
import hashlib
import html as _html
import json
import os
import re
import time

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------- ① 浏览器渲染取 HTML ----------
def fetch_html(url, timeout=90):
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    conf = BrowserConfig(
        browser_type="chromium", headless=True,
        extra_args=["--no-sandbox", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled"])
    run = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS, page_timeout=timeout * 1000,
        wait_until="networkidle", delay_before_return_html=3.0)
    async def _run():
        async with AsyncWebCrawler(config=conf) as c:
            return await c.arun(url, config=run)
    r = asyncio.run(_run())
    return r.html or ""


# ---------- ② js-initialData 提取 ----------
def parse_initial(html):
    m = re.search(r'<script id="js-initialData" type="text/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return None
    j = json.loads(m.group(1))
    # 实测两种形态：顶层 entities（部分页面）/ initialState.entities（zhuanlan 现行）
    ents = (j.get("entities") or (j.get("initialState") or {}).get("entities") or {})
    arts = ents.get("articles") or {}
    for _, a in arts.items():
        return {
            "id": str(a.get("id") or ""),
            "title": (a.get("title") or "").strip(),
            "author": ((a.get("author") or {}).get("name") or "").strip(),
            "voteup": a.get("voteupCount") or 0,
            "created": a.get("created") or 0,
            "updated": a.get("updated") or 0,
            "content_html": a.get("content") or "",
            "excerpt": (a.get("excerpt") or "").strip(),
        }
    return None


# ---------- ③ 知乎 HTML → Markdown ----------
def _inl(s):
    """行内标签 → Markdown（a/b/i/code，其余剥壳）。"""
    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
               lambda m: "[%s](%s)" % (_txt(m.group(2)), m.group(1)),
               s, flags=re.S)
    s = re.sub(r'<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>', r'**\1**', s, flags=re.S)
    s = re.sub(r'<(?:i|em)[^>]*>(.*?)</(?:i|em)>', r'*\1*', s, flags=re.S)
    s = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', s, flags=re.S)
    s = re.sub(r'<sup[^>]*>(.*?)</sup>', r'^\1', s, flags=re.S)
    s = _txt(s)
    return s


def _txt(s):
    """剥全部标签 + 实体还原。"""
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _html2md(h):
    h = h or ""
    # 图片 figure（知乎：data-original 全图 > data-actual-src > src）
    def _img(m):
        tag = m.group(0)
        sm = (re.search(r'data-original="([^"]+)"', tag)
              or re.search(r'data-actual-src="([^"]+)"', tag)
              or re.search(r'src="([^"]+)"', tag))
        return "\n\n![](%s)\n\n" % sm.group(1) if sm else "\n\n"
    h = re.sub(r"<figure[^>]*>.*?</figure>", _img, h, flags=re.S)
    h = re.sub(r"</?figure[^>]*>", "\n\n", h)
    # 引用块
    def _bq(m):
        inner = re.sub(r"<br\s*/?>|</p>", "\n", m.group(1))
        lines = [_inl(x).strip() for x in re.split(r"\n", inner)]
        lines = [x for x in lines if x]
        return "\n\n" + "\n".join("> " + x for x in lines) + "\n\n"
    h = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", _bq, h, flags=re.S)
    # 列表（知乎 li 内常包 <p>，交给 _inl 剥壳）
    def _lst(m, ordered):
        items = re.findall(r"<li[^>]*>(.*?)</li>", m.group(1), flags=re.S)
        out = []
        for i, it in enumerate(items, 1):
            t = _inl(it)
            if t:
                out.append(("%d. %s" % (i, t)) if ordered else ("- " + t))
        return "\n\n" + "\n".join(out) + "\n\n"
    h = re.sub(r"<ol[^>]*>(.*?)</ol>", lambda m: _lst(m, True), h, flags=re.S)
    h = re.sub(r"<ul[^>]*>(.*?)</ul>", lambda m: _lst(m, False), h, flags=re.S)
    # 标题
    for lv in (1, 2, 3):
        h = re.sub(r"<h%d[^>]*>(.*?)</h%d>" % (lv, lv),
                   lambda m, lv=lv: "\n\n%s %s\n\n" % ("#" * lv, _inl(m.group(1))),
                   h, flags=re.S)
    h = re.sub(r"<hr\s*/?>", "\n\n---\n\n", h)
    h = re.sub(r"<br\s*/?>", "\n\n", h)
    # 段落
    h = re.sub(r"<p[^>]*>(.*?)</p>", lambda m: "\n\n" + _inl(m.group(1)) + "\n\n",
               h, flags=re.S)
    h = _txt(h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


# ---------- ④ zhimg 图片本地化 ----------
def _localize_images(md, img_dir):
    """下载 zhimg 图片替换为相对路径；失败保留外链。返回 (新md, 张数)。"""
    if not md or not img_dir:
        return md, 0
    os.makedirs(img_dir, exist_ok=True)
    base = os.path.dirname(os.path.dirname(img_dir))  # <vault> 根
    n = 0

    def _dl(m):
        nonlocal n
        url = m.group(2)
        if "zhimg.com" not in url:
            return m.group(0)
        try:
            r = requests.get(url, timeout=30, stream=True, headers={
                "User-Agent": _UA, "Referer": "https://www.zhihu.com/"})
            data = r.content
            if len(data) < 1000:      # 防盗链占位图
                return m.group(0)
            ext = ".png" if data[:8].startswith(b"\x89PNG") else \
                  (".gif" if data[:3] == b"GIF" else (".webp" if data[8:12] == b"WEBP" else ".jpg"))
            name = hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + ext
            dest = os.path.join(img_dir, name)
            if not os.path.exists(dest):
                with open(dest, "wb") as f:
                    f.write(data)
            n += 1
            return "![%s](%s)" % (m.group(1),
                                  os.path.relpath(dest, base).replace("\\", "/"))
        except Exception:
            return m.group(0)

    md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _dl, md)
    return md, n


# ---------- 入口 ----------
def ingest_article(url, img_dir=None):
    url = (url or "").strip()
    if not re.search(r"zhihu\.com/p/\d+", url):
        raise RuntimeError("知乎适配仅支持专栏文章链接 zhuanlan.zhihu.com/p/<id>")
    html = fetch_html(url)
    art = parse_initial(html)
    if not art or not art.get("content_html"):
        raise RuntimeError("页面未返回文章数据（initialData 缺失），可能被知乎风控，稍后重试或更新 cookie")
    md = _html2md(art["content_html"])
    n_img = 0
    if img_dir:
        md, n_img = _localize_images(md, img_dir)
    # 元信息头
    created = time.strftime("%Y-%m-%d", time.localtime(art["created"])) if art.get("created") else ""
    meta_lines = ["# " + (art["title"] or "知乎文章"), ""]
    sub = []
    if art.get("author"):
        sub.append("作者：" + art["author"])
    if created:
        sub.append("发布：" + created)
    if art.get("voteup"):
        sub.append("赞同 %d" % art["voteup"])
    sub.append("链接：" + url)
    meta_lines.append(" | ".join(sub))
    meta_lines += ["", md]
    if n_img:
        meta_lines += ["", "> 图片已本地化 %d 张" % n_img]
    return {"platform": "zhihu", "title": art["title"] or "知乎文章",
            "md": "\n".join(meta_lines), "url": url,
            "author": art.get("author", ""), "voteup": art.get("voteup", 0)}


if __name__ == "__main__":
    import sys
    out = ingest_article(sys.argv[1], img_dir=sys.argv[2] if len(sys.argv) > 2 else None)
    print("title:", out["title"])
    print("author:", out["author"], "| voteup:", out["voteup"])
    print("md len:", len(out["md"]))
    print("--- 开头 ---")
    print(out["md"][:800])
