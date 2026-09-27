# -*- coding: utf-8 -*-
"""抖音音乐播放器（服务端渲染自包含 HTML，纯播放不做解析）。

与 VOA 学习播放器的差异：抖音内容无逐句字幕 → 砍掉卡拉OK同步，做加法：
- 音频模式（m4a/mp3）：大封面海报 + 进度条（听歌形态）
- 视频模式（mp4）：video 元素 + poster
- 播放列表连播：同号主（或全部）自动下一首，点列表切换
- 单曲循环 / 倍速 / 进度记忆（localStorage，续播）
数据源：vault media/douyin/<aweme_id>.m4a|.mp3|.mp4 + images/douyin/<aweme_id>.jpg
（由 douyin_music.ingest 产出；Web 走 /media/ 与 /images/ 路由）
"""
import io
import json
import os
import re

# 与 app.py / douyin_music.py 保持同一 vault 解析逻辑
_ART_VAULT_DEFAULT = r"E:\Workbuddy\Claw\08-文章笔记"
ART_VAULT = (os.environ.get("ARTICLE_VAULT_DIR")
             or (_ART_VAULT_DEFAULT if os.path.isdir(os.path.dirname(_ART_VAULT_DEFAULT))
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "articles_vault")))

_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\](.*)$")


def parse_lrc(path: str):
    """LRC → [(t, text), ...]（抖音单行文本格式；VOA 双行双语格式兼容：取 en 行）。"""
    out = []
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LRC_LINE.match(line.strip())
            if not m:
                continue
            mm, ss, frac, text = m.groups()
            t = int(mm) * 60 + int(ss)
            if frac:
                t += int(frac.ljust(3, "0")[:3]) / 1000.0
            text = text.strip()
            if text:
                out.append((round(t, 2), text))
    # 同一时间戳连续两行（双语）只取第一行，避免重复显示
    merged, last_t = [], None
    for t, text in out:
        if last_t is not None and abs(t - last_t) <= 0.05 and merged:
            continue
        merged.append((t, text))
        last_t = t
    return merged


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def media_src(aweme_id: str):
    """返回 (src_url, kind) 或 (None, None)。kind: audio | video"""
    media_dir = os.path.join(ART_VAULT, "media", "douyin")
    if os.path.isfile(os.path.join(media_dir, f"{aweme_id}.mp4")):
        return f"/media/douyin/{aweme_id}.mp4", "video"
    for ext in (".m4a", ".mp3"):
        if os.path.isfile(os.path.join(media_dir, f"{aweme_id}{ext}")):
            return f"/media/douyin/{aweme_id}{ext}", "audio"
    return None, None


