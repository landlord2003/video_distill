#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频号/抖音 视频整理流水线（本地优先 / 零 API 费）
流程：本地视频(mp4) -> 抽音频 -> faster-whisper 转写全文(可用 GPU)
     -> ffmpeg 抽关键帧 -> qwen3-vl:8b 视觉理解
     -> qwen3:14b 结构化总结(标题/摘要/要点/章节/标签)
     -> 拼装 Markdown -> 写入 Obsidian 库(Claw/07-视频号整理)

所有模型调用走本机 Ollama(127.0.0.1:11434)，不外传。
任何一环不可用都会优雅降级（标注缺失），不中断整条链路。
"""
import os
import io
import re
import json
import base64
import subprocess
import threading
import urllib.request
from pathlib import Path
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "videos_inbox")          # 下载的视频
FRAMES = os.path.join(BASE, "videos_frames")        # 抽出的关键帧

# Obsidian 落库目录：可用环境变量 VIDEO_VAULT_DIR 覆盖（便于其它机器指定自己的库）。
# 默认：本机 Claw 库；若 Claw 不存在则回退到项目内 distilled/ 目录，保证开箱即用。
_DEFAULT_VAULT = r"E:\Workbuddy\Claw\07-视频号整理"
_env_vault = os.environ.get("VIDEO_VAULT_DIR")
if _env_vault:
    VAULT = _env_vault
elif os.path.isdir(os.path.dirname(_DEFAULT_VAULT)):
    VAULT = _DEFAULT_VAULT
else:
    VAULT = os.path.join(BASE, "distilled")

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
# 总结模型可通过环境变量 SUMMARY_MODEL 覆盖（默认 qwen3:14b）。
# 注：无 GPU 的机器（如本机，纯 CPU 推理）上 qwen3:14b(9.8G) 慢到超时，
# 需用 qwen3-vl:8b(6.1G) 才稳；有 GPU 的机器（如 RTX 5070）可直接用 qwen3:14b。
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "qwen3:14b")
VISION_MODEL = "qwen3-vl:8b"
TRANSCRIBE_MODEL = os.environ.get("VIDEO_WHISPER", "small")   # faster-whisper 模型尺寸
N_KEYFRAMES = 10                                     # 抽帧数（教程类视频步骤还原靠帧密度）

for _d in (INBOX, FRAMES, VAULT):
    os.makedirs(_d, exist_ok=True)


# ---------------- ffmpeg ----------------
def get_ffmpeg():
    try:
        import imageio_ffmpeg
        # 归一化反斜杠 -> 正斜杠，避免 Windows 下 subprocess 调用异常
        return imageio_ffmpeg.get_ffmpeg_exe().replace("\\", "/")
    except Exception:
        return "ffmpeg"


# ---------------- Ollama ----------------
def _ollama_generate(model, prompt, images=None, timeout=180, keep_alive=None):
    """调用本机 Ollama /api/generate，返回文本。images: 本地图片路径列表。
    keep_alive: 模型驻留显存时长（如 "10m"/"5m"），用于避免大模型被反复冷加载。"""
    payload = {"model": model, "prompt": prompt, "stream": False}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if images:
        payload["images"] = [b64(p) for p in images if p and os.path.exists(p)]
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA + "/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        out = resp.get("response", "") or ""
        # 去掉 qwen3 的 <think>...</think> 思考块，避免污染 JSON 解析
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()
        return out
    except Exception as e:
        return f"[Ollama 调用失败: {e}]"


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


# ---------------- 转写 ----------------
WHISPER_LOCAL_ROOT = os.path.join(BASE, "whisper_models")


def _resolve_whisper_source(model_size):
    """优先本地模型目录（国内 ModelScope 预下载，绕开 HF CDN 被墙），否则回退模型名。"""
    local = os.path.join(WHISPER_LOCAL_ROOT, f"faster-whisper-{model_size}", "model.bin")
    if os.path.isfile(local) and os.path.getsize(local) > 1_000_000:
        return os.path.dirname(local)
    # 指定尺寸的本地模型缺失时，回退到任何可用的本地模型（small -> tiny）
    if model_size != "tiny":
        local_tiny = os.path.join(WHISPER_LOCAL_ROOT, "faster-whisper-tiny", "model.bin")
        if os.path.isfile(local_tiny) and os.path.getsize(local_tiny) > 1_000_000:
            return os.path.dirname(local_tiny)
    return model_size


# 转写设备：固定 CPU int8。
# 历史教训：device="auto" 先试 GPU，12GB 显存被 Ollama(keep_alive) 占住时
# ctranslate2 的 CUDA 初始化会零 CPU 永久挂死（且不抛异常，except 兜不住）。
# 短视频 CPU small int8 速度足够，确定性优先。可用环境变量 WHISPER_DEVICE 覆盖。
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
# 单条转写总超时（含模型加载），超时报错放行，绝不让批量无限挂死。
# 1800s：兼容 YouTube 长视频（1小时+ 讲座/播客类）在 CPU small int8 下的转写耗时
WHISPER_TIMEOUT = int(os.environ.get("WHISPER_TIMEOUT", "1800"))

_WHISPER_CACHE = {"model": None, "size": None}


def _load_whisper_model(src, timeout=300):
    """在子线程加载 Whisper 模型（带看门狗）。

    ctranslate2 在 GPU/资源初始化挂死时不抛异常，主线程会永远等——
    所以放到 daemon 线程加载，超时即中止等待并报错。
    """
    from faster_whisper import WhisperModel
    result = {}

    def _load():
        try:
            result["m"] = WhisperModel(src, device=WHISPER_DEVICE, compute_type="int8")
        except Exception as e:
            result["err"] = e

    th = threading.Thread(target=_load, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise RuntimeError(f"Whisper 模型加载超过 {timeout}s 未完成，疑似初始化挂死，已中止")
    if "err" in result:
        raise result["err"]
    return result["m"]


def _get_whisper(model_size):
    """全局单例：模型只加载一次，后续转写零加载开销。"""
    if _WHISPER_CACHE["model"] is None or _WHISPER_CACHE["size"] != model_size:
        src = _resolve_whisper_source(model_size)
        _WHISPER_CACHE["model"] = _load_whisper_model(src)
        _WHISPER_CACHE["size"] = model_size
        print(f"[video] whisper 模型就绪: {src} (device={WHISPER_DEVICE})", flush=True)
    return _WHISPER_CACHE["model"]


def transcribe(video_path, model_size=TRANSCRIBE_MODEL):
    """faster-whisper 转写。不可用则返回 ('', 原因)。整段在看门狗线程里跑。"""
    try:
        from faster_whisper import WhisperModel  # noqa: F401 提前暴露缺依赖
    except Exception as e:
        return "", f"faster-whisper 未安装：{e}"
    result = {}

    def _work():
        try:
            model = _get_whisper(model_size)
            # 语言自动检测：任何语种音频都全量转写，不再强制中文
            # （强制 zh 会让英文等外语视频被硬解码成中文碎片，整段失真）
            segs, _ = model.transcribe(video_path, beam_size=5)
            result["text"] = "\n".join(s.text for s in segs).strip()
        except Exception as e:
            import traceback
            traceback.print_exc()  # 完整堆栈进服务日志，便于事后排查
            result["err"] = f"转写出错：{type(e).__name__}: {e}"

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(WHISPER_TIMEOUT)
    if th.is_alive():
        return "", f"转写超时（>{WHISPER_TIMEOUT}s 未完成，模型或解码疑似挂死），已中止本条"
    return result.get("text", ""), result.get("err")


def transcribe_segments(video_path, model_size=TRANSCRIBE_MODEL):
    """faster-whisper 带时间轴分段转写（语言自动检测）。

    返回 (segments, language, err)；segments = [(start_sec, end_sec, text), ...]。
    用于双语字幕模式：需要每段时间戳以便英/中逐段对照。
    """
    try:
        from faster_whisper import WhisperModel  # noqa: F401 提前暴露缺依赖
    except Exception as e:
        return [], "", f"faster-whisper 未安装：{e}"
    result = {}

    def _work():
        try:
            model = _get_whisper(model_size)
            segs, info = model.transcribe(video_path, beam_size=5)  # 语言自动检测
            out = []
            for s in segs:
                txt = (s.text or "").strip()
                if txt:
                    out.append((float(s.start), float(s.end), txt))
            result["segs"] = out
            result["lang"] = (getattr(info, "language", "") or "").lower()
        except Exception as e:
            result["err"] = f"转写出错：{type(e).__name__}: {e}"

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(WHISPER_TIMEOUT)
    if th.is_alive():
        return [], "", f"转写超时（>{WHISPER_TIMEOUT}s 未完成），已中止本条"
    return result.get("segs", []), result.get("lang", ""), result.get("err")


def _fmt_ts(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def translate_segments_zh(segments, batch_size=15):
    """把英文字幕段批量翻译成中文（本地 qwen3:14b），返回与 segments 等长的中文列表。

    分批（默认 15 段/批）防超长；按编号对齐，解析失败的段落留空并由调用方标注。
    """
    texts = [(t or "") for (_, _, t) in segments]
    outs = [""] * len(segments)
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {t}" for j, t in enumerate(chunk))
        prompt = (
            "你是专业字幕翻译。下面是视频字幕（英文原文）按序号排列，"
            "请逐条翻译成自然流畅的简体中文，严格保持序号一一对应，不要合并或拆分条目。\n"
            "输出格式：每行一条，形如「1. 翻译内容」，除序号行外不要输出任何解释。\n\n"
            f"{numbered}"
        )
        raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=900)
        if raw.startswith("[Ollama 调用失败"):
            continue  # 本批失败，留空
        for ln in raw.splitlines():
            ln = ln.strip()
            m = re.match(r"^(\d+)\s*[.、:：)）]\s*(.+)$", ln)
            if not m:
                continue
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(chunk) and not outs[i + idx]:
                outs[i + idx] = m.group(2).strip()
    return outs


# ---------------- 关键帧 ----------------
def extract_keyframes(video_path, out_dir, n=4, first_only=False):
    """均匀抽 n 帧，返回图片路径列表（失败返回 []）。不依赖 ffprobe/时长解析。

    first_only=True 时只取开头 0.5 秒处一帧（视频首页/封面），用于双语字幕等
    只需要一张画面配图的场景。
    """
    ff = get_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    if first_only:
        single = str(out_dir / f"{stem}_001.jpg")
        try:
            subprocess.run([ff, "-y", "-ss", "0.5", "-i", video_path,
                            "-frames:v", "1", "-q:v", "3", single],
                           capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            err = (e.stderr or b"")[-200:]
            print(f"[video] ffmpeg 首帧抽取超时: {video_path} stderr={err!r}", flush=True)
        files = sorted(out_dir.glob(f"{stem}_*.jpg"))
        return [str(f) for f in files if os.path.getsize(f) > 200]
    pat = str(out_dir / f"{stem}_%03d.jpg")
    # 每 ~2 秒抽 1 帧（fps=1/2），不依赖视频时长。
    # -skip_frame nokey：只解码关键帧，长视频(10min+)快 ~10 倍，否则 180s 超时被杀；
    # keyframe 画质略降但足够视觉理解用。
    try:
        subprocess.run([ff, "-y", "-i", video_path, "-skip_frame", "nokey",
                        "-an", "-sn", "-dn",
                        "-vf", "fps=1/2", "-q:v", "3", pat],
                       capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or b"")[-200:]
        print(f"[video] ffmpeg 抽帧超时: {video_path} stderr={err!r}", flush=True)
    files = sorted(out_dir.glob(f"{stem}_*.jpg"))
    if not files:
        single = str(out_dir / f"{stem}_001.jpg")
        try:
            subprocess.run([ff, "-y", "-ss", "0.5", "-i", video_path,
                            "-frames:v", "1", "-q:v", "3", single], capture_output=True, timeout=180, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            err = (e.stderr or b"")[-200:]
            print(f"[video] ffmpeg 单帧兜底超时: {video_path} stderr={err!r}", flush=True)
        files = sorted(out_dir.glob(f"{stem}_*.jpg"))
    if not files:
        print(f"[video] 抽帧失败(无产出，将跳过视觉理解): {video_path}", flush=True)
        return []
    # 从抽出的帧里均匀挑 n 张
    if len(files) > n:
        if n > 1:
            idx = [int(round(i * (len(files) - 1) / (n - 1))) for i in range(n)]
        else:
            idx = [0]
        chosen = [files[i] for i in idx]
    else:
        chosen = files
    return [str(f) for f in chosen if os.path.getsize(f) > 200]


# ---------------- 视觉理解 ----------------
def describe_frames(frame_paths, max_frames=N_KEYFRAMES):
    """逐张视觉理解（每张单独一次请求，规避 qwen3-vl 多图 HTTP 400 限制）。

    返回与 frame_paths 等长的描述列表（单张失败则该项为空字符串）。
    """
    if not frame_paths:
        return []
    sel = frame_paths[:max_frames]
    prompt = ("你是一名视频内容分析助手，正在为『还原视频中的制作/操作过程』收集素材。"
              "请用一句不超过 45 字的中文描述这张关键帧画面中可见的关键信息："
              "场景、人物与动作、可见的配料/原料/工具/器皿，以及画面上的任何文字或数字"
              "（如温度、克数、步骤名、品牌标识）。只输出一句描述，不要前缀或解释。")
    descs = []
    for fp in sel:
        if not fp or not os.path.exists(fp):
            descs.append("")
            continue
        out = _ollama_generate(VISION_MODEL, prompt, images=[fp], timeout=240)
        # 取第一行非空、且非错误串的内容作为该帧描述
        line = ""
        for ln in out.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("[Ollama"):
                line = ln
                break
        descs.append(line)
    return descs


# ---------------- 关键词提取（jieba TF-IDF，源自 smart-summarize 思路；jieba 缺失时返回空，由模型标签兜底） ----------------
_ZH_STOP_WORDS = {'可以', '这个', '这样', '因为', '所以', '但是', '而且', '我们', '你们', '他们',
                  '没有', '就是', '什么', '如何', '怎么', '一个', '一种', '通过', '进行', '实现',
                  '以及', '并且', '因此', '如果', '已经', '这些', '那些', '目前', '同时'}

def extract_tags(text, n=8):
    """基于全量转写的确定性关键词提取（不依赖 LLM、不吃上下文窗口）。"""
    if not text or not text.strip():
        return []
    try:
        import logging
        import jieba
        import jieba.analyse
        jieba.setLogLevel(logging.ERROR)
        tags = jieba.analyse.extract_tags(
            text, topK=n,
            allowPOS=('n', 'ns', 'nr', 'nt', 'nz', 'vn', 'v', 'a', 'an', 'i', 'j', 'l', 'eng'))
        return [t.strip() for t in tags if t.strip() not in _ZH_STOP_WORDS and len(t.strip()) >= 2]
    except Exception:
        return []

# ---------------- 结构化总结 ----------------
# 单段蒸馏的转写字符上限（留余量防超 qwen3:14b 上下文）
DISTILL_CHUNK_CHARS = 11000


def _split_transcript(transcript, size=DISTILL_CHUNK_CHARS):
    """把长转写按段落边界切成 n 段（每段≈size 字符），保证句子完整。

    返回列表；len==1 表示无需分段（单段直蒸）。
    """
    if len(transcript) <= size:
        return [transcript]
    paras = transcript.split("\n")
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 1 > size:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _summarize_single(transcript_text, descs_text, source_meta):
    """单段直蒸（转写 ≤ DISTILL_CHUNK_CHARS 时走这里）。返回 JSON dict。"""
    prompt = (
        "你是知识库整理助手。基于一段视频的【语音转写】与【关键帧画面描述】，"
        "产出结构化笔记。请只输出一个 JSON 对象，字段如下（不要输出任何多余文字、不要 Markdown 代码块标记）：\n"
        "{\n"
        '  "标题": "一句话标题（15字内）",\n'
        '  "摘要": "120字内的一句话摘要",\n'
        '  "制作流程": [{"步骤":"1","操作":"本步做什么","用料":"本步用到的原料/工具","参数":"温度/时间/比例等(无则空串)","要点":""}, ...],\n'
        '  "要点": ["要点1","要点2",...],\n'
        '  "章节": [{"时间":"","主题":""}, ...],\n'
        '  "标签": ["标签1","标签2",...],\n'
        '  "关键结论": "可执行的结论或金句（如有）"\n'
        "}\n\n"
        "【要点】填写规则（极重要）：\n"
        "1. 给出 6~10 条要点；每条必须是一个信息完整的句子，从【语音转写】中蒸馏出**具体事实**：\n"
        "   人物、机构、金额、时间、数量、比例、因果关系等细节，缺一不可。\n"
        "2. **严禁空泛概括**。不要写「讲了资本运作」「介绍了一个案例」这种话；\n"
        "   要写成「随天立注册20家壳公司控制上下游，虚构交易闭环」这种带细节、可独立成立的表述。\n"
        "3. 按视频叙事顺序排列；转写中的口语错误请自行纠正为规范书面语（如同音字、人名机构名）；\n"
        "   转写来自语音识别，可能存在同音字/错别字（人名、书名、专有名词尤甚），请依据上下文纠正。\n\n"
        "【制作流程】填写规则（极重要）：\n"
        "1. 若视频是制作方法/教程/工艺/实验类，请务必填写『制作流程』——按视频出现顺序，"
        "逐步还原每一步的具体操作、所用原料与工具、关键参数（温度/时间/配比/火候）、以及成败要点。\n"
        "2. 每一步必须基于【关键帧画面描述】与【语音转写】中的真实信息，**严禁编造**；"
        "信息不足的步骤请如实标注（画面/语音未明确展示），不要脑补步骤名或参数。\n"
        "3. 若为普通内容（非制作方法类），给空数组 []。\n\n"
        f"【关键帧画面描述】\n{descs_text}\n\n"
        f"【语音转写】\n{transcript_text}\n"
    )
    raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=1800, keep_alive="10m")
    return _parse_summary_json(raw, source_meta)


def _parse_summary_json(raw, source_meta):
    """容错解析 Ollama 输出的 JSON；先抽取 {...} 块再解析，失败退化为原始文本摘要。"""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if m:
        raw = m.group(0)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"标题": source_meta.get("title") or "未命名视频",
                "摘要": raw[:200], "要点": [], "章节": [],
                "标签": [], "关键结论": ""}


def _parse_map_output(raw):
    """解析 map 阶段纯文本输出 -> (主题, [要点...], 关键结论)。容错：接受 - · • * 或数字编号。"""
    theme, concl, pts = "", "", []
    for ln in (raw or "").splitlines():
        ln = ln.strip().strip('`')
        if not ln or ln.startswith("```"):
            continue
        m = re.match(r"^(?:本段)?主题\s*[:：]\s*(.+)$", ln)
        if m:
            if not theme:
                theme = m.group(1).strip()
            continue
        m = re.match(r"^(?:关键结论|结论)\s*[:：]\s*(.+)$", ln)
        if m:
            concl = m.group(1).strip()
            continue
        m = re.match(r"^(?:[-·•*]|\d{1,2}[.、:：)）])\s*(.+)$", ln)
        if m:
            t = m.group(1).strip()
            if len(t) >= 6 and not t.startswith(("要点", "主题")):
                pts.append(t)
    return theme, pts, concl


def _parse_reduce_output(raw):
    """解析 reduce 阶段纯文本输出 -> dict（标题/摘要/要点/关键结论/标签）。"""
    data = {"标题": "", "摘要": "", "要点": [], "章节": [], "标签": [], "关键结论": ""}
    mode = None
    for ln in (raw or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("```"):
            continue
        m = re.match(r"^标题\s*[:：]\s*(.+)$", ln)
        if m:
            data["标题"] = m.group(1).strip(); mode = None; continue
        m = re.match(r"^摘要\s*[:：]\s*(.+)$", ln)
        if m:
            data["摘要"] = m.group(1).strip(); mode = None; continue
        m = re.match(r"^(?:关键结论|结论)\s*[:：]\s*(.+)$", ln)
        if m:
            data["关键结论"] = m.group(1).strip(); mode = None; continue
        m = re.match(r"^(要点|标签)\s*[:：]?\s*$", ln)
        if m:
            mode = m.group(1); continue
        m = re.match(r"^(?:[-·•*]|\d{1,2}[.、:：)）])\s*(.+)$", ln)
        if m and mode in ("要点", "标签"):
            data[mode].append(m.group(1).strip())
            continue
        if mode == "要点" and len(ln) >= 6:
            data["要点"].append(ln)
    return data


def _deterministic_summary(map_items, source_meta):
    """reduce 失败时的无模型兜底：直接用分段要点拼最终结构，绝不把原文塞进摘要。"""
    pts = []
    for (_, p, _) in map_items:
        pts.extend(p)
    concl = next((c for (_, _, c) in map_items if c), "")
    chapters = [{"时间": f"第{i+1}段", "主题": th or "未命名"}
                for i, (th, _, _) in enumerate(map_items) if th]
    return {
        "标题": source_meta.get("title") or "未命名视频",
        "摘要": ("；".join(pts[:3])[:150] if pts else "（蒸馏失败，请查看全文转写）"),
        "制作流程": [],
        "章节": chapters,
        "要点": pts[:15],
        "标签": [],
        "关键结论": concl,
    }


def summarize(transcript, frame_descs, source_meta):
    """结构化总结。长转写**自动分段蒸馏**（map-reduce），对使用者无感。

    - 转写 ≤ DISTILL_CHUNK_CHARS：单段直蒸（与原行为一致）
    - 更长：先逐段提取要点（map），再合并去重产出最终结构（reduce）
    返回 JSON dict，附加 "_distill_chunks"=实际分段数（1=未分段）。
    """
    descs_text = "\n".join(f"- {d}" for d in frame_descs) if frame_descs else "（无关键帧描述）"
    chunks = _split_transcript(transcript or "")
    n = len(chunks)

    if n == 1:
        data = _summarize_single(transcript[:12000], descs_text, source_meta)
        data["_distill_chunks"] = 1
        return data

    # ---- map：逐段蒸馏要点（纯文本协议，容错解析；不强迫模型吐 JSON） ----
    map_items = []   # [(主题, [要点...], 关键结论), ...]
    for i, ch in enumerate(chunks):
        prompt = (
            f"你是知识库整理助手。下面是一段长视频语音转写的第 {i+1}/{n} 段（共 {n} 段）。\n"
            "转写来自语音识别，可能存在同音字/错别字（人名、书名、专有名词尤甚），"
            "请依据上下文自行纠正后再提炼；无法确定的按原样保留。\n"
            "请输出纯文本（不要 JSON、不要代码块、不要任何解释），格式严格如下：\n"
            "主题：<本段主题，10字内>\n"
            "- <要点1，必须是信息完整的句子，含人物/机构/金额/时间/数量/因果等具体细节>\n"
            "- <要点2>\n"
            "（最多 12 条要点，按本段叙事顺序，严禁空泛概括）\n"
            "关键结论：<本段最重要的结论或金句，无则写「无」>\n\n"
            f"【语音转写 第{i+1}/{n}段】\n{ch}\n"
        )
        raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=1200, keep_alive="10m")
        theme, pts, concl = _parse_map_output(raw)
        if pts:
            map_items.append((theme, pts, concl))

    # 全部分段都没提出要点：退化单段直蒸（截断兜底）
    if not map_items:
        data = _summarize_single(transcript[:12000], descs_text, source_meta)
        data["_distill_chunks"] = n
        return data

    # ---- reduce：合并为最终结构（纯文本协议；失败则用分段要点直接拼装，绝不用原文） ----
    seg_text = "\n\n".join(
        (f"【第{i+1}段「{th}」】\n" if th else f"【第{i+1}段】\n")
        + "\n".join(f"- {p}" for p in pts)
        + (f"\n关键结论：{c}" if c and c != "无" else "")
        for i, (th, pts, c) in enumerate(map_items))
    prompt = (
        "你是知识库整理助手。下面是对一段长视频**分段蒸馏**后汇总的全部分段要点。"
        "请合并去重、按视频叙事顺序整理，输出纯文本（不要 JSON、不要代码块），格式严格如下：\n"
        "标题：<全片标题，15字内>\n"
        "摘要：<120字内的整体摘要>\n"
        "要点：\n"
        "- <要点1，保留具体细节>\n"
        "- <要点2>\n"
        "（8~15 条；合并重复，保留全部具体事实细节，严禁空泛概括）\n"
        "关键结论：<全片最核心的可执行结论或金句>\n\n"
        f"【分段要点汇总（共{n}段）】\n{seg_text}\n"
    )
    raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=1800, keep_alive="10m")
    data = _parse_reduce_output(raw)
    if data["要点"]:
        data["标题"] = data["标题"] or (source_meta.get("title") or "未命名视频")
        data["摘要"] = data["摘要"] or "；".join(data["要点"][:3])[:150]
        data["章节"] = [{"时间": f"第{i+1}段", "主题": th or "未命名"}
                        for i, (th, _, _) in enumerate(map_items) if th]
        data["标签"] = data.get("标签") or []
    else:
        # reduce 失败：用分段要点确定性拼装，不再依赖模型
        data = _deterministic_summary(map_items, source_meta)
    data["_distill_chunks"] = n
    return data


# ---------------- 拼装 Markdown ----------------
def _embed_image(fp):
    """压缩后内嵌（ffmpeg 缩到 640px 宽、q7），控制笔记体积；失败退化原图。

    生成 <原名>_embed.jpg 缓存在帧目录，原图保留在库内 frames/（高清备份）。
    """
    if not (fp and os.path.exists(fp)):
        return ""
    try:
        small = os.path.splitext(fp)[0] + "_embed.jpg"
        if not os.path.exists(small) or os.path.getsize(small) == 0:
            ff = get_ffmpeg()
            subprocess.run([ff, "-y", "-i", fp, "-vf", "scale=640:-1", "-q:v", "7", small],
                           capture_output=True, timeout=30)
        if os.path.exists(small) and os.path.getsize(small) > 0:
            return f"data:image/jpeg;base64,{b64(small)}"
        return f"data:image/jpeg;base64,{b64(fp)}"
    except Exception:
        try:
            return f"data:image/jpeg;base64,{b64(fp)}"
        except Exception:
            return ""


def build_markdown(meta, transcript, frame_descs, summary, frames, bilingual_blocks=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    title = summary.get("标题") or meta.get("title") or "未命名视频"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 来源：{meta.get('source','')}  ")
    lines.append(f"> 原始链接：{meta.get('url','')}  ")
    lines.append(f"> 整理时间：{now}  ")
    if (summary.get("_distill_chunks") or 1) > 1:
        lines.append(f"> 🧠 分段蒸馏：全文较长，自动分 **{summary['_distill_chunks']}** 段蒸馏后合并（要点已覆盖全片）")
    tags = summary.get("标签") or []
    if tags:
        lines.append("")
        lines.append("**标签：** " + " · ".join(f"`{t}`" for t in tags))
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append(summary.get("摘要") or "（无）")
    if summary.get("关键结论"):
        lines.append("")
        lines.append(f"> **关键结论：** {summary['关键结论']}")
    # 制作流程（核心：还原操作步骤）
    flow = summary.get("制作流程") or []
    if flow:
        lines.append("")
        lines.append("## 制作流程")
        lines.append("")
        for step in flow:
            if isinstance(step, dict):
                idx = str(step.get("步骤", "")).strip()
                op = step.get("操作", "") or ""
                ing = step.get("用料", "") or ""
                param = step.get("参数", "") or ""
                note = step.get("要点", "") or ""
                head = f"**{idx}. {op}**" if op else f"**{idx}.**"
                lines.append(head)
                if ing:
                    lines.append(f"  - 用料/工具：{ing}")
                if param:
                    lines.append(f"  - 关键参数：{param}")
                if note:
                    lines.append(f"  - 要点：{note}")
            else:
                lines.append(f"- {step}")
    pts = summary.get("要点") or []
    if pts:
        lines.append("")
        lines.append("## 要点")
        lines.append("")
        for p in pts:
            lines.append(f"- {p}")
    ch = summary.get("章节") or []
    if ch:
        lines.append("")
        lines.append("## 章节")
        lines.append("")
        for c in ch:
            t = c.get("时间", "") if isinstance(c, dict) else ""
            s = c.get("主题", "") if isinstance(c, dict) else str(c)
            lines.append(f"- **{t}** {s}")
    if frames:
        lines.append("")
        lines.append("## 关键帧")
        lines.append("")
        for i, fp in enumerate(frames):
            uri = _embed_image(fp)
            lines.append(f"![关键帧{i+1}]({uri})")
            if i < len(frame_descs):
                lines.append(f"> {frame_descs[i]}")
            lines.append("")
    if bilingual_blocks:
        # 双语字幕对照表（替代纯文本转写全文，避免重复）
        lines.append("")
        lines.append("## 双语字幕（原文 / 中文对照）")
        lines.append("")
        lines.append("| 时间 | 原文 | 中文翻译 |")
        lines.append("|---|---|---|")
        for ts, src, zh in bilingual_blocks:
            lines.append(f"| {ts} | {src.replace('|', '/')} | {(zh or '（翻译缺失）').replace('|', '/')} |")
    elif transcript:
        lines.append("## 语音转写全文")
        lines.append("")
        lines.append(transcript)
    lines.append("")
    return "\n".join(lines)


def process_video(video_path, url="", source="", write_vault=True, model_size=TRANSCRIBE_MODEL,
                  bilingual=False, first_frame_only=False):
    """主流程：单视频 -> Markdown + 落库。返回结果 dict。

    bilingual: 双语字幕模式——转写语言自动检测，非中文音频产出「原文/中文对照」表
               （中文翻译走本地 qwen3:14b）；中文音频自动退回普通模式并标注。
    first_frame_only: 只抽开头 0.5s 一帧（视频首页/封面），不跑全片抽帧。
    """
    video_path = str(video_path)
    if not os.path.exists(video_path):
        return {"ok": False, "error": f"视频不存在：{video_path}"}
    stem = Path(video_path).stem or "video"
    slug = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w一-鿿-]", "", stem)[:30]
    frames_dir = os.path.join(FRAMES, slug)
    os.makedirs(frames_dir, exist_ok=True)

    meta = {"source": source or "视频号/抖音", "url": url, "title": stem}

    bilingual_blocks = None
    transcript, terr = "", None
    if bilingual:
        # 双语模式：带时间轴分段转写（语言自动检测）
        segs, lang, terr = transcribe_segments(video_path, model_size)
        if segs and lang == "zh":
            # 中文音频：无需英中对照，退回普通全文
            transcript = "\n".join(t for (_, _, t) in segs)
            meta["lang"] = lang
        elif segs:
            # 非中文（英文等）音频：逐段翻译成中文，产出对照表
            zhs = translate_segments_zh(segs)
            bilingual_blocks = [(_fmt_ts(st), tx, zh) for (st, _, tx), zh in zip(segs, zhs)]
            transcript = "\n".join(t for (_, _, t) in segs)
            meta["lang"] = lang
        else:
            # 分段转写失败，兜底普通全文转写
            transcript, terr = transcribe(video_path, model_size)
    else:
        # 1) 转写
        transcript, terr = transcribe(video_path, model_size)
    # 1.5) 纯音频文件（mp3/wav/m4a 等）：无画面可抽，跳过关键帧与视觉理解
    AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
    is_audio = Path(video_path).suffix.lower() in AUDIO_EXTS
    if is_audio:
        frames, frame_descs = [], []
        meta["source"] = source or "音频文件"
        meta["media"] = "audio"
    else:
        # 2) 关键帧（首页模式只取一帧）
        frames = extract_keyframes(video_path, frames_dir, n=N_KEYFRAMES,
                                   first_only=first_frame_only)
        # 3) 视觉理解
        frame_descs = describe_frames(frames)
    # 4) 结构化总结（双语模式喂中文翻译，总结质量更好）
    summarize_text = transcript
    if bilingual_blocks:
        zh_join = "\n".join(zh for (_, _, zh) in bilingual_blocks if zh)
        if len(zh_join) > 50:
            summarize_text = zh_join
    summary = summarize(summarize_text, frame_descs, meta)
    # 4.5) 标签补强：jieba 基于【全量】转写提取关键词，与模型标签合并去重——
    #      模型标签可能空/质量不稳，关键词提取不受上下文窗口限制，覆盖全片
    try:
        kw = extract_tags(transcript, n=8)
        merged = list(dict.fromkeys((summary.get("标签") or []) + kw))[:10]
        if merged:
            summary["标签"] = merged
    except Exception:
        pass
    # 5) 拼装
    md = build_markdown(meta, transcript, frame_descs, summary, frames,
                        bilingual_blocks=bilingual_blocks)

    result = {
        "ok": True,
        "slug": slug,
        "title": summary.get("标题") or stem,
        "video_path": video_path,
        "transcript_len": len(transcript),
        "transcript_error": terr,
        "frames": len(frames),
        "tags": summary.get("标签", []),
        "lang": meta.get("lang", ""),
        "bilingual": bool(bilingual_blocks),
        "distill_chunks": summary.get("_distill_chunks", 1),
        "markdown": md,
    }

    if write_vault:
        try:
            os.makedirs(VAULT, exist_ok=True)
            md_path = os.path.join(VAULT, slug + ".md")
            with io.open(md_path, "w", encoding="utf-8") as f:
                f.write(md)
            # 关键帧也拷进库内 frames/
            vault_frames = os.path.join(VAULT, "frames")
            os.makedirs(vault_frames, exist_ok=True)
            import shutil
            for fp in frames:
                shutil.copy(fp, os.path.join(vault_frames, os.path.basename(fp)))
            result["vault_path"] = md_path
        except Exception as e:
            result["vault_error"] = f"{type(e).__name__}: {e}"

    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: video_pipeline.py <video.mp4> [url]")
        sys.exit(1)
    r = process_video(sys.argv[1], url=sys.argv[2] if len(sys.argv) > 2 else "")
    print(json.dumps({k: v for k, v in r.items() if k != "markdown"},
                     ensure_ascii=False, indent=2))
    print("\n--- MARKDOWN (前 1500 字) ---\n")
    print(r.get("markdown", "")[:1500])
