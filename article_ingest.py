#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文章/笔记采集统一入口（供 app.py 调用）。

- 公众号文章：wechat_fetch.py（vendor 自 chubbyskills/wechat-article-ingest，MIT）
- 小红书笔记：xhs_fetch.py（vendor 自 chubbyskills/xiaohongshu-ingest，MIT）
  视频笔记不转录（转写走本工具视频流水线，faster-whisper），只保留视频链接。
- Twitter/X 推文：twitter_ingest.py（fxtwitter 等免 cookie 通道，正文+图片本地化）
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def detect_platform(url: str):
    """按 URL 推断平台：wechat / xhs / twitter / None。"""
    u = (url or "").lower().strip()
    if not u:
        return None
    if "mp.weixin.qq.com" in u or "weixin.qq.com/s" in u:
        return "wechat"
    if "xiaohongshu.com" in u or "xhslink.com" in u:
        return "xhs"
    # Twitter/X 推文（含 t.co 不在此列——t.co 短链无法本地判断）
    if ("twitter.com" in u or "x.com" in u) and "/status" in u:
        return "twitter"
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


def ingest_xhs(url: str, cookie: str = None, img_dir: str = None) -> dict:
    """小红书笔记 → Markdown（图文优先；视频笔记保留视频链接不转录）。

    内容加工：
      - 清理正文里的 #话题[话题]# 标记（标签已在 frontmatter，不重复）
      - 图片下载到本地 img_dir（小红书 CDN 外链带时效 token，会过期），
        Markdown 内嵌本地相对路径，失败自动回落为外链
    """
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
    data["desc"] = _clean_xhs_desc(data.get("desc") or "")
    # 图片：优先下载到本地，失败回落外链
    images = data.get("images") or []
    image_refs = _localize_xhs_images(images, title, img_dir)
    md = xhs_fetch.build_markdown(data, final_url or url, title, image_refs, None)
    # 兜底：外链图片也渲染成 markdown 图片（而非裸 URL 列表）
    md = re.sub(r"^- (https?://\S+)$", r"![图](\1)", md, flags=re.M)
    # 图片型笔记：正文文字全在图里（desc 过短）→ 本地 qwen3-vl OCR 补文字
    ocr_info = ""
    if (img_dir and data.get("note_type", "image") != "video"
            and len(data.get("desc") or "") < 120):
        vault_root = os.path.dirname(os.path.dirname(img_dir))
        local_paths = [os.path.join(vault_root, v.replace("/", os.sep))
                       for k, v in image_refs if k == "local"]
        if local_paths:
            try:
                import vision_ocr
                texts = vision_ocr.ocr_images(local_paths)
                got = [(i + 1, t) for i, t in enumerate(texts) if t.strip()]
                if got:
                    seg = ["", "## 📝 图片文字识别（本地 qwen3-vl）", ""]
                    seg += [f"**图 {n}**\n\n{t}\n" for n, t in got]
                    ocr_md = "\n".join(seg).rstrip() + "\n"
                    md = md.replace("（正文为空，可能被反爬拦截，建议配置 XHS_COOKIE）",
                                    "（正文为空，文字内容见图内 OCR）")
                    if "\n## 图片" in md:
                        md = md.replace("\n## 图片", "\n" + ocr_md + "\n## 图片", 1)
                    else:
                        md = md.rstrip() + "\n\n" + ocr_md
                    ocr_info = f"图片型笔记已 OCR {len(got)}/{len(local_paths)} 张（本地 qwen3-vl）"
            except Exception as _oe:
                ocr_info = f"OCR 失败（不阻断）：{str(_oe)[:100]}"
    return {"title": title, "md": md, "note_type": data.get("note_type", "image"),
            "ocr": ocr_info}


def _clean_xhs_desc(desc: str) -> str:
    """正文清洗：去话题标记/零宽字符/多余空行/营销尾部。"""
    s = desc or ""
    s = re.sub(r"#[^#\n]{1,40}?\[话题\]#", "", s)          # #xxx[话题]#
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)        # 零宽字符
    s = re.sub(r"\n{3,}", "\n\n", s)                        # 压缩空行
    s = re.sub(r"[ \t]+\n", "\n", s)                        # 行尾空白
    return s.strip()


def _localize_xhs_images(images, title: str, img_dir: str):
    """下载图片到 img_dir，返回 [(kind, value)]：本地成功用 ('local', 相对路径)，
    失败回落 ('url', 原始外链)。img_dir 为空时全部保留外链。"""
    import urllib.request
    refs = []
    if not images:
        return refs
    slug = _sanitize(title)[:40] or "note"
    folder = None
    if img_dir:
        try:
            os.makedirs(img_dir, exist_ok=True)
            folder = img_dir
        except OSError:
            folder = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i, u in enumerate(images, 1):
        ok_local = False
        if folder and u:
            ext = ".jpg"
            m = re.search(r"\.(jpe?g|png|webp|gif)(?:[?!]|$)", u, re.I)
            if m:
                ext = "." + m.group(1).lower()
            dest = os.path.join(folder, f"{slug}-{i:02d}{ext}")
            try:
                req = urllib.request.Request(u, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36",
                    "Referer": "https://www.xiaohongshu.com/",
                })
                with opener.open(req, timeout=20) as r, open(dest, "wb") as f:
                    f.write(r.read())
                if os.path.getsize(dest) > 1024:   # <1KB 视为失败（风控空响应）
                    ok_local = True
                else:
                    os.remove(dest)
            except Exception:
                if os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
        if ok_local:
            # vault 内相对路径（img_dir 形如 <vault>/images/xhs，vault 根为其上两级，
            # 这样 Obsidian 相对 md 目录与记录中心 Web 路由 /images/... 两边都能解析）
            rel = os.path.relpath(
                dest, os.path.dirname(os.path.dirname(img_dir))).replace("\\", "/")
            refs.append(("local", rel))
        else:
            refs.append(("url", u))
    return refs


def ingest(url: str, platform: str = "", img_dir: str = None) -> dict:
    """统一入口。platform 缺省时按 URL 自动推断。返回 {platform, title, md}。"""
    url = (url or "").strip()
    if not url:
        raise RuntimeError("url 不能为空")
    plat = (platform or "").strip() or detect_platform(url)
    if plat == "wechat":
        out = ingest_wechat(url)
    elif plat == "xhs":
        out = ingest_xhs(url, img_dir=img_dir)
    elif plat == "twitter":
        import twitter_ingest as twi
        out = twi.ingest_tweet(url, img_dir=img_dir)
    else:
        raise RuntimeError("无法识别平台（支持：公众号 mp.weixin.qq.com / 小红书 xiaohongshu.com"
                           " / Twitter 推文 x.com/*/status/*）")
    out["platform"] = plat
    out["filename"] = _sanitize(out["title"]) + ".md"
    return out
