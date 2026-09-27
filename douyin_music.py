#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抖音音乐/歌曲采集器 —— 指定作品链接 → 本地音频/视频 → 可播放可管理，不做任何解析。

与 VOA 源的本质差异（决定了实现方式）：
- 抖音 CDN 直链有时效（几小时内失效）→ 必须「拦截到直链后立即下载落地」，
  不支持"只记链接后补媒体"
- 无逐句字幕 → 播放器是纯播放（大封面+连播），无卡拉OK同步

采集路径（单条）：
  playwright 拦截 aweme/v1/web/aweme/detail（复用 douyin_auto 的 cookie/反检测基建）
  → aweme_detail 里同时有：
    music.play_url.url_list   纯音频直链（音乐类作品大多有，直接下，~2MB）
    video.play_addr.url_list  视频直链（兜底：下载后 ffmpeg -vn 抽音轨）
    video.cover.url_list      封面
    author.nickname / desc / duration / create_time
  → 音频落地 <vault>/media/douyin/<aweme_id>.m4a|.mp3；视频落地 <id>.mp4（with_video=True 默认两者都存）
  → 封面落地 <vault>/images/douyin/<aweme_id>.jpg（Web 走 /images/ 路由）
  → 字幕两级：官方原生（自动字幕/创作者字幕）→ 本地 whisper 转写，落 <id>.lrc
  → 极简 Markdown 笔记（标题/号主/时长/封面/播放器链接/原链），platform=douyin_music

