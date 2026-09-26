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
  → 音频落地 <vault>/media/douyin/<aweme_id>.m4a|.mp3（audio_only=True 时删中间 mp4）
  → 封面落地 <vault>/images/douyin/<aweme_id>.jpg（Web 走 /images/ 路由）
  → 极简 Markdown 笔记（标题/号主/时长/封面/播放器链接/原链），platform=douyin_music

账号采集：复用 /api/video/profile（douyin_profile.py 拦截主页作品清单），
前端勾选后逐条走本模块下载入库（与「先过目再入库」原则一致）。
"""
import datetime
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
    # 展示标题：曲库歌曲用「歌名 - 歌手」，原声用「文案（号主 原声）」兜底
    if song["kind"] == "song" and song["song"]:
        title = song["song"] + (f" - {song['artist']}" if song["artist"] else "")
    else:
        owner = song["orig_owner"] or author or "未知号主"
        title = f"{caption}（{owner} 原声）" if caption else f"{owner} 原声"
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


def download_media(meta: dict, vault_root: str, audio_only: bool = True,
                   verbose: bool = True) -> dict:
    """按元数据落地媒体与封面到 vault，返回 {media_rel, cover_rel, keep_mp4}。

    media/douyin/<id>.m4a|.mp3|.mp4 + images/douyin/<id>.jpg
    """
    vid = meta["id"]
    media_dir = os.path.join(vault_root, "media", "douyin")
    img_dir = os.path.join(vault_root, "images", "douyin")
    os.makedirs(media_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)

    media_rel = ""
    keep_mp4 = not audio_only
    tmp_mp4 = os.path.join(media_dir, f"_dytmp_{vid}.mp4")

    # 1) 纯音频直链（音乐类作品大多有）：直接下，秒级且 ~2MB
    if meta.get("music_url"):
        ext = _ext_from_url(meta["music_url"])
        dest = os.path.join(media_dir, f"{vid}{ext}")
        if _ffmpeg_download(meta["music_url"], dest) \
                and os.path.getsize(dest) > 20 * 1024:
            media_rel = os.path.relpath(dest, vault_root).replace("\\", "/")

    # 2) 兜底：视频直链 → mp4 → 抽音轨（audio_only 时删 mp4，保留则改名为正式文件）
    if not media_rel and meta.get("video_url"):
        if douyin_auto.download(meta["video_url"], tmp_mp4) \
                and os.path.getsize(tmp_mp4) > 100 * 1024:
            m4a = os.path.join(media_dir, f"{vid}.m4a")
            if _extract_audio(tmp_mp4, m4a):
                if keep_mp4:
                    dest = os.path.join(media_dir, f"{vid}.mp4")
                    os.replace(tmp_mp4, dest)
                    media_rel = os.path.relpath(dest, vault_root).replace("\\", "/")
                else:
                    try:
                        os.remove(tmp_mp4)
                    except OSError:
                        pass
                    media_rel = os.path.relpath(m4a, vault_root).replace("\\", "/")
            elif keep_mp4:
                dest = os.path.join(media_dir, f"{vid}.mp4")
                if os.path.exists(tmp_mp4):
                    os.replace(tmp_mp4, dest)
                    media_rel = os.path.relpath(dest, vault_root).replace("\\", "/")
            else:
                # audio_only 但抽不出音轨：保留 mp4 兜底（能听能看总比丢好）
                if os.path.exists(tmp_mp4):
                    dest = os.path.join(media_dir, f"{vid}.mp4")
                    os.replace(tmp_mp4, dest)
                    media_rel = os.path.relpath(dest, vault_root).replace("\\", "/")
                    keep_mp4 = True
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

    return {"media_rel": media_rel, "cover_rel": cover_rel, "keep_mp4": keep_mp4}


def media_paths(vault_root: str, aweme_id: str) -> dict:
    """检查某 aweme_id 的本地媒体现状（播放器/删除联动用）。"""
    media_dir = os.path.join(vault_root, "media", "douyin")
    img = os.path.join(vault_root, "images", "douyin", f"{aweme_id}.jpg")
    out = {"m4a": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.m4a")),
           "mp3": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.mp3")),
           "mp4": os.path.isfile(os.path.join(media_dir, f"{aweme_id}.mp4")),
           "cover": os.path.isfile(img)}
    return out


def ingest(url: str, vault_root: str = None, audio_only: bool = True,
           verbose: bool = True) -> dict:
    """单条抖音作品链接 → 下载媒体 + 极简 Markdown。失败抛 RuntimeError。"""
    if not vault_root:
        vault_root = ART_VAULT
    # playwright 拦截 aweme_detail（拿全量字段：music/cover/author/duration）
    aw = fetch_direct_url(url, return_full=True, verbose=verbose)
    meta = _meta_from_aw(aw)
    if not meta["id"]:
        raise RuntimeError("aweme_detail 中无 aweme_id")
    if not meta["music_url"] and not meta["video_url"]:
        raise RuntimeError("aweme_detail 中既无音频直链也无视频直链（可能需登录/风控）")

    files = download_media(meta, vault_root, audio_only=audio_only)
    if not files["media_rel"]:
        raise RuntimeError("媒体下载失败（直链可能过期或被风控，稍后重试）")

    vid = meta["id"]
    title = meta["title"]
    author = meta["author"] or "未知号主"
    dur_s = meta["duration_ms"] // 1000 if meta["duration_ms"] else 0
    dur_txt = f"{dur_s // 60}:{dur_s % 60:02d}" if dur_s else "未知"

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
    md += "\n".join([
        f"- 🎵 音频：[{os.path.basename(files['media_rel'])}]({files['media_rel']})",
        *( [song_line] if song_line else [] ),
        f"- 👤 号主：{author} · ⏱ 时长：{dur_txt}"
        + (f" · 📅 {meta['create_time']}" if meta["create_time"] else ""),
        f"- ▶️ 播放器：[在线播放](http://127.0.0.1:{PLAYER_PORT}/player/douyin/{vid})",
        f"- 🔗 原链：{meta['url']}", "",
    ])
    return {"title": title, "md": md, "platform": "douyin_music",
            "source": author, "aweme_id": vid,
            "media_rel": files["media_rel"], "cover_rel": files["cover_rel"],
            "duration_sec": dur_s, "url": meta["url"]}


def cleanup_media(vault_root: str, aweme_id: str) -> list:
    """删除某 aweme_id 的本地媒体与封面（记录删除联动）。"""
    removed = []
    media_dir = os.path.join(vault_root, "media", "douyin")
    for ext in (".m4a", ".mp3", ".mp4"):
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
        print("用法: python douyin_music.py <抖音作品链接或ID> [keep_mp4]")
        _sys.exit(1)
    keep = len(_sys.argv) > 2 and _sys.argv[2] == "keep_mp4"
    out = ingest(_sys.argv[1], audio_only=not keep)
    print(_json.dumps({k: v for k, v in out.items() if k != "md"},
                      ensure_ascii=False, indent=2))
