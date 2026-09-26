#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
crawl4ai 交互式采集服务（本地优先 / 不出网 / 免费）
- POST /api/crawl      {url, use_llm?}  -> 抓取转 Markdown，存历史
- GET  /api/history?q= -> 历史列表（支持按 url/title 关键字搜索）
- GET  /api/crawls/<id>-> 单条详情（含 markdown / 抽取 json）
- GET  /api/crawls/<id>/download -> 下载 .md
- GET  /api/crawls/<id>  (DELETE) -> 删除
- GET  /               -> 前端页面
"""
import os
import sys
import io
import json
import sqlite3
import re
import html
import datetime
import asyncio
import urllib.parse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 清掉代理，避免 Chromium 继承死代理 ----------
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "no_proxy"):
    os.environ.pop(_k, None)

BASE = os.path.dirname(os.path.abspath(__file__))
CRAWLS_DIR = os.path.join(BASE, "crawls")
DB = os.path.join(BASE, "crawls.db")
PORT = int(os.environ.get("PORT", "8788"))
os.makedirs(CRAWLS_DIR, exist_ok=True)

# ---------- 视频整理（视频号/抖音） ----------
import video_downloader as vdl
import video_pipeline as vp

# ---------- 文章/笔记采集（公众号 / 小红书） ----------
try:
    import article_ingest as ai
except Exception as _aie:
    ai = None
    print("[warn] article_ingest 加载失败:", _aie, file=sys.stderr)

# 文章落库目录（Obsidian）：可用环境变量 ARTICLE_VAULT_DIR 覆盖
_ART_VAULT_DEFAULT = r"E:\Workbuddy\Claw\08-文章笔记"
ART_VAULT = (os.environ.get("ARTICLE_VAULT_DIR")
             or (_ART_VAULT_DEFAULT if os.path.isdir(os.path.dirname(_ART_VAULT_DEFAULT))
                 else os.path.join(BASE, "articles_vault")))
os.makedirs(ART_VAULT, exist_ok=True)

# ---------- VoiceStudio 本地 TTS 桥接 ----------
try:
    import voice_tts as vt
except Exception as _vte:
    vt = None
    print("[warn] voice_tts 加载失败:", _vte, file=sys.stderr)

TTS_OUT = os.path.join(BASE, "tts_out")
os.makedirs(TTS_OUT, exist_ok=True)

# ---------- 第三步：要点解析（文字类，复用 video_pipeline 的 Ollama） ----------
try:
    import content_analyze as ca
except Exception as _cae:
    ca = None
    print("[warn] content_analyze 加载失败:", _cae, file=sys.stderr)

# ---------- 抖音主页采集 ----------
try:
    import douyin_profile as dp
except Exception as _dpe:  # 依赖缺失不让整个服务挂掉
    dp = None
    print("[warn] douyin_profile 加载失败:", _dpe, file=sys.stderr)

# ---------- Twitter/X 采集（单条推文 + 账号推文清单） ----------
try:
    import twitter_ingest as twi
except Exception as _twe:
    twi = None
    print("[warn] twitter_ingest 加载失败:", _twe, file=sys.stderr)

_profile_lock = threading.Lock()   # 主页采集互斥：playwright 实例只跑一个

# ---------- DB ----------
_lock = threading.Lock()
_video_lock = threading.Lock()   # 视频流水线全局互斥：多请求串行，防 Ollama/CPU 争抢


def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS crawls (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            created_at TEXT,
            md_len INTEGER,
            fit_len INTEGER,
            use_llm INTEGER,
            status TEXT,
            error TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            source_url TEXT,
            title TEXT,
            created_at TEXT,
            status TEXT,
            md_len INTEGER,
            vault_path TEXT,
            error TEXT
        )""")
        # 迁移：videos 增加 category 列（记录分类管理，''=未分类）
        try:
            c.execute("ALTER TABLE videos ADD COLUMN category TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 迁移：videos 增加 source 列（来源分类：抖音视频/YouTube视频，''=未标来源）
        try:
            c.execute("ALTER TABLE videos ADD COLUMN source TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        # 文章/笔记采集（公众号 / 小红书）
        c.execute("""CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            url TEXT,
            platform TEXT,
            title TEXT,
            created_at TEXT,
            status TEXT,
            md_len INTEGER,
            vault_path TEXT,
            error TEXT
        )""")
        # 迁移：articles 增加 category/source 列（与 videos 同一套分类/来源池，''=未分类/未标来源）
        for _col in ("category", "source"):
            try:
                c.execute(f"ALTER TABLE articles ADD COLUMN {_col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 存量文章按平台一次性回填来源（只填空值，幂等）
        for _p, _s in (("twitter", "Twitter"), ("wechat", "公众号"), ("xhs", "小红书")):
            c.execute("UPDATE articles SET source=? WHERE source='' AND platform=?",
                      (_s, _p))
        # TTS 配音记录（VoiceStudio 本地合成）
        c.execute("""CREATE TABLE IF NOT EXISTS tts (
            id TEXT PRIMARY KEY,
            created_at TEXT,
            text TEXT,
            voice TEXT,
            fmt TEXT,
            chars INTEGER,
            chunks INTEGER,
            seconds REAL,
            file TEXT,
            error TEXT
        )""")
        c.commit()


def db():
    return sqlite3.connect(DB)


def short_id(url: str) -> str:
    t = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    h = abs(hash(url)) % 100000
    return f"{t}-{h:05d}"


def save_crawl(rec: dict, markdown: str, fit: str, extracted):
    with _lock:
        path = os.path.join(CRAWLS_DIR, rec["id"] + ".md")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(markdown or "")
        if fit:
            with io.open(os.path.join(CRAWLS_DIR, rec["id"] + ".fit.md"), "w", encoding="utf-8") as f:
                f.write(fit)
        if extracted:
            with io.open(os.path.join(CRAWLS_DIR, rec["id"] + ".json"), "w", encoding="utf-8") as f:
                json.dump(extracted, f, ensure_ascii=False, indent=2)
        with db() as c:
            c.execute(
                "INSERT OR REPLACE INTO crawls (id,url,title,created_at,md_len,fit_len,use_llm,status,error) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["url"], rec["title"], rec["created_at"],
                 rec["md_len"], rec["fit_len"], rec["use_llm"], rec["status"], rec["error"]),
            )
            c.commit()


# ---------- crawl ----------
def do_crawl(url: str, use_llm: bool, proxy: str = ""):
    from crawl4ai import (AsyncWebCrawler, BrowserConfig, CrawlerRunConfig,
                          CacheMode, LLMConfig)
    from crawl4ai.extraction_strategy import LLMExtractionStrategy

    browser_conf = BrowserConfig(
        browser_type="chromium",
        headless=True,
        extra_args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage"]
                    + (["--no-proxy-server"] if not proxy else ["--proxy-server=" + proxy]),
    )
    run_conf = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=120000,
    )
    extracted = None
    if use_llm:
        try:
            llm = LLMConfig(provider="ollama/qwen3:14b", api_token="sk-noauth")
            run_conf.extraction_strategy = LLMExtractionStrategy(
                llm_config=llm,
                instruction=("从网页抽取关键事实，输出 JSON 数组，每条含字段："
                             "name(主体名), business(业务), products(产品/服务), "
                             "region(地区), summary(一句话摘要)。只输出 JSON。"),
            )
        except Exception as e:
            print("[warn] llm setup skipped:", e, file=sys.stderr)

    async def _run():
        async with AsyncWebCrawler(config=browser_conf) as crawler:
            return await crawler.arun(url=url, config=run_conf)

    result = asyncio.run(_run())
    # crawl4ai 0.9.3: result.markdown 是 str 兼容对象本身即是 Markdown 文本
    raw = str(result.markdown) if result.markdown else ""
    fit = getattr(result, "fit_markdown", "") or ""
    if use_llm and getattr(result, "extracted_content", None):
        try:
            extracted = json.loads(result.extracted_content)
        except Exception:
            extracted = result.extracted_content
    return raw or "", fit or "", extracted


# ---------- 视频整理（视频号/抖音） ----------
def detect_source_name(url):
    """按 URL 推断来源名称（写入 videos.source 列）；推断不出返回 ''。"""
    try:
        s = vdl.detect_source(url)
    except Exception:
        s = ""
    if s == "youtube":
        return "YouTube视频"
    if s == "douyin":
        return "抖音视频"
    if s == "bilibili":
        return "B站视频"
    return ""


def do_video_pipeline(source_url, video_path, write_vault, bilingual=False, first_frame_only=False,
                      source=""):
    """处理单个已下载/已上传的视频：转写->总结->Markdown->落库。"""
    rec = {"source_url": source_url, "video_path": video_path, "source": source or ""}
    try:
        r = vp.process_video(video_path, url=source_url,
                             source=source or "视频号/抖音",
                             write_vault=write_vault,
                             bilingual=bilingual, first_frame_only=first_frame_only)
        rec.update({
            "ok": r.get("ok", False),
            "title": r.get("title", os.path.basename(video_path)),
            "markdown": r.get("markdown", ""),
            "vault_path": r.get("vault_path", ""),
            "transcript_len": r.get("transcript_len", 0),
            "frames": r.get("frames", 0),
            "tags": r.get("tags", []),
            "lang": r.get("lang", ""),
            "bilingual": r.get("bilingual", False),
            "error": r.get("vault_error") or r.get("transcript_error") or "",
        })
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["markdown"] = ""
    return rec


def do_video_batch(urls, write_vault, bilingual=False, first_frame_only=False, source_hint=""):
    """链接清单批量：**逐条**「下载 -> 整理 -> 立即入库」。

    设计要点（防止一条卡死拖垮整批）：
    - 每条处理完立即 save_video 写 DB，后面哪怕卡住，已完成的记录也都在；
    - 单条异常被捕获转为 error 记录，不会中断循环；
    - 全程持 _video_lock 串行执行，避免多个并发请求同时压本机 Ollama/CPU。
    """
    results = []
    with _video_lock:
        for raw in urls:
            url = (raw or "").strip()
            if not url or url.startswith("#"):
                continue
            try:
                path, err = vdl.download_one(url, vp.INBOX)
                # 来源：URL 可推断优先，否则用发起页提示（直链/短链场景）
                rec_src = detect_source_name(url) or source_hint
                if not path:
                    rec = {"source_url": url, "ok": False,
                           "error": err or "下载失败", "markdown": ""}
                else:
                    rec = do_video_pipeline(url, path, write_vault,
                                            bilingual=bilingual,
                                            first_frame_only=first_frame_only,
                                            source=rec_src)
                    rec["source_url"] = url
            except Exception as e:
                rec = {"source_url": url, "ok": False,
                       "error": f"{type(e).__name__}: {e}", "markdown": ""}
                rec_src = detect_source_name(url) or source_hint
            rec["source"] = rec_src
            try:
                vid, _ = save_video(rec)
                rec["id"] = vid
            except Exception as se:
                rec["save_error"] = str(se)[:200]
            results.append(rec)
    return results


def parse_multipart(body, boundary):
    """极简 multipart 解析，返回 [(filename, bytes), ...]（仅取文件部分）。"""
    parts = []
    delim = ("--" + boundary).encode()
    for seg in body.split(delim):
        if seg in (b"--\r\n", b"--", b"", b"\r\n"):
            continue
        head_end = seg.find(b"\r\n\r\n")
        if head_end == -1:
            continue
        head = seg[:head_end].decode("utf-8", "ignore")
        m = re.search(r'filename="([^"]+)"', head)
        if not m:
            continue
        data = seg[head_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        parts.append((m.group(1), data))
    return parts


def save_video(rec):
    with _lock:
        src = rec.get("source_url", "")
        vid = ""
        prev_cat = ""
        prev_source = ""
        # 去重：同一来源链接，成功记录复用原 id 覆盖更新，失败记录复用原失败行，
        # 避免重复提交/重试在列表里堆积重复条目
        if src:
            want = "done" if rec.get("ok") else "error"
            with db() as c:
                row = c.execute("SELECT id, category, source FROM videos WHERE source_url=? AND status=?",
                                (src, want)).fetchone()
            if row:
                vid = row[0]
                prev_cat = row[1] or ""
                prev_source = row[2] or ""
        if not vid:
            vid = short_id(src or rec.get("title") or "video")
        path = os.path.join(CRAWLS_DIR, "video_" + vid + ".md")
        md = rec.get("markdown") or ""
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(md)
        with db() as c:
            c.execute(
                "INSERT OR REPLACE INTO videos "
                "(id,source_url,title,created_at,status,md_len,vault_path,error,category,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (vid, rec.get("source_url", ""), (rec.get("title", "") or "")[:120],
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "done" if rec.get("ok") else "error", len(md),
                 rec.get("vault_path", ""), str(rec.get("error", ""))[:500], prev_cat,
                 (rec.get("source") or "").strip() or prev_source))
            c.commit()
    return vid, path


def save_article(url, platform, title, md, vault_path="", error="", source=""):
    """文章采集结果写 DB（同 URL 成功记录覆盖更新，失败记 error 行）。

    重复抓取保留用户已编辑的 category/source；首次入库 source 按平台自动标注。"""
    with _lock:
        aid = ""
        status = "done" if md else "error"
        prev_cat, prev_src = "", ""
        if url:
            with db() as c:
                row = c.execute("SELECT id FROM articles WHERE url=? AND status=?",
                                (url, status)).fetchone()
            if row:
                aid = row[0]
        if aid:
            with db() as c:
                row = c.execute("SELECT category, source FROM articles WHERE id=?",
                                (aid,)).fetchone()
                if row:
                    prev_cat, prev_src = row[0] or "", row[1] or ""
        if not aid:
            aid = short_id(url or title or "article")
        _PLAT_SRC = {"twitter": "Twitter", "wechat": "公众号", "xhs": "小红书",
                     "voa": "VOA"}
        def_src = prev_src or source or _PLAT_SRC.get((platform or "").lower(), "")
        path = os.path.join(CRAWLS_DIR, "article_" + aid + ".md")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(md or "")
        with db() as c:
            c.execute(
                "INSERT OR REPLACE INTO articles "
                "(id,url,platform,title,created_at,status,md_len,vault_path,error,category,source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (aid, url, platform, (title or "")[:120],
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 status, len(md or ""), vault_path, str(error)[:500],
                 prev_cat, def_src))
            c.commit()
    return aid, path