def render(aweme_id: str, title: str = "", author: str = "",
           playlist=None, sents=None, sub_source: str = ""):
    """返回 (http_code, html)。

    playlist: [{aid,title,vid,cur}]（当前条 cur=True）
    sents: [(t, text), ...] 字幕句（有则卡拉OK同步区，无则提示可补转写）
    sub_source: 字幕来源标注（官方字幕 / AI转写）
    """
    aweme_id = str(aweme_id).strip()
    src, kind = media_src(aweme_id)
    has_cover = os.path.isfile(os.path.join(ART_VAULT, "images", "douyin",
                                            f"{aweme_id}.jpg"))
    if not src:
        return 404, ("<h1>本地媒体缺失</h1>"
                     f"<p>期望位置：media/douyin/{_esc(aweme_id)}.m4a/.mp3/.mp4。"
                     "请先通过「🎵 抖音音乐」采集该条目。</p>")
    title = title or f"抖音 {aweme_id}"
    author = author or "未知号主"
    poster = f"/images/douyin/{aweme_id}.jpg" if has_cover else ""
    data = json.dumps(playlist or [], ensure_ascii=False).replace("</", "<\\/")
    poster_json = json.dumps(poster).replace("</", "<\\/")

    if kind == "video":
        media_el = (f'<video id="au" controls playsinline '
                    f'{"poster=" + chr(34) + _esc(poster) + chr(34) if poster else ""} '
                    f'src="{_esc(src)}"></video>')
    else:
        media_el = f'<audio id="au" controls src="{_esc(src)}"></audio>'

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · 抖音音乐播放器</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2333;--mut:#7a8299;--acc:#1a66d6;--line:#e3e7ee}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--mut);font-size:13px;margin-bottom:14px}
.grid{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
.main{flex:1 1 480px;min-width:320px}
.side{flex:0 0 280px;max-width:280px}
.cover{width:100%;border-radius:12px;display:block;background:#000;object-fit:contain;max-height:52vh;margin-bottom:10px}
audio{width:100%;margin-bottom:10px}
video{width:100%;max-height:52vh;border-radius:12px;background:#000;margin-bottom:10px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 14px;font-size:13.5px;cursor:pointer;color:var(--fg)}
button:hover{border-color:var(--acc);color:var(--acc)}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
select{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px;background:#fff;color:var(--fg)}
.pl{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.pl-h{padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px;color:var(--mut);display:flex;justify-content:space-between;align-items:center}
.row{display:flex;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer;align-items:center}
.row:last-child{border-bottom:0}
.row:hover{background:#f0f4ff}
.row.cur{background:#fff3cd;box-shadow:inset 3px 0 0 #e6a700;font-weight:600}
.row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px}
.subs{border:1px solid var(--line);border-radius:10px;overflow:hidden;max-height:46vh;overflow-y:auto}
.srow{display:flex;gap:10px;padding:9px 16px;border-bottom:1px solid var(--line);cursor:pointer;align-items:baseline}
.srow:last-child{border-bottom:0}
.srow:hover{background:#f0f4ff}
.srow.cur{background:#fff3cd;box-shadow:inset 3px 0 0 #e6a700}
.srow.cur .st{color:#b45309;font-weight:700}
.srow.cur .tm{color:#e6a700;font-weight:600}
.srow .tm{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums;min-width:44px;flex:none}
.srow .st{flex:1}
.hint{color:var(--mut);font-size:12.5px;margin:10px 4px}
.kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;background:#fff;font-size:11.5px}
@media (max-width:760px){ .side{flex:1 1 100%;max-width:none} }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎵 __TITLE__</h1>
  <div class="sub">👤 __AUTHOR__ · 抖音音乐播放器 · 内容版权归原号主/音乐人（仅个人收听）</div>
  <div class="grid">
    <div class="main">
      <div class="card">
__MEDIA__
        <div class="bar">
          <span style="font-size:13px;color:var(--mut)">倍速</span>
          <select id="rate">
            <option value="0.75">0.75×</option>
            <option value="1" selected>1.0×</option>
            <option value="1.25">1.25×</option>
            <option value="1.5">1.5×</option>
            <option value="2">2.0×</option>
          </select>
          <button id="bLoop" title="本曲播完自动重播">🔁 单曲循环</button>
          <button id="bForget" title="清除本曲进度记忆，下次从头播放">⏮ 忘记进度</button>
        </div>
        <div class="hint"><span class="kbd">Space</span> 播放/暂停 · <span class="kbd">←</span><span class="kbd">→</span> 快退/快进 10 秒 · 进度自动记忆，下次续播</div>
      </div>
__SUBSBOX__
    </div>
    <div class="side">
      <div class="pl" id="plbox">
        <div class="pl-h"><span>📀 播放列表（__PLN__）</span><span id="plmode"></span></div>
        <div id="plrows" style="max-height:420px;overflow:auto"></div>
      </div>
    </div>
  </div>
</div>
<script>
const au = document.getElementById("au");
const CUR = "__VID__";
const PL = __PL__;
const POSTER = __POSTER__;
const LSKEY = "dym_pos_" + CUR;
// 进度记忆：timeupdate 节流落 localStorage，loadedmetadata 恢复
let lastSave = 0;
au.addEventListener("timeupdate", () => {
  const now = Date.now();
  if (now - lastSave > 2000 && au.currentTime > 1 && !au.ended) {
    lastSave = now;
    try { localStorage.setItem(LSKEY, String(au.currentTime)); } catch(e) {}
  }
});
au.addEventListener("loadedmetadata", () => {
  try {
    const v = parseFloat(localStorage.getItem(LSKEY));
    if (v > 1 && v < au.duration - 3) au.currentTime = v;
  } catch(e) {}
});
au.addEventListener("ended", () => {
  try { localStorage.removeItem(LSKEY); } catch(e) {}
  if (!loop) playNext();
});
// 渲染播放列表
const plrows = document.getElementById("plrows");
if (PL.length) {
  const frag = document.createDocumentFragment();
  PL.forEach(it => {
    const row = document.createElement("div");
    row.className = "row" + (it.vid === CUR ? " cur" : "");
    row.innerHTML = '<span class="t" title="' + it.title.replace(/"/g,"&quot;") + '">' + it.title + "</span>";
    row.onclick = () => { if (it.vid !== CUR) location.href = "/player/douyin/" + it.aid; };
    frag.appendChild(row);
  });
  plrows.appendChild(frag);
  const same = PL.every(x => x.src === PL[0].src);
  document.getElementById("plmode").textContent = PL[0].src ? ("号主：" + PL[0].src) : "";
} else {
  document.getElementById("plbox").style.display = "none";
}
function playNext(){
  const i = PL.findIndex(x => x.vid === CUR);
  if (i >= 0 && i + 1 < PL.length) location.href = "/player/douyin/" + PL[i+1].aid;
}
// 单曲循环
let loop = false;
const bLoop = document.getElementById("bLoop");
bLoop.onclick = () => { loop = !loop; bLoop.classList.toggle("on", loop); };
// 倍速
document.getElementById("rate").onchange = e => { au.playbackRate = +e.target.value; };
// 忘记进度
document.getElementById("bForget").onclick = () => {
  try { localStorage.removeItem(LSKEY); } catch(e) {}
  au.currentTime = 0;
};
// 键盘
document.addEventListener("keydown", e => {
  if (e.target.tagName === "SELECT") return;
  if (e.code === "Space") { e.preventDefault(); au.paused ? au.play().catch(()=>{}) : au.pause(); }
  else if (e.key === "ArrowLeft") au.currentTime = Math.max(0, au.currentTime - 10);
  else if (e.key === "ArrowRight") au.currentTime = Math.min(au.duration || 0, au.currentTime + 10);
});
// ---------- 字幕卡拉OK同步（播放到哪句，哪句高亮；点击句子跳播） ----------
const SENTS = __SENTS__;
const subsEl = document.getElementById("subs");
let curS = -1;
function fmtT(t){ t = Math.round(t); return String(Math.floor(t/60)).padStart(2,"0") + ":" + String(t%60).padStart(2,"0"); }
if (subsEl && SENTS.length) {
  const frag = document.createDocumentFragment();
  SENTS.forEach((s, i) => {
    const row = document.createElement("div");
    row.className = "srow";
    row.innerHTML = '<span class="tm">' + fmtT(s[0]) + '</span><span class="st">' + s[1] + '</span>';
    row.onclick = () => { au.currentTime = s[0] + 0.01; if (au.paused) au.play().catch(()=>{}); };
    frag.appendChild(row);
  });
  subsEl.appendChild(frag);
  // 字幕容器内滚动：高亮句滚到容器中部，页面（含视频）保持不动
function keepInView(box, el){
  const br = box.getBoundingClientRect(), er = el.getBoundingClientRect();
  if (er.top < br.top + 10 || er.bottom > br.bottom - 10)
    box.scrollTop += er.top - br.top - (box.clientHeight - er.height) / 2;
}
au.addEventListener("timeupdate", () => {
    const t = au.currentTime;
    if (t <= 0) return;
    let i = curS < 0 ? 0 : curS;
    while (i + 1 < SENTS.length && t >= SENTS[i+1][0]) i++;
    while (i > 0 && t < SENTS[i][0]) i--;
    if (i !== curS) {
      curS = i;
      const rows = subsEl.children;
      for (let k = 0; k < rows.length; k++) rows[k].classList.toggle("cur", k === i);
      keepInView(subsEl, rows[i]);
    }
  });
}
</script>
</body>
</html>"""
    pl_n = f"{len(playlist or [])} 首" if playlist else "0 首"
    sents = list(sents or [])
    if sents:
        subs_box = ('<div class="card"><div class="bar" style="margin-bottom:8px">'
                    '<b style="font-size:14px">📝 字幕（共 ' + str(len(sents)) + ' 句）</b>'
                    '<span class="hint" style="margin:0">' + _esc(sub_source or "") + '</span>'
                    '<span style="flex:1"></span>'
                    '<span class="hint" style="margin:0">点击句子跳播 · 自动跟踪高亮</span></div>'
                    '<div class="subs" id="subs"></div></div>')
    else:
        subs_box = ('<div class="hint" style="margin:4px 2px">📝 暂无字幕——抖音源多数作品无原生字幕。'
                    '可在记录中心点「📝 转写」用本地 AI 生成（有原生字幕的作品采集时已自动抓取）。</div>')
    sents_data = json.dumps([[t, x] for t, x in sents],
                            ensure_ascii=False).replace("</", "<\\/")
    html = (html.replace("__TITLE__", _esc(title))
                .replace("__AUTHOR__", _esc(author))
                .replace("__MEDIA__", media_el)
                .replace("__VID__", aweme_id)
                .replace("__PL__", data)
                .replace("__POSTER__", poster_json)
                .replace("__PLN__", pl_n)
                .replace("__SUBSBOX__", subs_box)
                .replace("__SENTS__", sents_data))
    return 200, html
