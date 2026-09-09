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

# ---------- 抖音主页采集 ----------
try:
    import douyin_profile as dp
except Exception as _dpe:  # 依赖缺失不让整个服务挂掉
    dp = None
    print("[warn] douyin_profile 加载失败:", _dpe, file=sys.stderr)

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
def do_crawl(url: str, use_llm: bool):
    from crawl4ai import (AsyncWebCrawler, BrowserConfig, CrawlerRunConfig,
                          CacheMode, LLMConfig)
    from crawl4ai.extraction_strategy import LLMExtractionStrategy

    browser_conf = BrowserConfig(
        browser_type="chromium",
        headless=True,
        extra_args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu",
                    "--disable-dev-shm-usage", "--no-proxy-server"],
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
def do_video_pipeline(source_url, video_path, write_vault):
    """处理单个已下载/已上传的视频：转写->总结->Markdown->落库。"""
    rec = {"source_url": source_url, "video_path": video_path}
    try:
        r = vp.process_video(video_path, url=source_url, source="视频号/抖音",
                             write_vault=write_vault)
        rec.update({
            "ok": r.get("ok", False),
            "title": r.get("title", os.path.basename(video_path)),
            "markdown": r.get("markdown", ""),
            "vault_path": r.get("vault_path", ""),
            "transcript_len": r.get("transcript_len", 0),
            "frames": r.get("frames", 0),
            "tags": r.get("tags", []),
            "error": r.get("vault_error") or r.get("transcript_error") or "",
        })
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["markdown"] = ""
    return rec


def do_video_batch(urls, write_vault):
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
                if not path:
                    rec = {"source_url": url, "ok": False,
                           "error": err or "下载失败", "markdown": ""}
                else:
                    rec = do_video_pipeline(url, path, write_vault)
                    rec["source_url"] = url
            except Exception as e:
                rec = {"source_url": url, "ok": False,
                       "error": f"{type(e).__name__}: {e}", "markdown": ""}
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
        # 去重：同一来源链接，成功记录复用原 id 覆盖更新，失败记录复用原失败行，
        # 避免重复提交/重试在列表里堆积重复条目
        if src:
            want = "done" if rec.get("ok") else "error"
            with db() as c:
                row = c.execute("SELECT id FROM videos WHERE source_url=? AND status=?",
                                (src, want)).fetchone()
            if row:
                vid = row[0]
        if not vid:
            vid = short_id(src or rec.get("title") or "video")
        path = os.path.join(CRAWLS_DIR, "video_" + vid + ".md")
        md = rec.get("markdown") or ""
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(md)
        with db() as c:
            c.execute(
                "INSERT OR REPLACE INTO videos "
                "(id,source_url,title,created_at,status,md_len,vault_path,error) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (vid, rec.get("source_url", ""), (rec.get("title", "") or "")[:120],
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "done" if rec.get("ok") else "error", len(md),
                 rec.get("vault_path", ""), str(rec.get("error", ""))[:500]))
            c.commit()
    return vid, path


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

    def _send_file(self, path, ctype, fname):
        with io.open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{fname}"')
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
        if p == "/api/video/history":
            rows = []
            with db() as c:
                cur = c.execute(
                    "SELECT id,source_url,title,created_at,md_len,status,vault_path "
                    "FROM videos ORDER BY created_at DESC LIMIT 200")
                for r in cur.fetchall():
                    rows.append(dict(zip(
                        ["id", "source_url", "title", "created_at",
                         "md_len", "status", "vault_path"], r)))
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
                    "SELECT id,source_url,title,created_at,md_len,status,vault_path,error "
                    "FROM videos WHERE id=?", (vid,)).fetchone()
            if not row:
                self._send(404, {"error": "not found"})
                return
            rec = dict(zip(["id", "source_url", "title", "created_at", "md_len",
                            "status", "vault_path", "error"], row))
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

    def do_POST(self):
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
        # ---------------- 视频号/抖音 整理 ----------------
        if self.path == "/api/video":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                urls = data.get("urls") or []
                write_vault = bool(data.get("write_vault", True))
                if not urls:
                    self._send(400, {"error": "urls 不能为空"})
                    return
                results = do_video_batch(urls, write_vault)
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
        if self.path == "/api/video/upload":
            try:
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


def main():
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"crawl4ai app running at http://127.0.0.1:{PORT}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
