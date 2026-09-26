# -*- coding: utf-8 -*-
"""VOA 逐句学习播放器（服务端渲染自包含 HTML）。

功能（按用户需求定制，只做学习呈现、不做任何解析任务）：
- 音频 + 逐句双语字幕卡拉OK同步（当前句高亮 + 自动滚动）
- 点击句子跳播；单句循环（精听）
- 倍速 0.6~1.5
- 翻译三态：显示 / 遮掩（模糊，悬停偷看）/ 隐藏
数据源：vault 内 media/voa/voa_<id>.lrc + voa_<id>.mp3（由 voa_ingest 产出）。
"""
import io
import json
import os
import re

# 与 app.py 保持同一 vault 解析逻辑（避免循环 import app）
_ART_VAULT_DEFAULT = r"E:\Workbuddy\Claw\08-文章笔记"
ART_VAULT = (os.environ.get("ARTICLE_VAULT_DIR")
             or (_ART_VAULT_DEFAULT if os.path.isdir(os.path.dirname(_ART_VAULT_DEFAULT))
                 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "articles_vault")))

_LRC_LINE = re.compile(r"^\[(\d+):(\d{1,2})(?:\.(\d{1,3}))?\](.*)$")


def parse_lrc(path: str):
    """LRC → [(t, en, zh), ...]。同一时间戳连续两行 = 英文+中文（voa_ingest 写出格式）。"""
    entries = []  # (t, text)
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
                entries.append((round(t, 2), text))
    sents, cur_t, en, zh = [], None, "", ""
    for t, text in entries:
        if cur_t is not None and abs(t - cur_t) > 0.05:
            sents.append((cur_t, en, zh))
            en, zh = "", ""
        cur_t = t
        if not en:
            en = text
        else:
            zh = text
    if cur_t is not None and en:
        sents.append((cur_t, en, zh))
    return sents


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def render(voaid: str, title: str = ""):
    """返回 (http_code, html)。"""
    voaid = voaid.strip()
    if not voaid.isdigit():
        return 400, "<h1>bad voa id</h1>"
    lrc_path = os.path.join(ART_VAULT, "media", "voa", f"voa_{voaid}.lrc")
    mp3_rel = f"/media/voa/voa_{voaid}.mp3"
    if not os.path.isfile(lrc_path):
        return 404, ("<h1>未找到该条目的 LRC 字幕文件</h1>"
                     f"<p>期望位置：media/voa/voa_{_esc(voaid)}.lrc。"
                     "请先通过采集流水线采集该条目。</p>")
    try:
        sents = parse_lrc(lrc_path)
    except Exception as e:
        return 500, f"<h1>LRC 解析失败</h1><pre>{_esc(str(e))}</pre>"
    if not sents:
        return 404, "<h1>LRC 内无可解析句子</h1>"
    has_mp3 = os.path.isfile(os.path.join(ART_VAULT, "media", "voa", f"voa_{voaid}.mp3"))
    # 安全内嵌 JSON（防 </script> 提前闭合）
    data = json.dumps([[t, en, zh] for t, en, zh in sents], ensure_ascii=False
                      ).replace("</", "<\\/")
    title = title or f"VOA {voaid}"

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · VOA 学习播放器</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2333;--mut:#7a8299;--acc:#1a66d6;--line:#e3e7ee;--zh:#8a5b00;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:19px;margin:0 0 6px}
.sub{color:var(--mut);font-size:13px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}
audio{width:100%;margin-bottom:10px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
button,.btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:7px 14px;font-size:13.5px;cursor:pointer;color:var(--fg)}
button:hover{border-color:var(--acc);color:var(--acc)}
button.on{background:var(--acc);border-color:var(--acc);color:#fff}
select{border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px;background:#fff;color:var(--fg)}
.sents{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.row{display:flex;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line);cursor:pointer;align-items:baseline}
.row:last-child{border-bottom:0}
.row:hover{background:#f0f4ff}
.row.cur{background:#e8f0fe;box-shadow:inset 3px 0 0 var(--acc)}
.no{color:var(--mut);font-size:12px;min-width:26px;text-align:right;flex:none}
.tm{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums;min-width:44px;flex:none}
.en{flex:1}
.zh{flex:1;color:var(--zh);font-size:14px}
/* 翻译三态：show=显示 / blur=遮掩(悬停偷看) / hide=隐藏 */
.sents[data-zh="blur"] .zh{filter:blur(7px);transition:filter .15s}
.sents[data-zh="blur"] .zh:hover{filter:none}
.sents[data-zh="hide"] .zh{display:none}
.hint{color:var(--mut);font-size:12.5px;margin:10px 4px}
.kbd{border:1px solid var(--line);border-radius:4px;padding:0 5px;background:#fff;font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">
  <h1>🎧 __TITLE__</h1>
  <div class="sub">VOA 逐句学习播放器 · 共 __N__ 句 · 数据为爱语吧 VOA 内容（仅个人学习）</div>
  <div class="card">
__AUDIO__
    <div class="bar">
      <span style="font-size:13px;color:var(--mut)">倍速</span>
      <select id="rate">
        <option value="0.6">0.6×</option><option value="0.75">0.75×</option>
        <option value="0.9">0.9×</option><option value="1" selected>1.0×</option>
        <option value="1.25">1.25×</option><option value="1.5">1.5×</option>
      </select>
      <button id="bLoop" title="当前句播完自动重播（精听）">🔁 单句循环</button>
      <span style="flex:1"></span>
      <span style="font-size:13px;color:var(--mut)">中文翻译</span>
      <button id="bShow" class="on">显示</button>
      <button id="bBlur">遮掩</button>
      <button id="bHide">隐藏</button>
    </div>
    <div class="hint">点击句子跳播 · <span class="kbd">Space</span> 播放/暂停 · <span class="kbd">←</span><span class="kbd">→</span> 上一句/下一句 · 遮掩模式下悬停中文可偷看</div>
  </div>
  <div class="sents" id="list" data-zh="show"></div>
</div>
<script>
const SENTS = __DATA__;
const MP3 = "__MP3__";
const NO_MP3 = __NOMP3__;
const audio = new Audio(MP3);
audio.preload = "metadata";
let cur = -1, loop = false;
const list = document.getElementById("list");
const frag = document.createDocumentFragment();
SENTS.forEach((s, i) => {
  const row = document.createElement("div");
  row.className = "row"; row.dataset.i = i;
  row.innerHTML = '<span class="no">' + (i + 1) + '</span>' +
    '<span class="tm">' + fmt(s[0]) + '</span>' +
    '<span class="en">' + s[1] + '</span>' +
    '<span class="zh">' + (s[2] || "") + '</span>';
  row.onclick = () => seekTo(i);
  frag.appendChild(row);
});
list.appendChild(frag);
function fmt(t){ t = Math.round(t); return String(Math.floor(t/60)).padStart(2,"0") + ":" + String(t%60).padStart(2,"0"); }
function dur(i){ const nx = SENTS[i+1] ? SENTS[i+1][0] : (audio.duration || SENTS[i][0] + 10); return Math.max(nx - SENTS[i][0], 1.2); }
function seekTo(i){
  if (NO_MP3) return;
  cur = i;
  audio.currentTime = SENTS[i][0] + 0.01;
  if (audio.paused) audio.play().catch(()=>{});
  paint();
}
function paint(){
  const rows = list.children;
  for (let i = 0; i < rows.length; i++) rows[i].classList.toggle("cur", i === cur);
  if (cur >= 0) rows[cur].scrollIntoView({block: "center", behavior: "smooth"});
}
function tick(){
  if (NO_MP3 || cur < 0) return;
  const end = SENTS[cur][0] + dur(cur);
  if (audio.currentTime >= end - 0.03) {
    if (loop) { audio.currentTime = SENTS[cur][0] + 0.01; return; }
    if (cur + 1 < SENTS.length) { cur++; paint(); }
  } else {
    // 拖动进度后自动对齐当前句
    let i = cur;
    while (i + 1 < SENTS.length && audio.currentTime >= SENTS[i+1][0]) i++;
    while (i > 0 && audio.currentTime < SENTS[i][0]) i--;
    if (i !== cur) { cur = i; paint(); }
  }
}
setInterval(tick, 200);
audio.addEventListener("ended", () => { if (!loop && cur + 1 < SENTS.length) { cur = 0; } });
document.getElementById("rate").onchange = e => audio.playbackRate = +e.target.value;
const bLoop = document.getElementById("bLoop");
bLoop.onclick = () => { loop = !loop; bLoop.classList.toggle("on", loop); };
const listEl = list;
const bShow = document.getElementById("bShow"), bBlur = document.getElementById("bBlur"), bHide = document.getElementById("bHide");
function setZh(mode){
  listEl.dataset.zh = mode;
  bShow.classList.toggle("on", mode === "show");
  bBlur.classList.toggle("on", mode === "blur");
  bHide.classList.toggle("on", mode === "hide");
}
bShow.onclick = () => setZh("show");
bBlur.onclick = () => setZh("blur");
bHide.onclick = () => setZh("hide");
document.addEventListener("keydown", e => {
  if (e.target.tagName === "SELECT") return;
  if (e.code === "Space") { e.preventDefault(); audio.paused ? audio.play().catch(()=>{}) : audio.pause(); }
  else if (e.key === "ArrowLeft" && cur > 0) seekTo(cur - 1);
  else if (e.key === "ArrowRight" && cur + 1 < SENTS.length) seekTo(cur + 1);
});
if (NO_MP3) {
  const w = document.createElement("div");
  w.className = "hint"; w.style.color = "#c00";
  w.textContent = "⚠ 本地 MP3 缺失，仅浏览字幕。请重新采集该条目以下载音频。";
  document.querySelector(".card").prepend(w);
}
</script>
</body>
</html>"""
    html = (html.replace("__TITLE__", _esc(title))
                .replace("__N__", str(len(sents)))
                .replace("__DATA__", data)
                .replace("__MP3__", mp3_rel)
                .replace("__NOMP3__", "true" if not has_mp3 else "false"))
    if has_mp3:
        html = html.replace("__AUDIO__",
                            '    <audio id="au" controls src="%s"></audio>' % mp3_rel)
    else:
        html = html.replace("__AUDIO__", "")
    return 200, html
