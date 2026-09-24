#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文章/笔记采集统一入口（供 app.py 调用）。

- 公众号文章：wechat_fetch.py（vendor 自 chubbyskills/wechat-article-ingest，MIT）
- 小红书笔记：xhs_fetch.py（vendor 自 chubbyskills/xiaohongshu-ingest，MIT）
  视频笔记不转录（转写走本工具视频流水线，faster-whisper），只保留视频链接。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def detect_platform(url: str):
    """按 URL 推断平台：wechat / xhs / None。"""
    u = (url or "").lower().strip()
    if not u:
        return None
    if "mp.weixin.qq.com" in u or "weixin.qq.com/s" in u:
        return "wechat"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "xhs"
    return None


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "").strip()
    return (name or "untitled")[:80]


def ingest_wechat(url: str) -> dict:
    """公众号文章 → Markdown。返回 {title, md}。"""
    import wechat_fetch
    title, author, content = wechat_fetch.fetch_from_url(url)
    if not title and not content:
        raise RuntimeError("抓取失败：页面可能需要验证或链接无效（也可确认文章是否仅限微信内打开）")
    md = wechat_fetch.generate_markdown(title or "公众号文章", author or "", content or "", url)
    return {"title": (title or "公众号文章").strip(), "md": md}


def ingest_xhs(url: str, cookie: str = None) -> dict:
    """小红书笔记 → Markdown（图文优先；视频笔记保留视频链接不转录）。"""
    import xhs_fetch
    cookie = cookie or os.environ.get("XHS_COOKIE") or ""
    try:
        page, final_url = xhs_fetch.http_get(url, cookie)
    except Exception as e:
        raise RuntimeError(f"请求失败：{e}")
    if "请通过小红书" in page or "verify" in (final_url or "").lower():
        raise RuntimeError("被小红书风控拦截：请在环境变量 XHS_COOKIE 配置浏览器 cookie 后重试")
    data = None
    state = xhs_fetch.extract_initial_state(page)
    if state:
        note = xhs_fetch.find_note(state)
        if note:
            data = xhs_fetch.parse_from_state(note)
    if not data or not (data.get("desc") or data.get("title")):
        data = xhs_fetch.parse_from_meta(page) or data
    if not data or not (data.get("desc") or data.get("title")):
        raise RuntimeError("无法解析笔记内容（页面结构变化或被拦截）。建议设置 XHS_COOKIE 后重试")
    title = xhs_fetch.compute_title(data)
    # 图片保留外链（不落盘），避免 vault 体积膨胀
    image_refs = [("url", u) for u in data.get("images") or []]
    md = xhs_fetch.build_markdown(data, final_url or url, title, image_refs, None)
    return {"title": title, "md": md, "note_type": data.get("note_type", "image")}


def ingest(url: str, platform: str = "") -> dict:
    """统一入口。platform 缺省时按 URL 自动推断。返回 {platform, title, md}。"""
    url = (url or "").strip()
    if not url:
        raise RuntimeError("url 不能为空")
    plat = (platform or "").strip() or detect_platform(url)
    if plat == "wechat":
        out = ingest_wechat(url)
    elif plat == "xhs":
        out = ingest_xhs(url)
    else:
        raise RuntimeError("无法识别平台（支持：mp.weixin.qq.com 公众号文章 / xiaohongshu.com·xhslink.com 小红书笔记）")
    out["platform"] = plat
    out["filename"] = _sanitize(out["title"]) + ".md"
    return out