def _fetch_article_body(link: str, proxy: str = "", timeout: int = 45) -> str:
    """轻量抓外链正文：urllib 拉页面 -> 剥脚本/样式 -> 提取 <p> / article-paragraph 段落。
    不走 crawl4ai/chromium（Windows 上 async 清理偶发挂起会卡死请求线程）。
    支持简单分页（?page=N，如纽约时报中文网）。"""
    import urllib.request as _ur

    def _extract(html: str) -> list:
        html = re.sub(r"<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1\s*>",
                      "", html, flags=re.S | re.I)
        m = re.search(r"<article[^>]*>(.*?)</article\s*>", html, flags=re.S | re.I)
        seg = m.group(1) if m else html
        paras = []
        for a, b in re.findall(r"<p[^>]*>(.*?)</p\s*>"
                               r"|<div[^>]*class=\"[^\"]*article-paragraph[^\"]*\"[^>]*>(.*?)</div\s*>",
                               seg, flags=re.S | re.I):
            txt = re.sub(r"<[^>]+>", "", a or b)
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) >= 40:
                paras.append(txt)
        return paras

    def _get(u: str) -> str:
        req = _ur.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
        })
        op = _ur.build_opener(_ur.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}))
        with op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore")

    paras = _extract(_get(link))
    # 简单分页跟进（NYT 中文网等 ?page=N），最多 5 页
    if paras and "?" not in link.split("#")[0]:
        base = link.split("#")[0]
        for pg in range(2, 6):
            try:
                more = _extract(_get(f"{base}?page={pg}"))
            except Exception:
                break
            if not more:
                break
            paras.extend(more)
    return "\n\n".join(paras)


def _tweet_follow_link(tweet):
    """从推文正文提取第一条外部原文链接（推文常是长文的'指针'）。"""
    import re as _re
    if not tweet:
        return None
    skip = ("x.com", "twitter.com", "pbs.twimg.com", "video.twimg.com")
    best = None
    for u in _re.findall(r"https?://\S+", tweet.get("text") or ""):
        u = u.rstrip(").，。；、\\]}>\"'")
        host = u.split("/")[2].lower() if "://" in u else ""
        if any(s in host for s in skip):
            continue
        if "t.co" in host:          # Twitter 官方短链：备选（302 到真实地址）
            best = best or u
            continue
        return u                    # 明确的原文链接优先
    return best