账号采集：复用 /api/video/profile（douyin_profile.py 拦截主页作品清单），
前端勾选后逐条走本模块下载入库（与「先过目再入库」原则一致）。
"""
import datetime
import io
import json
import os
import re
import subprocess

import douyin_auto
from douyin_auto import (_no_proxy_env, extract_video_url, fetch_direct_url,
                         get_ffmpeg, resolve_cookie_file, UA)

PLAYER_PORT = os.environ.get("PORT", "8788")
# 与 voa_player 同源的 vault 解析（避免循环 import app）
_ART_VAULT_DEFAULT = r"E:\Workbuddy\Claw\08-文章笔记"
ART_VAULT = (os.environ.get("ARTICLE_VAULT_DIR")
             or (_ART_VAULT_DEFAULT if os.path.isdir(os.path.dirname(_ART_VAULT_DEFAULT))
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "articles_vault")))

URL_ID_RE = re.compile(r"(?:video/|modal_id=)(\d{5,})")

_SRT_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")
_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\](.*)$")


def extract_id(url: str) -> str:
    """抖音作品链接 → aweme_id。支持 /video/<id>、modal_id=<id>、纯数字 id。"""
    u = (url or "").strip()
    m = URL_ID_RE.search(u)
    if m:
        return m.group(1)
    if u.isdigit():
        return u
    return ""


def is_douyin_music(url: str) -> bool:
    """链接属于抖音作品（由前端/流水线显式指派 platform，这里仅做兜底识别）。"""
    return "douyin.com" in (url or "").lower()


def _ext_from_url(u: str, default: str = ".m4a") -> str:
    m = re.search(r"\.(mp3|m4a|aac|mp4)(?:[?&#]|$)", (u or "").lower())
    ext = m.group(1) if m else default.lstrip(".")
    return ".mp3" if ext == "mp3" else ".m4a"


_ORIG_SOUND_RE = re.compile(r"^@(.+?)创作的原声$")


def _song_info(aw: dict) -> dict:
    """从 aweme 提取歌曲信息（歌名/歌手/专辑/原声识别）。

    曲库歌曲：matched_song.title（真歌名）> music.title
    原声：music.title 形如 "@xxx创作的原声"，抖音本身无歌名数据
    """
    music = aw.get("music") or {}
    ms = music.get("matched_song") or {}
    mt = (music.get("title") or "").strip()
    m_orig = _ORIG_SOUND_RE.match(mt)
    info = {"kind": "original", "song": "", "artist": "", "album": "",
            "orig_owner": m_orig.group(1) if m_orig else ""}
    if ms.get("title"):
        info.update(kind="song", song=(ms.get("title") or "").strip(),
                    artist=(ms.get("author") or "").strip(),
                    album=(ms.get("album") or "").strip())
    elif mt and not m_orig:
        # 曲库音乐但无 matched_song：music.title 即歌名
        info.update(kind="song", song=mt, artist=(music.get("author") or "").strip())
    return info


def _meta_from_aw(aw: dict) -> dict:
    """aweme_detail → 采集所需元数据。"""
    vid = str(aw.get("aweme_id") or "").strip()
    author = ((aw.get("author") or {}).get("nickname") or "").strip()
    music = aw.get("music") or {}
    song = _song_info(aw)
    music_urls = (music.get("play_url") or {}).get("url_list") or []
    cover_urls = ((aw.get("video") or {}).get("cover") or {}).get("url_list") or []
    duration_ms = 0
    for node in (aw.get("video") or {}).get("duration", 0), (music.get("duration") or 0):
        try:
            duration_ms = max(duration_ms, int(node or 0))
        except (TypeError, ValueError):
            pass
    create_time = ""
    ct = aw.get("create_time")
    if ct:
        try:
            create_time = datetime.datetime.fromtimestamp(int(ct)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    vurl, _src = extract_video_url(aw)
    caption = (aw.get("desc") or "").strip()
    # 文案清洗：去掉 #话题 标签与多余空白（话题不是歌名，只是噪音）
    clean_caption = re.sub(r"#[^\s#]+", "", caption)
    clean_caption = re.sub(r"\s{2,}", " ", clean_caption).strip()
    # 展示标题：曲库歌曲用「歌名 - 歌手」，原声用「正文（号主 原声）」兜底
    # （注：原声类抖音数据层无歌名，可从转写歌词/话题人工识别后改名）
    if song["kind"] == "song" and song["song"]:
        title = song["song"] + (f" - {song['artist']}" if song["artist"] else "")
    else:
        owner = song["orig_owner"] or author or "未知号主"
        title = f"{clean_caption}（{owner} 原声）" if clean_caption else f"{owner} 原声"
    return {
        "id": vid,
        "title": title or f"抖音 {vid}",
        "caption": caption,
        "song": song,
        "author": author,
        "music_url": music_urls[0] if music_urls else "",
        "video_url": vurl or "",
        "cover_url": cover_urls[0] if cover_urls else "",
        "duration_ms": duration_ms,
        "create_time": create_time,
        "url": f"https://www.douyin.com/video/{vid}" if vid else "",
    }


def _ffmpeg_download(url: str, out_path: str, timeout: int = 300) -> bool:
    """复用 douyin_auto.download（ffmpeg -c copy + Referer），音频/视频直链通用。"""
    return douyin_auto.download(url, out_path)


def _extract_audio(mp4_path: str, m4a_path: str) -> bool:
    """mp4 → m4a：优先 -c:a copy（AAC 直通，秒级），失败重编码 aac。"""
    env = _no_proxy_env()
    ff = get_ffmpeg()
    for extra in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):
        cmd = [ff, "-y", "-i", mp4_path, "-vn", *extra, m4a_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=300, env=env)
            if r.returncode == 0 and os.path.isfile(m4a_path) \
                    and os.path.getsize(m4a_path) > 10 * 1024:
                return True
        except Exception:
            continue
    return False


def download_media(meta: dict, vault_root: str, with_video: bool = True,
                   verbose: bool = True) -> dict:
    """按元数据落地媒体与封面到 vault，返回 {media_rel, video_rel, cover_rel}。

    with_video=True（默认）：视频+音频都落地——
      video/douyin 下 <id>.mp4（可看画面），音频 <id>.m4a|.mp3（纯音频直链优先，
      没有则从 mp4 抽音轨，mp4 保留不删）
    with_video=False：只留音频（抽轨后删 mp4，省空间）
    """
    vid = meta["id"]
    media_dir = os.path.join(vault_root, "media", "douyin")
    img_dir = os.path.join(vault_root, "images", "douyin")
    os.makedirs(media_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    media_rel = ""   # 音频文件（播放/转写主载体）
    video_rel = ""   # mp4（with_video 时保留）
    tmp_mp4 = os.path.join(media_dir, f"_dytmp_{vid}.mp4")
    mp4_dest = os.path.join(media_dir, f"{vid}.mp4")

    # 1) 音频：纯音频直链（音乐类作品大多有，秒级 ~2MB）
    if meta.get("music_url"):
        ext = _ext_from_url(meta["music_url"])
        dest = os.path.join(media_dir, f"{vid}{ext}")
        if _ffmpeg_download(meta["music_url"], dest) \
                and os.path.getsize(dest) > 20 * 1024:
            media_rel = os.path.relpath(dest, vault_root).replace("\\", "/")

    # 2) 视频：有直链就下载（with_video 保留为正式 mp4；否则仅作抽轨源）
    need_mp4 = bool(meta.get("video_url")) and (with_video or not media_rel)
    if need_mp4:
        if douyin_auto.download(meta["video_url"], tmp_mp4) \
                and os.path.getsize(tmp_mp4) > 100 * 1024:
            # 2a) 没有纯音频 → 从 mp4 抽音轨
            if not media_rel:
                m4a = os.path.join(media_dir, f"{vid}.m4a")
                if _extract_audio(tmp_mp4, m4a):
                    media_rel = os.path.relpath(m4a, vault_root).replace("\\", "/")
            # 2b) mp4 保留 or 删除
            if with_video:
                os.replace(tmp_mp4, mp4_dest)
                video_rel = os.path.relpath(mp4_dest, vault_root).replace("\\", "/")
            else:
                try:
                    os.remove(tmp_mp4)
                except OSError:
                    pass
        else:
            if os.path.exists(tmp_mp4):
                try:
                    os.remove(tmp_mp4)
                except OSError:
                    pass

    # 封面（失败不阻断）
    cover_rel = ""
    if meta.get("cover_url"):
        dest_cover = os.path.join(img_dir, f"{vid}.jpg")
        try:
            import urllib.request
            req = urllib.request.Request(meta["cover_url"], headers={
                "User-Agent": UA, "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=30) as r, \
                    open(dest_cover, "wb") as f:
                f.write(r.read())
            if os.path.getsize(dest_cover) > 1024:
                cover_rel = os.path.relpath(dest_cover, vault_root).replace("\\", "/")
            else:
                os.remove(dest_cover)
        except Exception:
            if os.path.exists(dest_cover):
                try:
                    os.remove(dest_cover)
                except OSError:
                    pass

    return {"media_rel": media_rel, "video_rel": video_rel,
            "cover_rel": cover_rel}


def media_paths(vault_root: str, aweme_id: str) -> dict:
    """检查某 aweme_id 的本地媒体现状（播放器/删除联动用）。"""
    media_dir = os.path.join(vault_root, "media", "douyin")
    img = os.path.join(vault_root, "images", "douyin", f"{aweme_id}.jpg")
    out = {"m4a": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.m4a")),
           "mp3": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.mp3")),
           "mp4": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.mp4")),
           "cover": os.path.isfile(img)}
    return out


def lrc_path(vault_root: str, aweme_id: str) -> str:
    return os.path.join(vault_root, "media", "douyin", f"{aweme_id}.lrc")


def has_lrc(vault_root: str, aweme_id: str) -> bool:
    return os.path.isfile(lrc_path(vault_root, aweme_id))


# ---------------- 字幕：两级取（官方原生 → 本地 whisper 转写） ----------------

def _http_get(url: str, timeout: int = 30) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.douyin.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_srt_vtt(blob: bytes) -> list:
    """SRT/VTT → [(start_sec, end_sec, text), ...]（时间戳小时位可省，VTT 常见）"""
    txt = blob.decode("utf-8", errors="replace").replace("\r\n", "\n")
    segs, cur = [], None  # [start, end, [lines]]
    for line in txt.split("\n"):
        m = _SRT_TS.search(line)
        if m:
            g = m.groups()
            def _sec(h, mi, s, ms):
                return (int(h or 0)) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0
            s = _sec(g[0], g[1], g[2], g[3])
            e = _sec(g[4], g[5], g[6], g[7])
            if cur:
                segs.append(cur)
            cur = [s, e, []]
        elif cur is not None:
            t = line.strip()
            if not t:
                if cur[2]:
                    segs.append(cur)
                    cur = None
            elif not t.isdigit() and t != "WEBVTT":
                cur[2].append(t)
    if cur and cur[2]:
        segs.append(cur)
    return [(round(s, 2), round(e, 2), " ".join(ls)) for s, e, ls in segs]


def _parse_caption_json(blob: bytes) -> list:
    """抖音自动字幕 JSON（utterances: start_time/end_time 毫秒 + text）"""
    j = json.loads(blob.decode("utf-8", errors="replace"))
    utts = j.get("utterances") if isinstance(j, dict) else None
    if not utts:
        return []
    out = []
    for u in utts:
        txt = (u.get("text") or "").strip()
        if not txt:
            continue
        try:
            s = float(u.get("start_time") or 0) / 1000.0
            e = float(u.get("end_time") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        out.append((round(s, 2), round(e, 2), txt))
    return out


def _native_captions(aw: dict) -> list:
    """从 aweme_detail 提取官方原生字幕（yt-dlp DouyinIE 同款两级）。

    ① interaction_stickers[*].auto_video_caption_info.auto_captions[*]
       （平台 ASR 自动字幕；utterances 内联或 url 指向 JSON）
    ② video.cla_info.caption_infos[*].url（创作者/平台字幕，srt/webvtt 优先）
    返回 [(start_sec, end_sec, text), ...]，无则 []。
    """
    candidates = []  # (priority, kind, payload)
    for st in (aw.get("interaction_stickers") or []):
        cap_info = st.get("auto_video_caption_info") or {}
        for cap in (cap_info.get("auto_captions") or []):
            lang = (cap.get("LanguageCodeName") or cap.get("lang") or "").lower()
            pri = 0 if lang.startswith("zh") else 1
            if cap.get("url"):
                candidates.append((pri, "url", cap["url"]))
            utts = cap.get("utterances")
            if utts:
                candidates.append((pri, "inline_json",
                                   json.dumps({"utterances": utts}).encode()))
    cla = ((aw.get("video") or {}).get("cla_info") or {}).get("caption_infos") or []
    for cap in cla:
        u, fmt = cap.get("url") or "", (cap.get("Format") or cap.get("format") or "").lower()
        if not u:
            continue
        pri = 0 if fmt in ("srt", "webvtt") else 1
        candidates.append((pri, "srt_vtt" if fmt in ("srt", "webvtt") else "maybe_json", u))
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0])
    for _pri, kind, payload in candidates:
        try:
            blob = payload.encode() if isinstance(payload, str) and kind == "inline_json" \
                else _http_get(payload) if isinstance(payload, str) else payload
            if kind == "inline_json":
                segs = _parse_caption_json(blob)
            elif kind == "srt_vtt":
                segs = _parse_srt_vtt(blob)
            else:  # maybe_json：先试 utterances JSON，再试 srt/vtt
                segs = _parse_caption_json(blob) or _parse_srt_vtt(blob)
            if segs:
                return segs
        except Exception:
            continue
    return []


def _write_lrc(vault_root: str, aweme_id: str, segs: list) -> str:
    """[(s,e,text)] → media/douyin/<id>.lrc（VOA 同格式 [mm:ss.xx]text），返回 rel path。"""
    lines = []
    for s, _e, txt in sorted(segs, key=lambda x: x[0]):
        total = int(round(float(s) * 100))  # 厘秒
        mm, rem = divmod(total, 6000)
        ss, cs = divmod(rem, 100)
        lines.append(f"[{mm:02d}:{ss:02d}.{cs:02d}]{txt}")
    dest = lrc_path(vault_root, aweme_id)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with io.open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.relpath(dest, vault_root).replace("\\", "/")


def transcribe_lrc(vault_root: str, aweme_id: str, verbose: bool = True) -> list:
    """本地 whisper 转写已有音频 → 写 .lrc。返回 [(s,e,text), ...]；无音频/失败抛 RuntimeError。"""
    media_dir = os.path.join(vault_root, "media", "douyin")
    src = None
    for ext in (".m4a", ".mp3", ".mp4"):
        p = os.path.join(media_dir, f"{aweme_id}{ext}")
        if os.path.isfile(p):
            src = p
            break
    if not src:
        raise RuntimeError("本地无音频/视频文件，无法转写（请先采集）")
    import video_pipeline as vp
    segs, _lang, err = vp.transcribe_segments(src)
    if err:
        raise RuntimeError(f"转写失败：{err}")
    if not segs:
        raise RuntimeError("转写结果为空（可能是纯音乐无人声，抖音源本身无歌词）")
    if verbose:
        print(f"[douyin_music] whisper 转写 {aweme_id}: {len(segs)} 句", flush=True)
    _write_lrc(vault_root, aweme_id, segs)
    return segs


def ingest(url: str, vault_root: str = None, with_video: bool = True,
           verbose: bool = True, transcribe: bool = True) -> dict:
    """单条抖音作品链接 → 下载媒体（视频+音频）+ 字幕（两级）+ 极简 Markdown。

    失败抛 RuntimeError。字幕：官方原生（自动字幕/创作者字幕）优先；
    没有且 transcribe=True 时本地 whisper 转写（时间线真实、文字为 AI 听写，
    歌词全文写入 md 便于人工识别歌名）。来源固定「抖音音乐」，号主另存 md。
    """
    if not vault_root:
        vault_root = ART_VAULT
    # playwright 拦截 aweme_detail（拿全量字段：music/cover/author/duration）
    aw = fetch_direct_url(url, return_full=True, verbose=verbose)
    meta = _meta_from_aw(aw)
    if not meta["id"]:
        raise RuntimeError("aweme_detail 中无 aweme_id")
    if not meta["music_url"] and not meta["video_url"]:
        raise RuntimeError("aweme_detail 中既无音频直链也无视频直链（可能需登录/风控）")

    files = download_media(meta, vault_root, with_video=with_video)
    if not files["media_rel"]:
        raise RuntimeError("媒体下载失败（直链可能过期或被风控，稍后重试）")

    vid = meta["id"]
    title = meta["title"]
    author = meta["author"] or "未知号主"
    dur_s = meta["duration_ms"] // 1000 if meta["duration_ms"] else 0
    dur_txt = f"{dur_s // 60}:{dur_s % 60:02d}" if dur_s else "未知"

    # 字幕两级取：官方原生 → whisper 本地转写
    sub_src = ""
    lyric_lines = []  # 歌词/字幕纯文本（写入 md 方便识别歌名）
    try:
        segs = _native_captions(aw)
        if segs:
            _write_lrc(vault_root, vid, segs)
            sub_src = "官方字幕"
            lyric_lines = [t for _s, _e, t in sorted(segs, key=lambda x: x[0])]
            if verbose:
                print(f"[douyin_music] 官方原生字幕 {vid}: {len(segs)} 句", flush=True)
        elif transcribe:
            try:
                segs2 = transcribe_lrc(vault_root, vid, verbose=verbose)
                sub_src = f"AI转写({len(segs2)}句)"
                lyric_lines = [t for _s, _e, t in segs2]
            except RuntimeError as te:
                if verbose:
                    print(f"[douyin_music] 转写跳过: {te}", flush=True)
                sub_src = "无字幕源"
    except Exception as se:
        if verbose:
            print(f"[douyin_music] 字幕环节异常（不阻断）: {se}", flush=True)

    md = "\n".join([
        "---", "kind: article", "platform: douyin_music", f"aweme_id: {vid}",
        f"source: {author}", f"date: {meta['create_time'] or datetime.date.today().isoformat()}",
        f"url: {meta['url']}", "---", "",
        f"# {title}", "",
    ])
    if files["cover_rel"]:
        md += f"![封面]({files['cover_rel']})\n\n"
    song = meta.get("song") or {}
    song_line = ""
    if song.get("kind") == "song" and song.get("song"):
        song_line = f"- 🎼 歌曲：{song['song']}"
        if song.get("artist"):
            song_line += f" · 歌手：{song['artist']}"
        if song.get("album"):
            song_line += f" · 专辑：{song['album']}"
    elif song.get("orig_owner"):
        song_line = f"- 🎙 抖音原声：@{song['orig_owner']}创作的原声（抖音无歌名/歌词数据）"
    media_src = f"/media/{os.path.basename(os.path.dirname(files['media_rel']))}/{os.path.basename(files['media_rel'])}"
    video_line = ""
    if files.get("video_rel"):
        video_line = f"- 🎬 视频：[{os.path.basename(files['video_rel'])}]({files['video_rel']})"
    md += "\n".join([
        f"- 🎵 音频：[{os.path.basename(files['media_rel'])}]({files['media_rel']})",
        *( [video_line] if video_line else [] ),
        *( [song_line] if song_line else [] ),
        f"- 📝 字幕：{sub_src or '无'}"
        + ("（AI 听写，个别字可能有误）" if sub_src.startswith("AI") else ""),
        f"- 👤 号主：{author} · ⏱ 时长：{dur_txt}"
        + (f" · 📅 {meta['create_time']}" if meta["create_time"] else ""),
        f"- ▶️ 播放器：[在线播放](http://127.0.0.1:{PLAYER_PORT}/player/douyin/{vid})",
        f"- 🔗 原链：{meta['url']}", "",
    ])
    # 歌词/字幕全文（原声类抖音数据层无歌名，贴出歌词便于人工识别歌名后改名）
    if lyric_lines:
        md += "## 🎙 歌词（来自字幕，AI 听写可能有误）\n\n" + "\n".join(lyric_lines) + "\n\n"
    return {"title": title, "md": md, "platform": "douyin_music",
            "source": "抖音音乐", "author": author, "aweme_id": vid,
            "subtitle": sub_src,
            "media_rel": files["media_rel"], "video_rel": files.get("video_rel", ""),
            "cover_rel": files["cover_rel"],
            "duration_sec": dur_s, "url": meta["url"]}


def cleanup_media(vault_root: str, aweme_id: str) -> list:
    """删除某 aweme_id 的本地媒体/字幕/封面（记录删除联动）。"""
    removed = []
    media_dir = os.path.join(vault_root, "media", "douyin")
    for ext in (".m4a", ".mp3", ".mp4", ".lrc"):
        fp = os.path.join(media_dir, f"{aweme_id}{ext}")
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                removed.append(fp)
            except OSError:
                pass
    img = os.path.join(vault_root, "images", "douyin", f"{aweme_id}.jpg")
    if os.path.isfile(img):
        try:
            os.remove(img)
            removed.append(img)
        except OSError:
            pass
    return removed


if __name__ == "__main__":
    import sys as _sys
    import json as _json
    if len(_sys.argv) < 2:
        print("用法: python douyin_music.py <抖音作品链接或ID> [audio_only]")
        _sys.exit(1)
    out = ingest(_sys.argv[1], with_video=len(_sys.argv) <= 2)
    print(_json.dumps({k: v for k, v in out.items() if k != "md"},
                      ensure_ascii=False, indent=2))
