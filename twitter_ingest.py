#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Twitter/X 采集（供 app.py / article_ingest.py 调用）。

能力：
  1. 单条推文 -> Markdown（正文 + 作者 + 日期 + 图片本地化；视频推文只保留直链）
  2. 账号推文清单（像抖音号采集那样：先列清单，勾选后走链接清单批量入库）

免 cookie 通道（国内网络需代理，复用 video_downloader._get_proxy 的解析：
VIDEO_PROXY > HTTPS_PROXY/HTTP_PROXY > 项目内 proxy.txt > 直连）：
  单条推文  L0 fxtwitter API -> L1 vxtwitter API -> L2 syndication tweet-result
  账号清单  L0 syndication timeline（免 cookie）-> L1 guest-token user_timeline
            清单只回推文 ID，再用 fxtwitter 逐条补文本/日期

注：视频推文如需转录，请把推文链接放进视频流水线（yt-dlp 原生支持 x.com）。
"""
import os
import re
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import video_downloader as vdl

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Twitter Web 客户端公开 bearer（公开内嵌在网页 JS 里，非私密凭据），guest 通道用
WEB_BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
              "%3D1Zv7onMptdQjEsfCmMrgR76ldr1")

# graphql UserTweets 的 features 参数（社区通用最小集）
_GQL_FEATURES = json.dumps({
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "articles_preview_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}, separators=(",", ":"))

_RESERVED = {"i", "home", "explore", "search", "notifications", "messages",
             "settings", "status", "statuses", "intent", "compose", "tos",
             "privacy", "hashtag"}


# ---------------- 基础 HTTP ----------------
def _opener():
    p = vdl._get_proxy()
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": p, "https": p} if p else {}))


def _get(url, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with _opener().open(req, timeout=timeout) as r:
        return r.read()


def _get_json(url, headers=None, timeout=30):
    return json.loads(_get(url, headers, timeout).decode("utf-8", "ignore"))


# ---------------- 链接解析 ----------------
def extract_tweet_id(url: str):
    """从推文链接（x.com/twitter.com/*/status/<id>）或纯数字里取推文 ID。"""
    s = (url or "").strip()
    m = re.search(r"/(?:status|statuses)/(\d+)", s)
    if not m:
        m = re.fullmatch(r"(\d{10,25})", s)
    return m.group(1) if m else ""


def extract_handle(url: str):
    """从主页链接（x.com/<handle>）或 @handle / 裸 handle 取账号名。"""
    s = (url or "").strip()
    m = re.search(r"(?:twitter\.com|x\.com)/@?([A-Za-z0-9_]{1,15})", s, re.I)
    if m:
        h = m.group(1)
    elif s.startswith("@"):
        h = s[1:].split("/")[0].strip()
    else:
        h = s.split("?")[0].split("#")[0].split("/")[0].strip().lstrip("@")
    h = h.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) or h.lower() in _RESERVED:
        raise RuntimeError(f"无法识别 Twitter 账号名：{url!r}（示例：x.com/elonmusk 或 @elonmusk）")
    return h


def tweet_url(handle: str, tid: str) -> str:
    return f"https://x.com/{handle}/status/{tid}"


# ---------------- 单条推文：三通道归一化 ----------------
def _norm_fxtwitter(d):
    t = d.get("tweet") or d or {}
    text = (t.get("text") or "").strip()
    if not text:
        return None
    author = t.get("author") or {}
    media = (t.get("media") or {})
    imgs, video = [], ""
    for m in media.get("all") or []:
        if m.get("type") == "photo" and m.get("url"):
            imgs.append(m["url"])
        elif m.get("type") in ("video", "gif") and not video:
            video = m.get("url") or ""
    return {
        "id": str(t.get("id") or ""),
        "text": text,
        "author": author.get("name") or "",
        "handle": author.get("screen_name") or "",
        "date": _fmt_date((t.get("created_timestamp") and t["created_timestamp"])
                          or t.get("created_at")),
        "images": imgs,
        "video_url": video,
        "tweet_url": t.get("url") or "",
    }


def _norm_vxtwitter(d):
    text = (d.get("text") or "").strip()
    if not text:
        return None
    imgs, video = [], ""
    for m in d.get("media_extended") or []:
        if m.get("type") == "photo" and m.get("url"):
            imgs.append(m["url"])
        elif not video and m.get("url"):
            video = m.get("url") or ""
    for u in d.get("mediaURLs") or []:
        if u not in imgs and not video:
            video = u
    return {
        "id": str(d.get("tweetID") or ""),
        "text": text,
        "author": d.get("user_name") or "",
        "handle": d.get("user_screen_name") or "",
        "date": _fmt_date(d.get("created_at")),
        "images": imgs,
        "video_url": video,
        "tweet_url": d.get("tweetURL") or "",
    }


def _norm_synd(d):
    text = (d.get("text") or "").strip()
    if not text:
        return None
    user = d.get("user") or {}
    imgs, video = [], ""
    for m in d.get("mediaDetails") or []:
        if m.get("type") == "photo" and m.get("media_url_https"):
            imgs.append(m["media_url_https"])
    vi = d.get("video_info") or {}
    best = 0
    for v in vi.get("variants") or []:
        if (v.get("content_type") or "") == "video/mp4":
            br = v.get("bitrate") or 0
            if br >= best:
                best, video = br, v.get("url") or ""
    return {
        "id": str(d.get("id_str") or ""),
        "text": text,
        "author": user.get("name") or "",
        "handle": user.get("screen_name") or "",
        "date": _fmt_date(d.get("created_at")),
        "images": imgs,
        "video_url": video,
        "tweet_url": "",
    }


def fetch_tweet(tid: str) -> dict:
    """单条推文抓取，三通道依次尝试。返回归一化 dict。"""
    tid = str(tid or "").strip()
    if not tid:
        raise RuntimeError("推文 ID 为空")
    tries = [
        ("fxtwitter", f"https://api.fxtwitter.com/i/status/{tid}", _norm_fxtwitter),
        ("vxtwitter", f"https://api.vxtwitter.com/twitter/status/{tid}", _norm_vxtwitter),
        ("syndication",
         f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=a&lang=zh-cn",
         _norm_synd),
    ]
    errs = []
    for name, url, norm in tries:
        try:
            d = _get_json(url, timeout=25)
            tw = norm(d)
            if tw:
                tw.setdefault("id", tid)
                if not tw.get("tweet_url"):
                    tw["tweet_url"] = f"https://x.com/i/status/{tid}"
                return tw
            errs.append(f"{name}: 响应无正文")
        except Exception as e:
            errs.append(f"{name}: {type(e).__name__} {str(e)[:80]}")
    raise RuntimeError("推文抓取失败（三通道均不可用）：" + "；".join(errs)
                       + "。请检查代理（VIDEO_PROXY/proxy.txt）后重试")


# ---------------- 推文 -> Markdown ----------------
def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name or "").strip()
    return (name or "tweet")[:60]


def _fmt_date(v):
    """date 归一化：fxtwitter 可能给 epoch 秒，统一转 YYYY-MM-DD。"""
    s = str(v or "").strip()
    if re.fullmatch(r"\d{10}", s):
        import datetime
        try:
            return datetime.datetime.utcfromtimestamp(int(s)).strftime("%Y-%m-%d")
        except Exception:
            return s
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s[:19]


def _title_of(text: str) -> str:
    line = ((text or "").strip().splitlines() or [""])[0]
    line = re.sub(r"https?://\S+", "", line).strip(" \t#>-*")
    # 去掉开头的 @提及，让标题更像正文
    line = re.sub(r"^(?:@[A-Za-z0-9_]+\s+)+", "", line)
    return (line or "推文")[:40]


def _localize_images(images, title: str, img_dir):
    """下载图片到 img_dir，返回 [(kind, value)]；失败回落外链。"""
    refs = []
    if not images:
        return refs
    folder = None
    if img_dir:
        try:
            os.makedirs(img_dir, exist_ok=True)
            folder = img_dir
        except OSError:
            folder = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for i, u in enumerate([x for x in images if x], 1):
        ok_local = False
        dest = ""
        if folder and u:
            ext = ".jpg"
            m = re.search(r"\.(jpe?g|png|webp|gif)(?:[?!]|$)", u, re.I)
            if m:
                ext = "." + m.group(1).lower()
            dest = os.path.join(folder, f"tw-{i:02d}{ext}")
            try:
                req = urllib.request.Request(u, headers={
                    "User-Agent": UA,
                    "Referer": "https://x.com/",
                })
                with opener.open(req, timeout=20) as r, open(dest, "wb") as f:
                    f.write(r.read())
                if os.path.getsize(dest) > 1024:
                    ok_local = True
                else:
                    os.remove(dest)
            except Exception:
                if dest and os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
        if ok_local:
            rel = os.path.relpath(dest, os.path.dirname(img_dir)).replace("\\", "/")
            refs.append(("local", rel))
        else:
            refs.append(("url", u))
    return refs


def tweet_to_md(tw: dict, img_dir=None) -> str:
    """归一化推文 -> Obsidian Markdown（图片本地化）。"""
    title = _sanitize(_title_of(tw.get("text") or ""))
    imgs = _localize_images(tw.get("images") or [], title, img_dir)
    lines = [
        "---",
        f"source: Twitter",
        f"author: {tw.get('author') or ''}".rstrip(),
        f"handle: '@{tw.get('handle')}'" if tw.get("handle") else None,
        f"date: {tw.get('date') or ''}".rstrip(),
        f"url: {tw.get('tweet_url') or ''}".rstrip(),
        "---",
        "",
        f"# {title}",
        "",
        (tw.get("text") or "").strip(),
        "",
    ]
    lines = [l for l in lines if l is not None]
    for _kind, v in imgs:
        lines += [f"![图]({v})", ""]
    if tw.get("video_url"):
        lines += [f"> 🎬 视频推文：{tw['video_url']}",
                  "> 如需转录蒸馏，把该推文链接放进「采集流水线」视频类来源（yt-dlp 支持 x.com）。",
                  ""]
    lines += [f"原文：{tw.get('tweet_url') or ''}", ""]
    return "\n".join(lines)


def ingest_tweet(url: str, img_dir=None) -> dict:
    """推文链接 -> {platform:'twitter', title, md}。"""
    tid = extract_tweet_id(url)
    if not tid:
        raise RuntimeError("无法从链接识别推文 ID（支持 x.com/twitter.com 的 /status/ 链接）")
    tw = fetch_tweet(tid)
    md = tweet_to_md(tw, img_dir)
    return {"platform": "twitter", "title": _sanitize(_title_of(tw.get("text") or "")),
            "md": md, "tweet": tw}


# ---------------- 账号推文清单 ----------------
def _cookie_auth():
    """从 TWITTER_COOKIE 环境变量解析 auth_token / ct0（浏览器登录 x.com 后复制）。"""
    ck = os.environ.get("TWITTER_COOKIE") or ""
    def gv(name):
        m = re.search(r"(?:^|;\s*)" + name + r"=([^;]+)", ck)
        return m.group(1) if m else ""
    return gv("auth_token"), gv("ct0")


# UserTweets graphql 的 features 集（twscrape 等开源实现通用）
_GQL_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "articles_preview_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


def _graphql_user_tweets(handle: str, max_count: int):
    """L2：登录 cookie（TWITTER_COOKIE）+ GraphQL UserTweets（最可靠通道）。

    queryId 会随前端发版变化，这里运行时从 x.com 的 main.*.js bundle 里提取，
    不需要硬编码。
    """
    auth, ct0 = _cookie_auth()
    if not auth or not ct0:
        raise RuntimeError("no-cookie")
    headers = {
        "Cookie": f"auth_token={auth}; ct0={ct0}",
        "Authorization": WEB_BEARER,
        "X-Csrf-Token": ct0,
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "en",
    }
    # 1) 主页 HTML -> 用户数字 ID
    page = _get(f"https://x.com/{handle}", headers, 30).decode("utf-8", "ignore")
    m = re.search(r'"rest_id":"(\d+)"', page)
    if not m:
        raise RuntimeError("主页未解析到用户 ID（TWITTER_COOKIE 可能已失效）")
    uid = m.group(1)
    # 2) 前端 bundle -> UserTweets queryId
    mb = re.search(r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"\\]+?\.js',
                   page)
    if not mb:
        raise RuntimeError("未找到前端 bundle 链接")
    bundle = _get(mb.group(0), headers, 40).decode("utf-8", "ignore")
    qm = re.search(r'queryId:"([0-9A-Za-z_-]+)",operationName:"UserTweets"', bundle)
    if not qm:
        raise RuntimeError("bundle 中未提取到 UserTweets queryId")
    # 3) graphql 调用
    import urllib.parse
    variables = json.dumps({"userId": uid, "count": min(max(1, max_count), 20),
                            "includePromotedContent": False,
                            "withQuickPromoteEligibilityTweetFields": True,
                            "withVoice": False}, separators=(",", ":"))
    features = json.dumps(_GQL_FEATURES, separators=(",", ":"))
    url = (f"https://x.com/i/api/graphql/{qm.group(1)}/UserTweets"
           f"?variables={urllib.parse.quote(variables)}"
           f"&features={urllib.parse.quote(features)}")
    data = _get_json(url, headers, 30)
    out = []
    try:
        ins = (data["data"]["user"]["result"].get("timeline_v2")
               or data["data"]["user"]["result"]["timeline"])["timeline"]["instructions"]
        for inst in ins:
            for e in inst.get("entries") or []:
                item = ((e.get("content") or {}).get("itemContent") or {})
                res = (item.get("tweet_results") or {}).get("result") or {}
                if res.get("__typename") == "TweetWithVisibilityResults":
                    res = res.get("tweet") or {}
                legacy = res.get("legacy") or {}
                tid = legacy.get("id_str")
                if not tid:
                    continue
                txt = legacy.get("full_text") or ""
                if txt.startswith("RT @"):
                    continue  # 跳过纯转推
                out.append(tid)
    except Exception:
        pass
    if not out:
        raise RuntimeError("graphql 返回 0 条（cookie 失效或接口变更）")
    return out


def _twitter_cookies():
    """从环境变量 TWITTER_COOKIE 取 auth_token / ct0（浏览器登录 x.com 后复制 Cookie 串）。"""
    ck = (os.environ.get("TWITTER_COOKIE") or "").strip()
    def gv(name):
        m = re.search(r"(?:^|;\s*)" + name + r"=([^;\s]+)", ck)
        return m.group(1) if m else ""
    return gv("auth_token"), gv("ct0")


_COOKIE_HINT = ("需要登录凭据：环境变量 TWITTER_COOKIE 设为浏览器登录 x.com 后复制的 "
                "Cookie 串（须含 auth_token 与 ct0）")


def _cookie_user_tweets(handle: str, max_count: int):
    """登录 cookie + GraphQL UserTweets（最可靠）。
    queryId 不写死：运行时从 x.com 主页入口 JS bundle 里提取，抗版本变更。"""
    auth, ct0 = _twitter_cookies()
    if not auth or not ct0:
        raise RuntimeError(_COOKIE_HINT)
    h = {"Cookie": f"auth_token={auth}; ct0={ct0}",
         "Authorization": WEB_BEARER,
         "X-Csrf-Token": ct0,
         "X-Twitter-Active-User": "yes",
         "X-Twitter-Client-Language": "en",
         "Referer": f"https://x.com/{handle}"}
    page = _get(f"https://x.com/{handle}", h, 30).decode("utf-8", "ignore")
    m = re.search(r'"rest_id":"(\d+)"', page)
    if not m:
        raise RuntimeError("主页未解析到用户 ID（cookie 可能已失效，请重新复制 TWITTER_COOKIE）")
    uid = m.group(1)
    mb = re.search(r'https://abs\.twimg\.com/responsive-web/client-web/main[^"\\]+?\.js',
                   page)
    if not mb:
        raise RuntimeError("未找到 x.com 前端入口 JS（页面结构变化）")
    bundle = _get(mb.group(0).replace("&amp;", "&"), {"User-Agent": UA}, 30).decode(
        "utf-8", "ignore")
    qm = re.search(r'queryId:"([0-9A-Za-z_-]+)",operationName:"UserTweets"', bundle)
    if not qm:
        raise RuntimeError("前端 JS 中未找到 UserTweets queryId（X 前端结构变化）")
    variables = json.dumps(
        {"userId": uid, "count": min(max(1, max_count), 20),
         "includePromotedContent": False,
         "withQuickPromoteEligibilityTweetFields": True, "withVoice": False},
        separators=(",", ":"))
    url = ("https://x.com/i/api/graphql/" + qm.group(1) + "/UserTweets?variables=" +
           urllib.parse.quote(variables) + "&features=" + urllib.parse.quote(_GQL_FEATURES))
    data = _get_json(url, h, 30)
    return _parse_timeline_ids(data)


def _parse_timeline_ids(data):
    """GraphQL 响应 -> 原创推文 ID 列表（跳过转推）。"""
    out = []
    try:
        instr = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
    except Exception:
        instr = []
    for ins in instr if isinstance(instr, list) else []:
        for e in (ins.get("entries") or []) if isinstance(ins, dict) else []:
            ic = ((e.get("content") or {}).get("itemContent")) or {}
            tr = ((ic.get("tweet_results") or {}).get("result")) or {}
            if isinstance(tr.get("tweet"), dict):
                tr = tr["tweet"]
            leg = tr.get("legacy") or {}
            tid, txt = leg.get("id_str"), leg.get("full_text") or ""
            if tid and not txt.startswith("RT @"):
                out.append(str(tid))
    # 兜底：结构变化时全量扫 legacy.id_str（可能混入转推/引用）
    if not out:
        def _walk(o):
            if isinstance(o, dict):
                leg = o.get("legacy")
                if isinstance(leg, dict) and leg.get("id_str") and \
                        leg.get("full_text") is not None and \
                        not str(leg["full_text"]).startswith("RT @"):
                    out.append(str(leg["id_str"]))
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
        _walk(data)
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def _syndication_timeline_ids(handle: str):
    """syndication timeline 页面里正则提取该账号的推文 ID（免 cookie）。"""
    raw = _get(
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
        f"?showReplies=false", timeout=25).decode("utf-8", "ignore")
    pat = re.compile(
        r"(?:twitter\.com|x\.com)/" + re.escape(handle) +
        r"/status(?:es)?/(\d+)", re.I)
    ids, seen = [], set()
    for tid in pat.findall(raw):
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return ids


def _guest_timeline_ids(handle: str, max_count: int):
    """guest-token user_timeline 兜底（公开 web bearer）。"""
    tok = _get_json("https://api.twitter.com/1.1/guest/activate.json",
                    {"Authorization": WEB_BEARER}, 20)["guest_token"]
    data = _get_json(
        "https://api.twitter.com/1.1/statuses/user_timeline.json"
        f"?screen_name={handle}&count={min(max(1, max_count), 200)}"
        "&tweet_mode=extended&exclude_replies=true",
        {"Authorization": WEB_BEARER, "x-guest-token": tok}, 25)
    return [str(t.get("id_str") or t.get("id")) for t in data if t.get("id_str") or t.get("id")]


def list_user_tweets(handle_or_url: str, max_count: int = 50) -> dict:
    """账号推文清单（先清单、人工勾选后批量入库）。返回 {handle, count, tweets}。"""
    handle = extract_handle(handle_or_url)
    max_count = max(1, min(int(max_count or 50), 200))
    ids, errs = [], []
    # L0 syndication timeline（免 cookie，IP 级限流常见 429）
    try:
        ids = _syndication_timeline_ids(handle)
        if ids:
            errs.append(f"syndication: {len(ids)} 条")
    except Exception as e:
        errs.append(f"syndication: {type(e).__name__} {str(e)[:80]}")
    # L1 登录 cookie + GraphQL UserTweets（最可靠，需 TWITTER_COOKIE）
    if not ids:
        try:
            ids = _cookie_user_tweets(handle, max_count)
            if ids:
                errs.append(f"graphql(cookie): {len(ids)} 条")
        except RuntimeError as e:
            errs.append(f"graphql(cookie): {e}")
        except Exception as e:
            errs.append(f"graphql(cookie): {type(e).__name__} {str(e)[:80]}")
    # L2 guest-token user_timeline（官方已大多关闭，最后尝试）
    if not ids:
        try:
            ids = _guest_timeline_ids(handle, max_count)
            if ids:
                errs.append(f"guest: {len(ids)} 条")
        except Exception as e:
            errs.append(f"guest: {type(e).__name__} {str(e)[:80]}")
    if not ids:
        raise RuntimeError(
            f"获取 @{handle} 的推文清单失败（{'；'.join(errs)}）。"
            "可靠方案：浏览器登录 x.com 后复制 cookie，设为环境变量 TWITTER_COOKIE"
            "（需含 auth_token 与 ct0）再重启本服务；"
            "单条推文抓取无需任何配置——把推文链接粘贴到「链接清单」即可。")
    ids = ids[:max_count]

    def _one(tid):
        try:
            tw = fetch_tweet(tid)
            return {"id": tid, "url": tw.get("tweet_url") or tweet_url(handle, tid),
                    "text": (tw.get("text") or "")[:120], "date": tw.get("date") or ""}
        except Exception:
            return {"id": tid, "url": tweet_url(handle, tid), "text": "", "date": ""}

    with ThreadPoolExecutor(8) as ex:
        tweets = list(ex.map(_one, ids))
    return {"handle": handle, "count": len(tweets), "tweets": tweets}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if "/status/" in sys.argv[1]:
            out = ingest_tweet(sys.argv[1])
            print(out["title"], "\n----\n", out["md"][:800])
        else:
            d = list_user_tweets(sys.argv[1], 10)
            print(f"@{d['handle']} 共 {d['count']} 条")
            for t in d["tweets"]:
                print("-", t["date"], t["text"][:60])