# ---------------- 文章 md 远程图片本地化（微信防盗链：无 Referer 才放行） ----------------
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _localize_md_images(md, img_dir, base_dir):
    """把 md 里的远程图片下载到 img_dir，替换为相对 base_dir 的路径；失败保留外链。

    微信 mmbiz.qpic.cn 有 Referer 防盗链（Obsidian/记录中心加载会被拒），必须本地化；
    返回 (新md, 成功本地化张数)。
    """
    if not md or not img_dir:
        return md, 0
    os.makedirs(img_dir, exist_ok=True)
    import hashlib
    import urllib.request
    n = 0

    def _dl(m):
        nonlocal n
        alt, url = m.group(1), m.group(2)
        # 仅处理公众号/微信系 CDN（其余源各自已处理或无明显防盗链）
        if "qpic.cn" not in url and "mmbiz" not in url and "wx.qlogo.cn" not in url:
            return m.group(0)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                # 故意不带 Referer：微信 CDN 靠 Referer 防盗链
            })
            data = urllib.request.urlopen(req, timeout=30).read()
            if len(data) < 1000:      # 防盗链会返回很小的占位图
                return m.group(0)
            ext = ".png" if data[:8].startswith(b"\x89PNG") else (".gif" if data[:3] == b"GIF" else ".jpg")
            name = hashlib.md5(url.encode("utf-8")).hexdigest()[:16] + ext
            dest = os.path.join(img_dir, name)
            if not os.path.exists(dest):
                with open(dest, "wb") as f:
                    f.write(data)
            rel = os.path.relpath(dest, base_dir).replace("\\", "/")
            n += 1
            return f"![{alt}]({rel})"
        except Exception:
            return m.group(0)

    return _MD_IMG_RE.sub(_dl, md), n


def _backfill_wechat_images():
    """存量公众号记录一次性回填：vault 与 crawls 两份 md 都本地化图片（幂等）。"""
    try:
        with db() as c:
            rows = c.execute(
                "SELECT id, vault_path FROM articles "
                "WHERE platform='wechat' AND status='done'").fetchall()
    except Exception:
        return
    done = 0
    for aid, vpath in rows:
        targets = [p for p in (vpath, os.path.join(CRAWLS_DIR, f"article_{aid}.md"))
                   if p and os.path.isfile(p)]
        if not targets:
            continue
        img_dir = os.path.join(ART_VAULT, "images", "wechat")
        for p in targets:
            try:
                with io.open(p, "r", encoding="utf-8") as f:
                    md = f.read()
                new_md, n = _localize_md_images(md, img_dir, os.path.dirname(p) or ".")
                if n and new_md != md:
                    with io.open(p, "w", encoding="utf-8") as f:
                        f.write(new_md)
                    done += n
            except Exception:
                pass
    if done:
        print(f"[backfill] 公众号存量图片本地化 {done} 张")


def _plain_text_len(md: str) -> int:
    """估算 md 正文纯文字长度（去 frontmatter/图片/链接/URL/标记符），作 OCR 兜底判据。"""
    body = re.sub(r"^---\n.*?\n---\n", "", md or "", flags=re.S)
    if "\n## 图片" in body:
        body = body.split("\n## 图片")[0]
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"https?://\S+", "", body)
    body = re.sub(r"[#>*`|_-]", " ", body)
    return len(re.sub(r"\s", "", body))


def _maybe_ocr_images(md: str, vault_root: str):
    """全平台通用兜底：正文纯文字过短且 md 里有已本地化图片 → 本地 qwen3-vl OCR 补文字。
    覆盖 xhs/twitter/wechat 及未来任何走文章管线、做了图片本地化的源。
    返回 (新md, 描述)。失败不阻断。"""
    try:
        if _plain_text_len(md) >= 120:
            return md, ""
        locals_ = sorted(set(re.findall(
            r"!\[[^\]]*\]\((images/[A-Za-z0-9_\-]+/[^)]+)\)", md)))
        paths = []
        for s in locals_:
            fp = os.path.join(vault_root, s.replace("/", os.sep))
            if os.path.isfile(fp):
                paths.append(fp)
        if not paths:
            return md, ""
        import vision_ocr
        texts = vision_ocr.ocr_images(paths)
        got = [(i + 1, t) for i, t in enumerate(texts) if t.strip()]
        if not got:
            return md, ""
        seg = ["", "## 📝 图片文字识别（本地 qwen3-vl）", ""]
        seg += [f"**图 {n}**\n\n{t}\n" for n, t in got]
        ocr_md = "\n".join(seg).rstrip() + "\n"
        if "\n## 图片" in md:
            md = md.replace("\n## 图片", "\n" + ocr_md + "\n## 图片", 1)
        else:
            md = md.rstrip() + "\n\n" + ocr_md
        return md, f"正文过短，已 OCR {len(got)}/{len(paths)} 张图片补文字（本地 qwen3-vl）"
    except Exception as _e:
        return md, f"OCR 兜底失败（不阻断）：{str(_e)[:100]}"


def _backfill_vault_refs():
    """后台回填（线程，不阻塞启动）：
    ① 修存量 xhs/twitter 本地图引用前缀（旧格式缺 images/：](xhs/… → ](images/xhs/…）
    ② 图片型 xhs/twitter 旧记录（正文过短）补本地 qwen3-vl OCR 段。均幂等。"""
    import threading

    def _work():
        n_fix = n_ocr = 0
        try:
            with db() as c:
                rows = c.execute(
                    "SELECT id, vault_path FROM articles "
                    "WHERE platform IN ('xhs','twitter') AND status='done'").fetchall()
        except Exception:
            return
        for aid, vpath in rows:
            targets = [p for p in (vpath, os.path.join(CRAWLS_DIR, f"article_{aid}.md"))
                       if p and os.path.isfile(p)]
            if not targets:
                continue
            need_ocr = False
            for p in targets:
                try:
                    with io.open(p, "r", encoding="utf-8") as f:
                        md = f.read()
                    new = re.sub(r"\]\((xhs|twitter)/", r"](images/\1/", md)
                    if new != md:
                        with io.open(p, "w", encoding="utf-8") as f:
                            f.write(new)
                        n_fix += 1
                    if (p == targets[0] and "图片文字识别" not in new
                            and re.search(r"images/(xhs|twitter|wechat)/", new)):
                        need_ocr = True
                except Exception:
                    pass
            # 图片型笔记补 OCR：正文纯文字（去图片/链接/标记符）过短
            if need_ocr and vpath and os.path.isfile(vpath):
                try:
                    with io.open(vpath, "r", encoding="utf-8") as f:
                        md = f.read()
                    # 注意：正文为空的占位文案含"反爬拦截"字样，
                    # 那正是最需要 OCR 的场景，不能据此跳过
                    if _plain_text_len(md) >= 120:
                        continue
                    vault_root = os.path.dirname(vpath)
                    locals_ = re.findall(
                        r"!\[[^\]]*\]\((images/(?:xhs|twitter|wechat)/[^)]+)\)", md)
                    paths = [os.path.join(vault_root, s.replace("/", os.sep))
                             for s in locals_ if os.path.isfile(os.path.join(vault_root, s.replace("/", os.sep)))]
                    if not paths:
                        continue
                    import vision_ocr
                    texts = vision_ocr.ocr_images(paths)
                    got = [(i + 1, t) for i, t in enumerate(texts) if t.strip()]
                    if not got:
                        continue
                    seg = ["", "## 📝 图片文字识别（本地 qwen3-vl）", ""]
                    seg += [f"**图 {n}**\n\n{t}\n" for n, t in got]
                    ocr_md = "\n".join(seg).rstrip() + "\n"
                    md = md.replace("（正文为空，可能被反爬拦截，建议配置 XHS_COOKIE）",
                                    "（正文为空，文字内容见图内 OCR）")
                    if "\n## 图片" in md:
                        md = md.replace("\n## 图片", "\n" + ocr_md + "\n## 图片", 1)
                    else:
                        md = md.rstrip() + "\n\n" + ocr_md
                    with io.open(vpath, "w", encoding="utf-8") as f:
                        f.write(md)
                    # crawls 副本同步
                    cp = os.path.join(CRAWLS_DIR, f"article_{aid}.md")
                    if os.path.isfile(cp):
                        with io.open(cp, "w", encoding="utf-8") as f:
                            f.write(md)
                    n_ocr += 1
                except Exception:
                    pass
        if n_fix or n_ocr:
            print(f"[backfill] 图引用前缀修正 {n_fix} 处，图片型笔记补 OCR {n_ocr} 条")

    threading.Thread(target=_work, daemon=True).start()


