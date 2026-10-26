#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音图文两类形态采集（产出统一 markdown，由 app.py 入库 articles 表）。

① 纯文本文章  https://www.douyin.com/article/<id>
   RENDER_DATA 里没有正文（异步加载），须 CDP 真浏览器渲染后从 DOM 提取
   标题/日期/正文（_probe_article.py 实测命中 [class*=container] 4170 字）

② 纯图片图文  .../note/<id>、/slides/<id>、modal_id=<id>
   复用 douyin_auto 拦截 aweme_detail —— web API 的图片在 images[] 字段
   （移动端字段 image_list 为 None），原图取 url_list[0]，
   qwen3-vl 逐图「转录文字 + 内容解释」
"""

import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

VL_MODEL = os.environ.get("OCR_MODEL", "qwen3-vl:8b")
# qwen3-vl 系在本机 Ollama 0.40.6 下图像输入存在系统性退化（生成 2800 个空白
# token，done_reason=length，重启也无效，偶发成功），gemma 系正常出全文。
# 主模型失败/空响应时按序回退。
VL_FALLBACKS = ["gemma4:e4b", "gemma3:4b"]
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

_VL_PROMPT = (
    "请分两段回答本张图片：\n"
    "① 以「📝 转录」开头：完整转录图片中的全部文字，按原文段落顺序，"
    "保持原有分段；若图片没有文字则写「无文字」。\n"
    "② 以「💡 解释」开头：用两三句话说明这张图片的内容、主题或意图。"
)


class NotImgtextError(RuntimeError):
    """modal_id/note 链接经 detail 判定实为视频（无 images），应回退视频流水线。"""


def is_article_url(url: str) -> bool:
    return bool(url) and "douyin.com/article/" in url


def looks_like_imgtext_url(url: str) -> bool:
    """modal_id / note / slides 形态（可能是图文帖，也可能是视频，由 detail 判定）。"""
    if not url or "douyin.com" not in url:
        return False
    return bool(re.search(r"modal_id=\d{5,}|/note/\d{5,}|/slides/\d{5,}", url))


def _sanitize(name: str) -> str:
    """长期红线：先压空白再替换非法字符，最后 strip(' .')。"""
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)
    return name.strip(" .")[:60] or "untitled"


# ---------------- ① 纯文本文章 ----------------

def fetch_article(url: str, verbose: bool = True, wait_secs: int = 10) -> dict:
    """CDP 渲染文章页，提取 {title, date, text}。"""
    from playwright.sync_api import sync_playwright
    import douyin_cdp as dc

    out = {"title": "", "date": "", "text": ""}
    with sync_playwright() as p:
        b = dc.connect(p, verbose=verbose)
        ctx = dc.default_context(b)
        pg = dc.new_page(ctx)
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(wait_secs)
            # og:title 兜底标题
            try:
                out["title"] = pg.title().strip()[:80]
            except Exception:
                pass
            body = ""
            for sel in ("article", ".article-content", "[class*=article]",
                        "[class*=container]"):
                try:
                    el = pg.query_selector(sel)
                    if el:
                        txt = el.inner_text().strip()
                        if len(txt) > len(body):
                            body = txt
                except Exception:
                    pass
            if len(body) < 150:
                try:
                    body = pg.inner_text("body").strip()
                except Exception:
                    pass
            out["text"] = body
        finally:
            try:
                pg.close()
            except Exception:
                pass
            dc.stop(b)

    # 标题取正文第一行（og:title 常是站点名），日期用正则
    lines = [l.strip() for l in out["text"].splitlines() if l.strip()]
    if lines:
        cand = lines[0]
        if 4 <= len(cand) <= 60 and not re.match(r"^\d{4}-\d{2}-\d{2}$", cand):
            out["title"] = cand
        m = re.search(r"\d{4}-\d{2}-\d{2}", "\n".join(lines[:4]))
        if m:
            out["date"] = m.group(0)
        # 去掉已作标题的首行，避免正文重复
        if lines and out["title"] == lines[0]:
            out["text"] = "\n".join(lines[1:])
    return out


# ---------------- ② 纯图片图文 ----------------

def fetch_image_detail(url: str, verbose: bool = True):
    """拦截 aweme_detail；是图文帖（images 非空）返回 detail dict，否则 None。"""
    import douyin_auto as da
    try:
        detail = da.fetch_direct_url(url, verbose=verbose, return_full=True)
    except RuntimeError:
        return None
    if isinstance(detail, dict) and detail.get("images"):
        return detail
    return None


def download_images(detail: dict, img_dir: str, verbose: bool = True) -> list:
    """逐图下载原图（url_list[0]）到 img_dir，返回本地路径列表。"""
    os.makedirs(img_dir, exist_ok=True)
    aweme_id = detail.get("aweme_id") or str(int(time.time()))
    paths = []
    for i, im in enumerate(detail.get("images") or [], 1):
        urls = (im.get("url_list") or []) + (im.get("download_url_list") or [])
        last_err = None
        for u in urls:
            try:
                req = urllib.request.Request(u, headers={
                    "User-Agent": _UA,
                    "Referer": "https://www.douyin.com/",
                })
                data = urllib.request.urlopen(req, timeout=30).read()
                if len(data) < 1000:
                    raise RuntimeError(f"响应过小 {len(data)}B")
                if data[:8].startswith(b"\x89PNG"):
                    ext = ".png"
                elif data[:3] == b"GIF":
                    ext = ".gif"
                elif data[:3] == b"\xff\xd8\xff":
                    ext = ".jpg"
                else:
                    ext = ".webp"
                dest = os.path.join(img_dir, f"{aweme_id}_{i:02d}{ext}")
                io.open(dest, "wb").write(data)
                paths.append(dest)
                if verbose:
                    print(f"  [img] 图{i} 保存 {len(data)//1024}KB -> {os.path.basename(dest)}")
                break
            except Exception as e:
                last_err = e
        else:
            if verbose:
                print(f"  [img] 图{i} 全部地址下载失败: {last_err}")
    return paths


def _prep_for_vl(path: str) -> str:
    """给 qwen3-vl 用的分析副本：webp→jpg + 最长边压到 1024。

    实测 1792×2240 大图会让 qwen3-vl 空响应（5s 返回空串），
    降采样后正常完整转录；原图不动（vault 里保留原图）。
    返回临时文件路径，调用方负责删除。
    """
    try:
        import video_pipeline as vp
        ff = vp.get_ffmpeg()
        out = path + "_vl.jpg"
        r = subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", path,
             "-vf", "scale='min(1024,iw)':-1", "-q:v", "3", out],
            capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000:
            return out
    except Exception:
        pass
    return path


def _trim_repeat_tail(txt: str, max_repeat: int = 4) -> str:
    """截掉尾部重复循环（小模型偶发同一行刷屏，如「业投资」×200）。"""
    lines = txt.rstrip().splitlines()
    out = []
    for ln in lines:
        if len(out) >= max_repeat and all(o.strip() == ln.strip()
                                          for o in out[-max_repeat:]):
            break
        out.append(ln)
    return "\n".join(out).rstrip()


def analyze_image(path: str, timeout: int = 240, retries: int = 1) -> str:
    """逐图「转录 + 解释」：qwen3-vl 优先，空响应按链回退 gemma 系。"""
    import base64
    tmp = _prep_for_vl(path)
    try:
        with open(tmp, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""
    finally:
        if tmp != path and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    for model in [VL_MODEL] + VL_FALLBACKS:
        for _ in range(retries + 1):
            try:
                payload = json.dumps({
                    "model": model,
                    "stream": False,
                    "messages": [{"role": "user",
                                  "content": _VL_PROMPT, "images": [b64]}],
                    "options": {"temperature": 0.7, "num_predict": 1024,
                                "repeat_penalty": 1.1},
                }).encode("utf-8")
                # 实测：/api/generate 对 qwen3-vl 退化更甚，统一走 chat 端点
                req = urllib.request.Request(
                    OLLAMA.rstrip("/") + "/api/chat",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    j = json.loads(resp.read().decode("utf-8"))
                txt = ((j.get("message") or {}).get("content") or "").strip()
                if txt:
                    return _trim_repeat_tail(txt)
            except Exception:
                time.sleep(2)
    return ""


def build_image_md(detail: dict, img_paths: list, analyses: list,
                   source_url: str, img_dir_rel: str) -> str:
    """组装图文帖 markdown。"""
    aweme_id = detail.get("aweme_id") or ""
    desc = (detail.get("desc") or "").strip()
    author = ((detail.get("author") or {}).get("nickname")) or ""
    ts = detail.get("create_time")
    date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""
    title = _sanitize(desc) or f"抖音图文_{aweme_id}"

    lines = [f"# {title}", "",
             f"> 来源：[抖音视频/图文]({source_url}) · 作者：{author} · {date}", ""]
    if desc:
        lines += [desc, ""]
    lines += [f"## 🖼 图片内容解析（共 {len(img_paths)} 张）", ""]
    for i, (p, an) in enumerate(zip(img_paths, analyses), 1):
        rel = f"{img_dir_rel}/{os.path.basename(p)}"
        lines += [f"### 图 {i}", "", f"![图{i}]({rel})", ""]
        if an:
            lines += [an, ""]
        else:
            lines += ["（图片内容解析失败）", ""]
    return "\n".join(lines).rstrip() + "\n"
