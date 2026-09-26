#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VOA 英语听力（爱语吧 iyuba）采集器 —— 手机 APP「VOA英语听力」同源内容。

内容源：m.iyuba.cn/voaS/，国内直连（显式绕开系统代理），无需 cookie。
- 栏目列表（服务端直渲染，?pages=N 翻页，每页约 7 条）：
    index.jsp=VOA慢速  indexC.jsp=VOA常速  indexAM.jsp=1分钟美语
    （indexCV.jsp / indexTV.html 为视频栏目，暂不支持）
- 详情/字幕：play.jsp?id=<voaid>（常速条目同样有效）→ 隐藏 #senAll 区块内逐句双语：
    <div id='<秒>'>英文句</div><div id='<秒>cn'>中文译</div>
    ⚠ 属性为单引号；页面里同句文本会以明文重复出现一遍，须只取带数字 id 的 div
- 音频：http://staticvip.iyuba.cn/sounds/voa/<yyyymm>/<id>.mp3（直链无防盗链，支持断点续传）
- 封面：http://staticvip.iyuba.cn/images/voa/<yyyymm>/<id>.jpg
- 已知失效：apps.iyuba.cn/afterClass/detailApi.jsp 返回 404，勿再用

产物：Markdown（双语逐句对照+时间轴）+ 本地 mp3/lrc（<vault>/media/voa/，Web 走 /media/
路由）+ 封面（img_dir=<vault>/images/voa/，复用 /images/ 路由）。
"""
import datetime
import os
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
BASE = "http://m.iyuba.cn/voaS/"
# 本机学习播放器端口（与 app.py PORT 默认一致）
PLAYER_PORT = os.environ.get("PORT", "8788")
CATS = {"index.jsp": "VOA慢速", "indexC.jsp": "VOA常速", "indexAM.jsp": "1分钟美语"}


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "").strip()
    return (name or "untitled")[:80]


def _get(url: str, timeout: int = 30) -> str:
    """国内直连抓取：显式空 ProxyHandler，绕开系统/环境代理。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _download(url: str, dest: str, min_size: int = 1024, timeout: int = 120) -> bool:
    """下载到 dest，成功且 ≥min_size 返回 True，失败清理残留返回 False。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with opener.open(req, timeout=timeout) as r, open(dest, "wb") as f:
            f.write(r.read())
        if os.path.getsize(dest) >= min_size:
            return True
        os.remove(dest)
    except Exception:
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
    return False


def is_voa(url: str) -> bool:
    return "iyuba.cn" in (url or "").lower()


def is_list(url: str) -> bool:
    """列表页链接（index*.jsp / indexTV.html）→ 整页批量。"""
    u = (url or "").lower()
    return is_voa(u) and re.search(r"/voas?/index[a-z]{0,2}\.(?:jsp|html)", u) is not None


def _unquote2(v: str) -> str:
    """列表参数中文是双重 URL 编码（%25E9…），解一次后仍含 % 再解一次。"""
    v = urllib.parse.unquote(v or "")
    if "%" in v:
        try:
            v = urllib.parse.unquote(v)
        except Exception:
            pass
    return v.strip()


def _cat_from_url(url: str) -> str:
    u = (url or "").lower()
    if "indexam" in u or "playam" in u:
        return "1分钟美语"
    if "indexc" in u or "playc" in u:
        return "VOA常速"
    if "index" in u or "play" in u:
        return "VOA慢速"
    return "VOA"


def fetch_list(url: str) -> list:
    """列表页 → [{id,title,pic,sound,date,keywords,intro,category,play_url}]。"""
    html = _get(url)
    cat = _cat_from_url(url)
    items, seen = [], set()
    # play.jsp / playC.jsp（慢速/常速）/ playAM.jsp（1分钟美语）
    for m in re.finditer(r'href="(play[A-Za-z]*)\.jsp\?([^"]+)"', html):
        qs = urllib.parse.parse_qs(m.group(2))
        voaid = (qs.get("id") or [""])[0].strip()
        if not voaid.isdigit() or voaid in seen:
            continue
        seen.add(voaid)
        title = _unquote2((qs.get("title") or [""])[0])
        items.append({
            "id": voaid,
            "title": title or f"VOA {voaid}",
            "pic": (qs.get("pic") or [""])[0],
            "sound": (qs.get("sound") or [""])[0],
            "date": (qs.get("creatTime") or [""])[0],
            "keywords": _unquote2((qs.get("Keyword") or [""])[0]).lstrip("+ ").replace("+", " "),
            "intro": _unquote2((qs.get("IntroDesc") or [""])[0]).lstrip("+ ").replace("+", " "),
            "category": cat,
            "play_url": f"{BASE}play.jsp?id={voaid}",
        })
    return items


# 批量采集预设入口（前端写死按钮；批量一次抓 10 条新内容，自动排除已抓、顺延翻页）
BATCH_SIZE = 10
PRESETS = [
    ("VOA常速", "http://m.iyuba.cn/voaS/indexC.jsp"),
    ("VOA慢速", "http://m.iyuba.cn/voaS/index.jsp"),
    ("1分钟美语", "http://m.iyuba.cn/voaS/indexAM.jsp"),
]


def fetch_new_items(url: str, limit: int = BATCH_SIZE, have_ids=None,
                    max_pages: int = 10):
    """从 url 对应页起顺延翻页，跳过 have_ids 已抓条目，收集至多 limit 条新内容。
    返回 (new_items, skipped, pages_scanned)。"""
    have = set(have_ids or [])
    # 以入参为模板重建分页 URL（保留栏目，替换 pages 参数）
    p = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(p.query)
    try:
        start = int((qs.get("pages") or ["1"])[0] or 1)
    except ValueError:
        start = 1
    new, skipped, pages = [], 0, 0
    bad_pages = 0
    for page in range(start, start + max_pages):
        q = dict(urllib.parse.parse_qsl(p.query))
        q["pages"] = str(page)
        u = urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))
        try:
            items = fetch_list(u)
        except Exception:
            # 个别栏目个别页服务端 500（如 indexAM.jsp?pages=1），跳页继续
            bad_pages += 1
            if bad_pages >= 3:
                break
            continue
        if not items:
            break
        pages += 1
        for it in items:
            if it["id"] in have or any(x["id"] == it["id"] for x in new):
                skipped += 1
                continue
            new.append(it)
            if len(new) >= limit:
                return new, skipped, pages
    return new, skipped, pages


def _enrich_from_lists(item: dict, max_pages: int = 2) -> None:
    """裸播放链接（无标题参数）→ 扫各栏目列表前几页反查标题/日期/简介/关键词。
    找不到就保留原值（标题回落 'VOA <id>'），不报错。"""
    for page in range(1, max_pages + 1):
        for jsp in ("indexC.jsp", "index.jsp", "indexAM.jsp"):
            try:
                u = f"{BASE}{jsp}?pages={page}"
                for it in fetch_list(u):
                    if it["id"] == item["id"]:
                        item.update({k: v for k, v in it.items()
                                     if v and (not item.get(k)
                                               or (k == "category" and item.get(k) == "VOA"))})
                        if item.get("title"):
                            return
            except Exception:
                continue


def _item_from_url(url: str) -> dict:
    """单条播放页链接 → 最小 item（id 必需；title/封面/音频在详情页兜底解析）。"""
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    voaid = (qs.get("id") or [""])[0].strip()
    if not voaid.isdigit():
        raise RuntimeError(f"无法从链接解析 VOA id：{url[:120]}")
    title = _unquote2((qs.get("title") or [""])[0])
    item = {"id": voaid, "title": title, "pic": (qs.get("pic") or [""])[0],
            "sound": (qs.get("sound") or [""])[0], "date": (qs.get("creatTime") or [""])[0],
            "keywords": "", "intro": "", "category": _cat_from_url(url),
            "play_url": f"{BASE}play.jsp?id={voaid}"}
    if not title:
        _enrich_from_lists(item)
    return item


def _extract_sentences(html: str):
    """从 #senAll 起提取逐句双语，返回 [(start_sec, en, zh)]（按时间升序）。"""
    i = html.find("senAll")
    seg = html[i:] if i >= 0 else html
    en, zh = {}, {}
    for nid, iscn, body in re.findall(
            r"<div id='(\d+)(cn)?'[^>]*>(.*?)</div>", seg, re.S):
        txt = re.sub(r"<br\s*/?>|<[^>]+>", " ", body)
        txt = re.sub(r"\s+", " ", txt).strip()
        if not txt:
            continue
        (zh if iscn else en)[nid] = txt
    out = [(int(k), en[k], zh.get(k, "")) for k in sorted(en, key=int) if en[k]]
    return out