def do_article_ingest(url, write_vault=True, platform="", analyze=False,
                      follow=False, meta=None):
    """公众号/小红书/Twitter 采集 -> Markdown -> 推文外链跟进(可选) -> 第三步要点解析(可选) -> 可选写 Obsidian 库。"""
    plat = (platform or "").strip() or ai.detect_platform(url) or ""
    # VOA 源只做「照搬」（音频+双语字幕+播放器），不做要点解析等任何解析任务
    if plat == "voa":
        analyze = False
    # VOA 列表页链接 → 批量采集：自动排除已抓条目、顺延翻页，每次抓 BATCH_SIZE 条新内容
    if plat == "voa":
        try:
            import voa_ingest as _vi
            if _vi.is_list(url):
                have = set()
                try:
                    with db() as c:
                        for (u,) in c.execute(
                                "SELECT url FROM articles WHERE platform='voa'"):
                            m = re.search(r"[?&]id=(\d+)", u or "")
                            if m:
                                have.add(m.group(1))
                except Exception:
                    pass
                items, skipped, pages = _vi.fetch_new_items(
                    url, limit=_vi.BATCH_SIZE, have_ids=have)
                if not items:
                    return {"id": "", "ok": True, "platform": "voa",
                            "title": "VOA 批量采集：没有新内容",
                            "md": "", "vault_path": "", "analyze": None,
                            "follow": f"该栏目已抓条目全部跳过（{skipped} 条），无新增",
                            "batch": [], "ocr": ""}
                batch, first = [], None
                for it in items:
                    try:
                        sub = do_article_ingest(it["play_url"], write_vault,
                                                platform="voa", analyze=False,
                                                follow=False, meta=it)
                        batch.append({"title": sub.get("title", ""), "ok": True})
                        if not first:
                            first = sub.get("md", "")
                    except Exception as _be:
                        batch.append({"title": it.get("title", ""), "ok": False,
                                      "error": str(_be)[:120]})
                okn = sum(1 for b in batch if b["ok"])
                return {"id": "", "ok": okn > 0, "platform": "voa",
                        "title": f"VOA 批量采集 {okn}/{len(batch)} 条新内容",
                        "md": first or "", "vault_path": "", "analyze": None,
                        "follow": (f"自动排除已抓 {skipped} 条 · 顺延扫描 {pages} 页 · "
                                   f"本次新增 {okn}/{len(batch)}"),
                        "batch": batch, "ocr": ""}
        except ImportError:
            pass
    img_dir = None
    if write_vault and plat in ("xhs", "twitter", "wechat", "voa"):
        # 图片本地化目录：<vault>/images/<platform>/（CDN 外链带时效 token/防盗链，会失效）
        img_dir = os.path.join(ART_VAULT, "images", plat)
    out = ai.ingest(url, img_dir=img_dir if img_dir else None, meta=meta)
    # 公众号 md 里的远程图不落地（wechat_fetch 不支持 img_dir），入库前统一本地化
    if write_vault and plat == "wechat" and out.get("md"):
        out["md"], _n_img = _localize_md_images(
            out["md"], img_dir, ART_VAULT)
    # 全平台 OCR 兜底：正文纯文字过短且图已本地化（如 Twitter 纯图推文、
    # 公众号纯图文章、小红书图片型笔记）→ 本地 qwen3-vl 补文字，任何源通用
    ocr_info = ""
    if write_vault and out.get("md") and img_dir:
        out["md"], ocr_info = _maybe_ocr_images(out["md"], ART_VAULT)
    follow_info = ""
    if follow and out.get("platform") == "twitter":
        if (out.get("tweet") or {}).get("article", {}).get("blocks"):
            follow_info = "X 原生长文（Article），全文已随推文抓取"
        else:
            link = _tweet_follow_link(out.get("tweet"))
            if link:
                try:
                    proxy = ""
                    try:
                        if twi is not None:
                            proxy = next((p for p in twi._proxy_candidates() if p), "")
                    except Exception:
                        proxy = ""
                    body = _fetch_article_body(link, proxy=proxy).strip()
                    if len(body) > 300:
                        out["md"] = (out["md"].rstrip()
                                     + "\n\n## 📄 原文全文（跟进自推文链接）\n\n"
                                     + f"> 链接：{link}\n\n" + body[:30000] + "\n")
                        follow_info = f"已跟进外链并抓取全文（{min(len(body),30000)} 字）"
                    else:
                        follow_info = "外链抓取内容过短，未合并"
                except Exception as _fe:
                    follow_info = f"外链跟进失败：{str(_fe)[:120]}"
            else:
                follow_info = "推文内无外部原文链接"
    ares = None
    if analyze and out.get("md"):
        try:
            dec = ca.analyze_and_decorate(out["md"])
            out["md"] = dec["md"]
            ares = dec["analyze"]
        except Exception as _ae:  # 第三步失败不阻断入库
            ares = {"ok": False, "error": str(_ae)[:200]}
    vpath = ""
    if write_vault and out.get("md"):
        os.makedirs(ART_VAULT, exist_ok=True)
        vpath = os.path.join(ART_VAULT, out["filename"])
        # 同名文件加序号，避免覆盖历史
        base, ext = os.path.splitext(vpath)
        n = 1
        while os.path.exists(vpath):
            vpath = f"{base}-{n}{ext}"
            n += 1
        with io.open(vpath, "w", encoding="utf-8") as f:
            f.write(out["md"])
    aid, _ = save_article(url, out.get("platform", ""), out.get("title", ""),
                          out.get("md", ""), vault_path=vpath,
                          source=out.get("source", ""))
    return {"id": aid, "ok": True, "platform": out.get("platform", ""),
            "title": out.get("title", ""), "md": out.get("md", ""),
            "vault_path": vpath, "analyze": ares, "follow": follow_info,
            "ocr": ocr_info or out.get("ocr", "")}


# ---------- 抖音音乐（采集·播放·管理，免解析） ----------
def do_douyin_music(url, write_vault=True, audio_only=True):
    """单条抖音作品链接 → 下载音频/视频落地 → 极简笔记入库（platform=douyin_music）。
    已抓过的链接直接跳过（返回 skipped），与 VOA 批量「自动排除已抓」一致。"""
    import douyin_music as dm
    url = (url or "").strip()
    if not url:
        raise RuntimeError("url 不能为空")
    with db() as c:
        row = c.execute("SELECT id, title FROM articles WHERE platform='douyin_music' "
                        "AND url=? AND status='done'", (url,)).fetchone()
    if row:
        return {"id": row[0], "ok": True, "platform": "douyin_music",
                "title": row[1] or "已抓过", "skipped": True,
                "md": "", "vault_path": "",
                "follow": f"该链接已采集过（记录 {row[0]}），自动跳过"}
    out = dm.ingest(url, vault_root=ART_VAULT, audio_only=audio_only)
    vpath = ""
    if write_vault and out.get("md"):
        os.makedirs(ART_VAULT, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        vpath = os.path.join(ART_VAULT, f"{ts}-{out['aweme_id']}.md")
        with io.open(vpath, "w", encoding="utf-8") as f:
            f.write(out["md"])
    aid, _ = save_article(url, out["platform"], out["title"], out.get("md", ""),
                          vault_path=vpath, source=out.get("source", ""))
    return {"id": aid, "ok": True, "platform": "douyin_music",
            "title": out["title"], "source": out.get("source", ""),
            "aweme_id": out.get("aweme_id", ""), "md": out.get("md", ""),
            "media_rel": out.get("media_rel", ""), "vault_path": vpath,
            "skipped": False}


def _douyin_playlist_rows(source: str, cur_aid: str = "") -> list:
    """播放列表：同号主优先（号主为空则全部抖音音乐），当前条在前。"""
    rows, seen = [], set()
    with db() as c:
        qs = ("SELECT id, url, title, source FROM articles "
              "WHERE platform='douyin_music' AND status='done'")
        args = ()
        if source:
            qs += " AND source=?"
            args = (source,)
        qs += " ORDER BY created_at DESC LIMIT 100"
        try:
            allrows = c.execute(qs, args).fetchall()
        except Exception:
            allrows = []
    if not allrows and source:
        with db() as c:
            allrows = c.execute(
                "SELECT id, url, title, source FROM articles "
                "WHERE platform='douyin_music' AND status='done' "
                "ORDER BY created_at DESC LIMIT 100").fetchall()
    import douyin_music as dm
    # 当前条排最前，其余保持时间序
    ordered = []
    for r in allrows:
        if r[0] == cur_aid:
            ordered.insert(0, r)
        else:
            ordered.append(r)
    for aid, u, t, src in ordered:
        vid = dm.extract_id(u or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        rows.append({"aid": aid, "vid": vid, "title": (t or f"抖音 {vid}")[:50],
                     "src": src or ""})
        if len(rows) >= 50:
            break
    return rows


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        # 页面与接口禁用缓存，避免改版后浏览器仍显示旧页面
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, fname, inline=False):
        with io.open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'{"inline" if inline else "attachment"}; filename="{fname}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/" or p == "/index.html":
            try:
                with io.open(os.path.join(BASE, "index.html"), "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(404, "index.html missing", "text/plain; charset=utf-8")
            return
        # 本地静态资源（marked / dompurify 等，仅 vendor/ 下 .js/.css，防目录穿越）
        if p.startswith("/vendor/"):
            fname = p[len("/vendor/"):]
            if ".." in fname or fname.startswith("/") or not fname.endswith((".js", ".css")):
                self._send(404, {"error": "not found"})
                return
            fpath = os.path.join(BASE, "vendor", fname)
            if os.path.isfile(fpath):
                with io.open(fpath, "rb") as f:
                    data = f.read()
                ctype = "application/javascript; charset=utf-8" if fname.endswith(".js") else "text/css; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {"error": "not found"})
            return
        # vault 本地化音频/LRC（VOA 等，media/... 相对路径 Web 渲染打到站点根，这里映射回 vault）
        if p.startswith("/media/"):
            fname = urllib.parse.unquote(p[len("/media/"):])
            parts = fname.split("/")
            def _seg_ok2(s):
                return bool(s) and ".." not in s and "/" not in s and "\\" not in s \
                    and all(c.isprintable() for c in s)
            if len(parts) != 2 or not _seg_ok2(parts[0]) or not _seg_ok2(parts[1]):
                self._send(404, {"error": "not found"})
                return
            fpath = os.path.join(ART_VAULT, "media", parts[0], parts[1])
            if os.path.isfile(fpath):
                ext = parts[1].rsplit(".", 1)[-1].lower()
                ctype = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
                         "mp4": "video/mp4", "webm": "video/webm",
                         "lrc": "text/plain; charset=utf-8",
                         "srt": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
                fsize = os.path.getsize(fpath)
                # Range 分段响应（视频/音频拖动进度条必需）
                rng = self.headers.get("Range") or ""
                m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
                start, end = 0, fsize - 1
                partial = False
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), fsize - 1)
                    else:
                        start = max(fsize - int(m.group(2)), 0)
                    start = min(start, fsize - 1)
                    end = max(end, start)
                    partial = True
                length = end - start + 1
                with io.open(fpath, "rb") as f:
                    f.seek(start)
                    data = f.read(length)
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {"error": "not found"})
            return
        # vault 本地化图片（记录中心 Web 渲染相对路径 images/... 会打到站点根，这里映射回 vault）
        if p.startswith("/images/"):
            fname = urllib.parse.unquote(p[len("/images/"):])
            parts = fname.split("/")
            # 仅允许 <platform>/<file> 两段；文件名可含中文/emoji，
            # 但禁路径分隔符、.. 与控制字符（防目录穿越）
            def _seg_ok(s):
                return bool(s) and ".." not in s and "/" not in s and "\\" not in s \
                    and all(c.isprintable() for c in s)
            if len(parts) != 2 or not _seg_ok(parts[0]) or not _seg_ok(parts[1]):
                self._send(404, {"error": "not found"})
                return
            fpath = os.path.join(ART_VAULT, "images", parts[0], parts[1])
            if os.path.isfile(fpath):
                ext = parts[1].rsplit(".", 1)[-1].lower()
                ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                         "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}.get(ext, "application/octet-stream")
                with io.open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {"error": "not found"})
            return
        # VOA 逐句学习播放器（音频+双语字幕同步，翻译可显示/遮掩/隐藏）
        m = re.match(r"^/player/voa/(\d+)$", p)
        if m:
            voaid = m.group(1)
            title = ""
            try:
                with db() as c:
                    r = c.execute(
                        "SELECT title FROM articles WHERE platform='voa' "
                        "AND url LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"%id={voaid}%",)).fetchone()
                    title = r[0] if r else ""
            except Exception:
                pass
            import voa_player
            code, html = voa_player.render(voaid, title=title)
            self.send_response(code)
            body = html.encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        # 抖音音乐播放器（纯播放+播放列表连播）。
        # <key> 两种形态：纯数字长 id = aweme_id；含 '-' = articles.id（记录主键）
        m = re.match(r"^/player/douyin/([^/]+)$", p)
        if m:
            import douyin_music as dm
            import douyin_player
            key = urllib.parse.unquote(m.group(1))
            vid, title, author, aid = "", "", "", ""
            if re.fullmatch(r"\d{10,}", key):
                vid = key
                try:
                    with db() as c:
                        r = c.execute(
                            "SELECT id, title, source FROM articles "
                            "WHERE platform='douyin_music' AND url LIKE ? "
                            "ORDER BY created_at DESC LIMIT 1",
                            (f"%{vid}%",)).fetchone()
                        if r:
                            aid, title, author = r[0], r[1] or "", r[2] or ""
                except Exception:
                    pass
            else:
                aid = key
                try:
                    with db() as c:
                        r = c.execute(
                            "SELECT url, title, source FROM articles "
                            "WHERE id=? AND platform='douyin_music'",
                            (aid,)).fetchone()
                        if not r:
                            self._send(404, "<h1>记录不存在</h1>", "text/html; charset=utf-8")
                            return
                        vid = dm.extract_id(r[0] or "")
                        title, author = r[1] or "", r[2] or ""
                except Exception:
                    pass
            if not vid:
                self._send(404, "<h1>无法从记录解析 aweme_id</h1>",
                           "text/html; charset=utf-8")
                return
            code, html = douyin_player.render(
                vid, title=title, author=author,
                playlist=_douyin_playlist_rows(author, cur_aid=aid))
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/history":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            rows = []
            with db() as c:
                if q:
                    like = f"%{q}%"
                    cur = c.execute(
                        "SELECT id,url,title,created_at,md_len,use_llm,status FROM crawls "
                        "WHERE url LIKE ? OR title LIKE ? ORDER BY created_at DESC LIMIT 200",
                        (like, like))
                else:
                    cur = c.execute(
                        "SELECT id,url,title,created_at,md_len,use_llm,status FROM crawls "
                        "ORDER BY created_at DESC LIMIT 200")
                for r in cur.fetchall():
                    rows.append(dict(zip(
                        ["id", "url", "title", "created_at", "md_len", "use_llm", "status"], r)))
            self._send(200, rows)
            return
        m = re.match(r"^/api/crawls/([^/]+)$", p)
        if m:
            cid = m.group(1)
            with db() as c:
                row = c.execute(
                    "SELECT id,url,title,created_at,md_len,fit_len,use_llm,status,error "
                    "FROM crawls WHERE id=?", (cid,)).fetchone()
            if not row:
                self._send(404, {"error": "not found"})
                return
            rec = dict(zip(["id", "url", "title", "created_at", "md_len",
                            "fit_len", "use_llm", "status", "error"], row))
            md_path = os.path.join(CRAWLS_DIR, cid + ".md")
            rec["markdown"] = io.open(md_path, "r", encoding="utf-8").read() if os.path.exists(md_path) else ""
            jp = os.path.join(CRAWLS_DIR, cid + ".json")
            rec["extracted"] = json.load(io.open(jp, "r", encoding="utf-8")) if os.path.exists(jp) else None
            self._send(200, rec)
            return
        m = re.match(r"^/api/crawls/([^/]+)/download$", p)
        if m:
            cid = m.group(1)
            path = os.path.join(CRAWLS_DIR, cid + ".md")
            if os.path.exists(path):
                self._send_file(path, "text/markdown; charset=utf-8", f"{cid}.md")
            else:
                self._send(404, "not found")
            return
        if p == "/api/articles":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            rows = []
            with db() as c:
                if q:
                    like = f"%{q}%"
                    cur = c.execute(
                        "SELECT id,url,platform,title,created_at,md_len,status,vault_path,"
                        "category,source "
                        "FROM articles WHERE url LIKE ? OR title LIKE ? "
                        "ORDER BY created_at DESC LIMIT 200", (like, like))
                else:
                    cur = c.execute(
                        "SELECT id,url,platform,title,created_at,md_len,status,vault_path,"
                        "category,source "
                        "FROM articles ORDER BY created_at DESC LIMIT 200")
                for r in cur.fetchall():
                    rows.append(dict(zip(
                        ["id", "url", "platform", "title", "created_at",
                         "md_len", "status", "vault_path", "category", "source"], r)))
            self._send(200, rows)
            return
        m = re.match(r"^/api/articles/([^/]+)$", p)
        if m:
            aid = m.group(1)
            with db() as c:
                row = c.execute(
                    "SELECT id,url,platform,title,created_at,md_len,status,vault_path,error,"
                    "category,source "
                    "FROM articles WHERE id=?", (aid,)).fetchone()
            if not row:
                self._send(404, {"error": "not found"})
                return
            rec = dict(zip(["id", "url", "platform", "title", "created_at",
                            "md_len", "status", "vault_path", "error",
                            "category", "source"], row))
            md = ""
            vpath = rec.get("vault_path") or ""
            if vpath and os.path.isfile(vpath):
                with io.open(vpath, "r", encoding="utf-8") as f:
                    md = f.read()
            else:
                cp = os.path.join(CRAWLS_DIR, "article_" + aid + ".md")
                if os.path.exists(cp):
                    with io.open(cp, "r", encoding="utf-8") as f:
                        md = f.read()
            rec["markdown"] = md
            self._send(200, rec)
            return
        # ---------------- VoiceStudio TTS ----------------
        if p == "/api/tts/status":
            if vt is None:
                self._send(200, {"up": False, "error": "voice_tts 模块未加载"})
                return
            up, info = vt.health()
            self._send(200, {"up": up, **info})
            return
        if p == "/api/tts/voices":
            if vt is None:
                self._send(500, {"error": "voice_tts 模块未加载"})
                return
            voices, err = vt.list_voices()
            if err:
                self._send(502, {"error": f"VoiceStudio 后端不可达: {err}"})
                return
            self._send(200, voices)
            return
        if p == "/api/tts/history":
            rows = []
            with db() as c:
                cur = c.execute(
                    "SELECT id,created_at,text,voice,fmt,chars,chunks,seconds,file,error "
                    "FROM tts ORDER BY created_at DESC LIMIT 200")
                for r in cur.fetchall():
                    rows.append(dict(zip(
                        ["id", "created_at", "text", "voice", "fmt", "chars",
                         "chunks", "seconds", "file", "error"], r)))
            self._send(200, rows)
            return
        m = re.match(r"^/api/tts/files/([A-Za-z0-9_.\-]+)$", p)
        if m:
            fp = os.path.join(TTS_OUT, m.group(1))
            if not os.path.isfile(fp):
                self._send(404, {"error": "not found"})
                return
            ctype = "audio/mpeg" if fp.endswith(".mp3") else "audio/wav"
            self._send_file(fp, ctype, m.group(1), inline=True)
            return
        if p == "/api/video/history":
            rows = []
            with db() as c:
                cur = c.execute(
                    "SELECT id,source_url,title,created_at,md_len,status,vault_path,category,source "
                    "FROM videos ORDER BY created_at DESC LIMIT 200")
                for r in cur.fetchall():
                    rows.append(dict(zip(
                        ["id", "source_url", "title", "created_at",
                         "md_len", "status", "vault_path", "category", "source"], r)))
            self._send(200, rows)
            return
        if p == "/api/video/health":
            # 工具可用性自诊断：确认当前进程加载的是新代码、依赖齐全
            health = {"python": sys.executable, "tools": {}}
            try:
                yt = vdl.get_yt_dlp_cmd()
                health["tools"]["yt_dlp"] = {"ok": True, "cmd": yt}
            except Exception as e:
                health["tools"]["yt_dlp"] = {"ok": False, "error": str(e)[:300]}
            try:
                ff = vp.get_ffmpeg()
                health["tools"]["ffmpeg"] = {"ok": bool(ff and os.path.exists(ff)),
                                             "path": ff}
            except Exception as e:
                health["tools"]["ffmpeg"] = {"ok": False, "error": str(e)[:300]}
            try:
                import faster_whisper  # noqa
                ws_dir = os.path.join(vp.WHISPER_LOCAL_ROOT,
                                      f"faster-whisper-{vp.TRANSCRIBE_MODEL}")
                binp = os.path.join(ws_dir, "model.bin")
                local_ok = os.path.isfile(binp) and os.path.getsize(binp) > 1_000_000
                health["tools"]["faster_whisper"] = {
                    "ok": True, "local_model": local_ok,
                    "model": vp.TRANSCRIBE_MODEL if local_ok else "tiny(回退)",
                    "model_dir": ws_dir if local_ok else ""}
            except Exception as e:
                health["tools"]["faster_whisper"] = {"ok": False,
                                                     "error": "未安装（转写将降级）"}
            health["vault"] = vp.VAULT
            self._send(200, health)
            return
        m = re.match(r"^/api/video/records/([^/]+)$", p)
        if m:
            vid = m.group(1)
            with db() as c:
                row = c.execute(
                    "SELECT id,source_url,title,created_at,md_len,status,vault_path,error,category,source "
                    "FROM videos WHERE id=?", (vid,)).fetchone()
            if not row:
                self._send(404, {"error": "not found"})
                return
            rec = dict(zip(["id", "source_url", "title", "created_at", "md_len",
                            "status", "vault_path", "error", "category", "source"], row))
            md = ""
            vpath = rec.get("vault_path") or ""
            if vpath and os.path.isfile(vpath):
                with io.open(vpath, "r", encoding="utf-8") as f:
                    md = f.read()
            else:
                cp = os.path.join(CRAWLS_DIR, "video_" + vid + ".md")
                if os.path.exists(cp):
                    with io.open(cp, "r", encoding="utf-8") as f:
                        md = f.read()
            rec["markdown"] = md
            self._send(200, rec)
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        # 删除 TTS 记录（连同音频文件）
        m = re.match(r"^/api/tts/history/([^/]+)$", self.path)
        if m:
            tid = m.group(1)
            with _lock, db() as c:
                row = c.execute("SELECT file FROM tts WHERE id=?", (tid,)).fetchone()
                if not row:
                    self._send(404, {"error": "not found"})
                    return
                c.execute("DELETE FROM tts WHERE id=?", (tid,))
                c.commit()
            if row[0]:
                fp = os.path.join(TTS_OUT, os.path.basename(row[0]))
                if os.path.isfile(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            self._send(200, {"ok": True})
            return
        # 删除分类/来源：该值下的记录回落（记录本身不动）；?dim=source 表示操作来源列
        _path_only, _, _query = self.path.partition("?")
        _dim_col = "source" if "dim=source" in _query else "category"
        m = re.match(r"^/api/video/cats/(.+)$", _path_only)
        if m:
            cat = urllib.parse.unquote(m.group(1)).strip()
            if not cat:
                self._send(400, {"error": "分类名为空"})
                return
            with _lock:
                with db() as c:
                    n = c.execute(f"UPDATE videos SET {_dim_col}='' WHERE {_dim_col}=?",
                                  (cat,)).rowcount
                    # 文章记录共用同一套分类/来源池，联动清空
                    n += c.execute(f"UPDATE articles SET {_dim_col}='' WHERE {_dim_col}=?",
                                   (cat,)).rowcount
                    c.commit()
            self._send(200, {"ok": True, "affected": n})
            return
        m = re.match(r"^/api/articles/([^/]+)$", self.path)
        if m:
            aid = m.group(1)
            removed = []
            with _lock:
                with db() as c:
                    row = c.execute(
                        "SELECT vault_path, platform, url FROM articles WHERE id=?",
                        (aid,)).fetchone()
                    c.execute("DELETE FROM articles WHERE id=?", (aid,))
                    c.commit()
            if row and row[0] and os.path.isfile(row[0]):
                try:
                    os.remove(row[0])
                    removed.append(row[0])
                except Exception:
                    pass
            # 抖音音乐：连同本地媒体（m4a/mp3/mp4）与封面一并删除
            if row and row[1] == "douyin_music":
                try:
                    import douyin_music as dm
                    vid = dm.extract_id(row[2] or "")
                    if vid:
                        removed += dm.cleanup_media(ART_VAULT, vid)
                except Exception:
                    pass
            fp = os.path.join(CRAWLS_DIR, "article_" + aid + ".md")
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    removed.append(fp)
                except Exception:
                    pass
            self._send(200, {"ok": True, "removed": removed})
            return
        m = re.match(r"^/api/crawls/([^/]+)$", self.path)
        if m:
            cid = m.group(1)
            with _lock:
                with db() as c:
                    c.execute("DELETE FROM crawls WHERE id=?", (cid,))
                    c.commit()
                for ext in (".md", ".fit.md", ".json"):
                    fp = os.path.join(CRAWLS_DIR, cid + ext)
                    if os.path.exists(fp):
                        os.remove(fp)
            self._send(200, {"ok": True})
            return
        m = re.match(r"^/api/video/([^/]+)$", self.path)
        if m:
            vid = m.group(1)
            removed = []
            with _lock:
                with db() as c:
                    row = c.execute(
                        "SELECT vault_path FROM videos WHERE id=?", (vid,)).fetchone()
                    c.execute("DELETE FROM videos WHERE id=?", (vid,))
                    c.commit()
            # 删知识库内的笔记（自包含 md，无外部引用）
            if row and row[0] and os.path.isfile(row[0]):
                try:
                    os.remove(row[0])
                    removed.append(row[0])
                except Exception:
                    pass
            # 删服务端的 md 副本
            fp = os.path.join(CRAWLS_DIR, "video_" + vid + ".md")
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                    removed.append(fp)
                except Exception:
                    pass
            self._send(200, {"ok": True, "removed": removed})
            return
        self._send(404, {"error": "not found"})

    def do_PATCH(self):
        # 编辑视频记录：改名（同步重命名 Obsidian 笔记文件）/ 归类
        m = re.match(r"^/api/video/([^/]+)$", self.path)
        if m:
            vid = m.group(1)
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._send(400, {"error": "请求体解析失败"})
                return
            new_title = (data.get("title") or "").strip()
            new_cat = data.get("category")
            new_src = data.get("source")
            with _lock:
                with db() as c:
                    row = c.execute(
                        "SELECT title, vault_path, category FROM videos WHERE id=?",
                        (vid,)).fetchone()
                    if not row:
                        self._send(404, {"error": "not found"})
                        return
                    old_title, vpath, _old_cat = row
                    vault_renamed = False
                    if new_title and new_title != old_title:
                        # 同步重命名知识库里的笔记文件（保持库内一致）
                        if vpath and os.path.isfile(vpath):
                            safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", new_title)[:120]
                            nv = os.path.join(os.path.dirname(vpath), safe + ".md")
                            if not os.path.exists(nv):
                                try:
                                    os.rename(vpath, nv)
                                    vpath = nv
                                    vault_renamed = True
                                except Exception:
                                    pass  # 文件被占用等：只改数据库，vault_path 不变
                        c.execute("UPDATE videos SET title=?, vault_path=? WHERE id=?",
                                  (new_title[:120], vpath, vid))
                    if new_cat is not None:
                        cat = (str(new_cat) or "").strip()[:30]
                        c.execute("UPDATE videos SET category=? WHERE id=?", (cat, vid))
                    if new_src is not None:
                        sv = (str(new_src) or "").strip()[:30]
                        c.execute("UPDATE videos SET source=? WHERE id=?", (sv, vid))
                    c.commit()
            self._send(200, {"ok": True, "vault_renamed": vault_renamed,
                             "vault_path": vpath})
            return
        # 编辑文章记录：改名（同步重命名 Obsidian 笔记文件）+ 归类/标来源
        m = re.match(r"^/api/articles/([^/]+)$", self.path)
        if m:
            aid = m.group(1)
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._send(400, {"error": "请求体解析失败"})
                return
            new_title = (data.get("title") or "").strip()
            if not new_title:
                self._send(400, {"error": "title required"})
                return
            with _lock:
                with db() as c:
                    row = c.execute("SELECT title, vault_path FROM articles WHERE id=?",
                                    (aid,)).fetchone()
                    if not row:
                        self._send(404, {"error": "not found"})
                        return
                    old_title, vpath = row
                    vault_renamed = False
                    if new_title != old_title:
                        if vpath and os.path.isfile(vpath):
                            safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", new_title)[:120]
                            nv = os.path.join(os.path.dirname(vpath), safe + ".md")
                            if not os.path.exists(nv):
                                try:
                                    os.rename(vpath, nv)
                                    vpath = nv
                                    vault_renamed = True
                                except Exception:
                                    pass  # 文件被占用等：只改数据库
                        c.execute("UPDATE articles SET title=?, vault_path=? WHERE id=?",
                                  (new_title[:120], vpath, aid))
                    new_cat = data.get("category")
                    if new_cat is not None:
                        c.execute("UPDATE articles SET category=? WHERE id=?",
                                  ((str(new_cat) or "").strip()[:30], aid))
                    new_src = data.get("source")
                    if new_src is not None:
                        c.execute("UPDATE articles SET source=? WHERE id=?",
                                  ((str(new_src) or "").strip()[:30], aid))
                    c.commit()
            self._send(200, {"ok": True, "vault_renamed": vault_renamed,
                             "vault_path": vpath})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/video/cats/rename":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send(400, {"error": "请求体解析失败"})
                return
            old = (data.get("old") or "").strip()
            new = (data.get("new") or "").strip()[:30]
            if not old or not new:
                self._send(400, {"error": "分类名不能为空"})
                return
            _col = "source" if "dim=source" in self.path else "category"
            with _lock:
                with db() as c:
                    n = c.execute(f"UPDATE videos SET {_col}=? WHERE {_col}=?",
                                  (new, old)).rowcount
                    # 文章记录共用同一套分类/来源池，联动更新
                    n += c.execute(f"UPDATE articles SET {_col}=? WHERE {_col}=?",
                                   (new, old)).rowcount
                    c.commit()
            self._send(200, {"ok": True, "affected": n})
            return
        if self.path == "/api/crawl":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                url = (data.get("url") or "").strip()
                use_llm = bool(data.get("use_llm", False))
                if not url:
                    self._send(400, {"error": "url required"})
                    return
                cid = short_id(url)
                rec = {"id": cid, "url": url, "title": url, "created_at":
                       datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "md_len": 0, "fit_len": 0, "use_llm": 1 if use_llm else 0,
                       "status": "running", "error": ""}
                try:
                    md, fit, extracted = do_crawl(url, use_llm)
                    rec["title"] = (md.split("\n", 1)[0].lstrip("# ").strip() or url)[:120]
                    rec["md_len"] = len(md)
                    rec["fit_len"] = len(fit)
                    rec["status"] = "done"
                    save_crawl(rec, md, fit, extracted)
                    out = dict(rec)
                    out["markdown"] = md
                    out["extracted"] = extracted
                    self._send(200, out)
                except Exception as e:
                    rec["status"] = "error"
                    rec["error"] = str(e)[:500]
                    save_crawl(rec, "", "", None)
                    self._send(500, rec)
            except Exception as e:
                self._send(500, {"error": str(e)[:500]})
            return
        # ---------------- 文章/笔记采集（公众号/小红书） ----------------
        if self.path == "/api/article":
            try:
                if ai is None:
                    self._send(500, {"error": "article_ingest 模块未加载（看服务日志）"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                url = (data.get("url") or "").strip()
                write_vault = bool(data.get("write_vault", True))
                platform = (data.get("platform") or "").strip()
                do_analyze = bool(data.get("analyze", False))
                do_follow = bool(data.get("follow", False))
                if not url:
                    self._send(400, {"error": "url required"})
                    return
                self._send(200, do_article_ingest(url, write_vault, platform,
                                                  analyze=do_analyze,
                                                  follow=do_follow))
            except Exception as e:
                # 失败也落一条 error 记录，便于前端历史里看到原因
                try:
                    save_article(url, platform, "", "", error=str(e)[:500])
                except Exception:
                    pass
                self._send(500, {"error": str(e)[:500]})
            return
        # ---------------- VoiceStudio TTS 合成 ----------------
        if self.path == "/api/tts":
            rec = {"id": "", "created_at": "", "text": "", "voice": "",
                   "fmt": "", "chars": 0, "chunks": 0, "seconds": 0,
                   "file": "", "error": ""}
            try:
                if vt is None:
                    self._send(500, {"error": "voice_tts 模块未加载（看服务日志）"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                text = (data.get("text") or "").strip()
                voice = (data.get("voice") or "demo0001").strip()[:64]
                fmt = (data.get("fmt") or "mp3").strip().lower()
                steps = int(data.get("steps") or 16)
                if fmt not in ("mp3", "wav"):
                    fmt = "mp3"
                if not text:
                    self._send(400, {"error": "text 不能为空"})
                    return
                # 健康预检：给用户明确提示而不是干等超时
                up, hinfo = vt.health()
                if not up:
                    self._send(502, {"error": "VoiceStudio 后端未启动（需先运行 VoiceStudio，端口 3900）"})
                    return
                rid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                rec.update(id=rid, created_at=rid, text=text[:2000],
                           voice=voice, fmt=fmt, chars=len(text))
                audio, ext, nchunks, err = vt.synthesize(text, voice, fmt, steps=steps)
                if err or not audio:
                    rec["error"] = (err or "合成失败")[:500]
                    with _lock, db() as c:
                        c.execute("INSERT OR REPLACE INTO tts VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  tuple(rec[k] for k in ["id", "created_at", "text", "voice",
                                                         "fmt", "chars", "chunks", "seconds",
                                                         "file", "error"]))
                        c.commit()
                    self._send(502, rec)
                    return
                fname = f"tts_{rid}.{ext}"
                with open(os.path.join(TTS_OUT, fname), "wb") as f:
                    f.write(audio)
                # 估算时长：wav 用 wave 模块精确读，mp3 按 ~128kbps 估算
                secs = 0.0
                if ext == "wav":
                    import wave as _wave
                    try:
                        w = _wave.open(os.path.join(TTS_OUT, fname), "rb")
                        secs = w.getnframes() / max(w.getframerate(), 1)
                        w.close()
                    except Exception:
                        pass
                else:
                    secs = len(audio) * 8.0 / 128000.0
                rec.update(file=fname, chunks=nchunks, seconds=round(secs, 1))
                with _lock, db() as c:
                    c.execute("INSERT OR REPLACE INTO tts VALUES (?,?,?,?,?,?,?,?,?,?)",
                              tuple(rec[k] for k in ["id", "created_at", "text", "voice",
                                                     "fmt", "chars", "chunks", "seconds",
                                                     "file", "error"]))
                    c.commit()
                self._send(200, {"ok": True, **rec})
            except Exception as e:
                rec["error"] = str(e)[:500]
                try:
                    with _lock, db() as c:
                        c.execute("INSERT OR REPLACE INTO tts VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  tuple(rec[k] for k in ["id", "created_at", "text", "voice",
                                                         "fmt", "chars", "chunks", "seconds",
                                                         "file", "error"]))
                        c.commit()
                except Exception:
                    pass
                self._send(500, {"error": str(e)[:500]})
            return
        # ---------------- 抖音音乐采集（免解析，媒体落地+极简笔记） ----------------
        if self.path == "/api/douyin_music":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                url = (data.get("url") or "").strip()
                write_vault = bool(data.get("write_vault", True))
                audio_only = bool(data.get("audio_only", True))
                self._send(200, do_douyin_music(url, write_vault=write_vault,
                                                audio_only=audio_only))
            except Exception as e:
                # 失败也落一条 error 记录，便于历史里看到原因
                try:
                    _u = (data.get("url") if isinstance(data, dict) else "") or ""
                    save_article(_u, "douyin_music", "", "", error=str(e)[:500])
                except Exception:
                    pass
                self._send(500, {"error": str(e)[:500]})
            return
        # ---------------- 视频号/抖音 整理 ----------------
        if self.path == "/api/video":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                urls = data.get("urls") or []
                write_vault = bool(data.get("write_vault", True))
                bilingual = bool(data.get("bilingual", False))
                first_frame_only = bool(data.get("first_frame", False))
                source_hint = (data.get("source_hint") or "").strip()[:30]
                if not urls:
                    self._send(400, {"error": "urls 不能为空"})
                    return
                results = do_video_batch(urls, write_vault,
                                         bilingual=bilingual,
                                         first_frame_only=first_frame_only,
                                         source_hint=source_hint)
                self._send(200, {"results": results})
            except Exception as e:
                self._send(500, {"error": str(e)[:500]})
            return
        if self.path == "/api/video/profile":
            try:
                if dp is None:
                    self._send(500, {"error": "douyin_profile 模块未加载（看服务日志）"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                url = (data.get("url") or "").strip()
                # max_count: 0/缺省语义区分——None 才用默认 50，显式 0 表示不限量
                mc = data.get("max_count", None)
                max_count = 50 if mc is None else max(0, int(mc))
                if not url:
                    self._send(400, {"error": "url required（抖音用户主页链接）"})
                    return
                # 采集比较慢（要滚动翻页），与视频流水线互不抢资源但自身串行
                with _profile_lock:
                    out = dp.fetch_profile_videos(url, verbose=False,
                                                  max_count=max_count)
                self._send(200, out)
            except Exception as e:
                self._send(500, {"error": str(e)[:500]})
            return
        if self.path == "/api/twitter/profile":
            try:
                if twi is None:
                    self._send(500, {"error": "twitter_ingest 模块未加载（看服务日志）"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                url = (data.get("url") or "").strip()
                mc = data.get("max_count", None)
                max_count = 50 if mc is None else max(0, int(mc))
                if not url:
                    self._send(400, {"error": "url required（Twitter 主页链接或 @handle）"})
                    return
                # 纯网络抓取（syndication/guest 通道 + fxtwitter 补文本），不占 playwright
                self._send(200, twi.list_user_tweets(url, max_count))
            except Exception as e:
                self._send(500, {"error": str(e)[:500]})
            return
        if self.path.split("?")[0] == "/api/video/upload":
            try:
                _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                _src_hint = (_q.get("source_hint", [""])[0] or "").strip()[:30]
                ctype = self.headers.get("Content-Type", "")
                write_vault = True
                if "multipart/form-data" not in ctype:
                    self._send(400, {"error": "需要 multipart/form-data"})
                    return
                boundary = ctype.split("boundary=")[-1].strip()
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                files = parse_multipart(body, boundary)
                if not files:
                    self._send(400, {"error": "未收到文件"})
                    return
                results = []
                for fname, fdata in files:
                    outp = os.path.join(vp.INBOX, fname)
                    with open(outp, "wb") as f:
                        f.write(fdata)
                    with _video_lock:
                        rec = do_video_pipeline("", outp, write_vault)
                    rec["source_url"] = "(本地文件) " + fname
                    vid, _ = save_video(rec)
                    rec["id"] = vid
                    results.append(rec)
                self._send(200, {"results": results})
            except Exception as e:
                self._send(500, {"error": str(e)[:500]})
            return
        self._send(404, {"error": "not found"})


class NoReuseServer(ThreadingHTTPServer):
    # 禁用 SO_REUSEADDR：Windows 下它允许多个进程重复绑同一端口，
    # 造成双实例并存（请求随机分发 + GPU/模型争抢死锁），必须独占
    allow_reuse_address = False


def main():
    # 单实例守卫：启动前主动探测端口，已有实例则拒绝启动
    import socket
    _s = socket.socket()
    _s.settimeout(1)
    _already = (_s.connect_ex(("127.0.0.1", PORT)) == 0)
    _s.close()
    if _already:
        print(f"[abort] 端口 {PORT} 已有实例在运行，拒绝重复启动（这是防双实例死锁的守卫）")
        sys.exit(1)
    init_db()
    _backfill_wechat_images()   # 存量公众号记录图片本地化（幂等）
    _backfill_vault_refs()      # 后台：修图引用前缀 + 图片型笔记补 OCR（幂等）
    srv = NoReuseServer(("127.0.0.1", PORT), Handler)
    print(f"crawl4ai app running at http://127.0.0.1:{PORT} (pid={os.getpid()})")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