def _fmt_lrc(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}.{int(round((t - int(t)) * 100)):02d}"


def _fmt_ts(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def ingest_item(item: dict, img_dir: str = None) -> dict:
    """单条 VOA → Markdown + 本地 mp3/lrc/封面。item 可来自列表或 URL 解析。"""
    voaid = item["id"]
    html = _get(item.get("play_url") or f"{BASE}play.jsp?id={voaid}")
    sents = _extract_sentences(html)
    if not sents:
        raise RuntimeError("详情页未解析到逐句字幕（页面结构变化或该条目无文本）")

    # 音频直链：优先详情页里的完整 URL，回落列表 sound 参数补全
    m = re.search(r"https?://staticvip\.iyuba\.cn/sounds/voa/[^\"'\s<>]+\.mp3", html)
    if m:
        audio_url = m.group(0)
    elif item.get("sound"):
        audio_url = "http://staticvip.iyuba.cn/sounds/voa" + item["sound"]
    else:
        audio_url = ""
    # 封面直链：详情页 → 列表 pic
    m = re.search(r"https?://staticvip\.iyuba\.cn/images/voa/[^\"'\s<>]+\.(?:jpg|jpeg|png)", html)
    cover_url = m.group(0) if m else (item.get("pic") or "")

    vault_root = os.path.dirname(os.path.dirname(img_dir)) if img_dir else None
    media_dir = os.path.join(vault_root, "media", "voa") if vault_root else None

    # 下载 mp3 / lrc / 封面（失败回落外链，不阻断）
    audio_rel, lrc_rel, cover_rel = "", "", ""
    if media_dir:
        dest_mp3 = os.path.join(media_dir, f"voa_{voaid}.mp3")
        if audio_url and _download(audio_url, dest_mp3, min_size=100 * 1024):
            audio_rel = os.path.relpath(dest_mp3, vault_root).replace("\\", "/")
        lrc_lines = []
        for i, (t, en, zh) in enumerate(sents):
            tag = f"[{_fmt_lrc(t)}]"
            lrc_lines.append(f"{tag}{en}")
            if zh:
                lrc_lines.append(f"{tag}{zh}")
        try:
            dest_lrc = os.path.join(media_dir, f"voa_{voaid}.lrc")
            with open(dest_lrc, "w", encoding="utf-8") as f:
                f.write("\n".join(lrc_lines) + "\n")
            lrc_rel = os.path.relpath(dest_lrc, vault_root).replace("\\", "/")
        except OSError:
            pass
    if img_dir and cover_url:
        ext = ".png" if cover_url.lower().endswith(".png") else ".jpg"
        dest_cover = os.path.join(img_dir, f"voa_{voaid}{ext}")
        if _download(cover_url, dest_cover, min_size=1024, timeout=30):
            cover_rel = os.path.relpath(dest_cover, vault_root).replace("\\", "/")

    cat = item.get("category") or "VOA"
    title = (item.get("title") or f"VOA {voaid}").strip()
    date = item.get("date") or datetime.date.today().isoformat()
    audio_src = audio_rel or audio_url
    lrc_src = lrc_rel or ""

    L = ["---", "kind: article", f"platform: voa", f"voaid: {voaid}",
         f"category: {cat}", f"source: {cat}", f"date: {date}",
         f"url: {item.get('play_url', '')}", "---", "",
         f"# {title}", ""]
    if cover_rel:
        L += [f"![封面]({cover_rel})", ""]
    L += ["## 🎧 音频与字幕", ""]
    if audio_src:
        note = "" if audio_rel else "（外链，未本地化）"
        L.append(f"- 音频：[{os.path.basename(audio_src)}]({audio_src}){note}")
    else:
        L.append("- 音频：未找到直链")
    if lrc_src:
        L.append(f"- LRC 字幕：[{os.path.basename(lrc_src)}]({lrc_src})")
    L += ["", f"- 栏目：{cat} · 日期：{date}",
          f"- 学习播放器：[🎧 逐句精听（翻译可遮掩）](http://127.0.0.1:{PLAYER_PORT}/player/voa/{voaid})"]
    if item.get("keywords"):
        L.append(f"- 关键词：{item['keywords']}")
    if item.get("intro"):
        L += ["", "## 简介", "", "> " + item["intro"]]
    L += ["", f"## 双语逐句字幕（{len(sents)} 句）", ""]
    for i, (t, en, zh) in enumerate(sents):
        L.append(f"**[{_fmt_ts(t)}]** {en}")
        if zh:
            L.append(f"> {zh}")
        if i + 1 < len(sents):
            L.append("")
    md = "\n".join(L).rstrip() + "\n"
    return {"title": title, "md": md, "platform": "voa",
            "source": cat, "category": cat,
            "audio_rel": audio_rel, "lrc_rel": lrc_rel, "cover_rel": cover_rel,
            "sents": len(sents)}


def ingest(url: str, img_dir: str = None, item: dict = None) -> dict:
    """统一入口（单条播放页链接）。列表页由 app.py 展开为批量。
    item：批量路径传入的列表条目元数据（含 id/title/date 等），免二次反查。"""
    return ingest_item(item if item else _item_from_url(url), img_dir=img_dir)
