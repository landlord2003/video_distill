#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频号/抖音 视频整理流水线（本地优先 / 零 API 费）
流程：本地视频(mp4) -> 抽音频 -> faster-whisper 转写全文(可用 GPU)
     -> qwen3:14b 结构化总结(标题/摘要/要点/章节/标签)，摘要硬约束 >50 字
     -> 拼装 Markdown -> 写入 Obsidian 库(Claw/07-视频号整理)

关键帧抓取与 qwen3-vl 视觉理解已取消（2026-10-14）：总结完全基于语音转写。
所有模型调用走本机 Ollama(127.0.0.1:11434)，不外传。
任何一环不可用都会优雅降级（标注缺失），不中断整条链路。
"""
import os
import io
import re
import json
import time
import glob
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


# ---------------- 流水线进度（进度栏实时显示当前阶段：下载/转写/蒸馏） ----------------
# 全局单槽即可：_video_lock 保证同一时刻只有一条流水线在跑，后写覆盖前写。
_progress = {"stage": "", "detail": "", "url": "", "ts": 0}

def set_progress(stage, detail="", url=""):
    """更新流水线当前阶段（任意线程可调）。"""
    _progress.update({"stage": stage, "detail": str(detail)[:160],
                      "url": str(url)[:120], "ts": time.time()})

def get_progress():
    """读取当前进度；60s 无更新视为过期（前端据此显示空闲）。"""
    p = dict(_progress)
    if p["ts"] and time.time() - p["ts"] > 60:
        p["stale"] = True
    return p


# ---------------- ffmpeg ----------------
def get_ffmpeg():
    try:
        import imageio_ffmpeg
        # 归一化反斜杠 -> 正斜杠，避免 Windows 下 subprocess 调用异常
        return imageio_ffmpeg.get_ffmpeg_exe().replace("\\", "/")
    except Exception:
        return "ffmpeg"


# ---------------- Ollama ----------------
def _ollama_generate(model, prompt, images=None, timeout=180, keep_alive=None, options=None, think=None):
    """调用本机 Ollama /api/generate，返回文本。images: 本地图片路径列表。
    keep_alive: 模型驻留显存时长（如 "10m"/"5m"），用于避免大模型被反复冷加载。
    options: 透传 Ollama 采样参数（如 {"num_predict": 800} 硬限输出长度，
             防 qwen3 无视格式指令写长篇大论）。
    think: qwen3 思考模式开关（Ollama 顶层 think 字段）。False=禁用思考——
           实测思考块会吃掉 num_predict 配额导致正文为空。"""
    payload = {"model": model, "prompt": prompt, "stream": False}
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if options:
        payload["options"] = options
    if think is not None:
        payload["think"] = think
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


# ---- 转写设备（2026-10-14 起：GPU 优先 + 自动降级）----
# 实测（60s 中文音频 / small / beam5）：
#   cuda int8_float16 = 2.0s（~30x 实时，显存 ~465MB）
#   cpu  int8         = 11.0s（~5.4x 实时）
# 即 GPU 快约 5 倍；28 分钟视频 CPU 要 ~5 分钟，GPU 只要 ~1 分钟。
#
# 🔴 历史教训（仍在生效，别删守卫）：显存被 Ollama(keep_alive) 占满时，
#    ctranslate2 的 CUDA 初始化会**静默挂死**（不抛异常，except 兜不住）。
#    所以：① 加载走看门狗线程；② 显存不足先卸载 Ollama 腾地方；③ 仍不足就退 CPU。
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "auto").strip().lower()  # auto|cuda|cpu
# 单条转写总超时（含模型加载），超时报错放行，绝不让批量无限挂死。
WHISPER_TIMEOUT = int(os.environ.get("WHISPER_TIMEOUT", "1800"))
# 模型加载看门狗：GPU 正常 0.7s，给 180s 已经很宽松；挂死时尽早脱身
WHISPER_LOAD_TIMEOUT = int(os.environ.get("WHISPER_LOAD_TIMEOUT", "180"))
# 走 GPU 所需的最小空闲显存(MB)；低于此值先腾显存，再不够就退 CPU。0 = 关闭检查
# 1200MB：whisper small int8_float16 实测占 ~465MB，再留 CT2 workspace 余量
WHISPER_MIN_FREE_MB = int(os.environ.get("WHISPER_MIN_FREE_MB", "1200"))
# 显存不足时是否先让 Ollama 释放驻留模型（摘要步骤会按需自动重载）
WHISPER_FREE_OLLAMA = os.environ.get("WHISPER_FREE_OLLAMA", "1").lower() not in ("0", "false", "no")

# 计算类型：GPU 用 int8_float16（int8 权重省显存 + fp16 计算），CPU 用 int8
_COMPUTE_BY_DEVICE = {"cuda": "int8_float16", "cpu": "int8"}

_WHISPER_CACHE = {"model": None, "size": None, "device": None, "compute": None}
_CUDA_DISABLED = {"why": ""}      # 一旦 GPU 这条路失败，本进程内不再重试
_CUDA_DLLS = {"done": False, "dirs": []}


def _cuda_dll_dirs():
    """找出可用的 CUDA 12 运行库目录（含 cublas64_12.dll）。

    🔴 为什么不用 `pip install nvidia-cublas-cu12`：Windows 版 wheel 在常用国内镜像
       上时有时无（清华源能查到文件、pip 却解析不到）。而**本机 Ollama 自带一整套**
       cuda_v12 运行库（cublas64_12 / cublasLt64_12 / cudart64_12），直接借用，
       零下载、零版本冲突。实测 ctranslate2 4.8.2 + faster-whisper 1.2.1 直接可用。
    ⚠️ 必须挑**确实含 cublas64_12.dll** 的目录：Ollama 同时带 cuda_v13（cublas64_13），
       喂错目录照样报 "cublas64_12.dll is not found"。
    ⚠️ **不需要 cuDNN**（Whisper 用不到卷积核走 cuDNN 的路径）——省了 ~800MB 下载。
    """
    cands = []
    env = os.environ.get("WHISPER_CUDA_DLL_DIR")
    if env:
        cands.append(env)
    for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)")):
        if not root:
            continue
        for sub in ("Programs/Ollama/lib/ollama", "Ollama/lib/ollama"):
            base = os.path.join(root, *sub.split("/"))
            if os.path.isdir(base):
                try:
                    cands += [os.path.join(base, v) for v in sorted(os.listdir(base))
                              if v.startswith("cuda_v")]
                except OSError:
                    pass
    # 万一以后 pip 装上了 nvidia-*-cu12，也认
    for sp in (os.path.join(BASE, "venv", "Lib", "site-packages"),
               os.path.join(os.path.dirname(os.__file__), "site-packages")):
        cands += glob.glob(os.path.join(sp, "nvidia", "*", "bin"))
    out = []
    for d in cands:
        try:
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "cublas64_12.dll")):
                out.append(d)
        except OSError:
            pass
    return out


def _prepare_cuda_dlls():
    """把 CUDA 运行库目录挂进 DLL 搜索路径（幂等）。

    🔴 踩过的坑：只调 `os.add_dll_directory()` **不够** —— CTranslate2 是通过
       PATH 去 LoadLibrary 的，必须同时改 `PATH`。实测只设前者，
       模型能加载（0.7s）但一到 encode 就报 cublas64_12.dll not found。
    """
    if _CUDA_DLLS["done"]:
        return _CUDA_DLLS["dirs"]
    dirs = _cuda_dll_dirs()
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
        print("[video] CUDA 运行库就绪: %s" % "; ".join(dirs), flush=True)
    _CUDA_DLLS.update({"done": True, "dirs": dirs})
    return dirs


def _vram_free_mb():
    """查空闲显存(MB)。取不到返回 None（不因此阻断流程）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15)
        if out.returncode == 0 and (out.stdout or "").strip():
            return int(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def _ollama_unload_all():
    """让 Ollama 立刻释放驻留显存（keep_alive=0）。返回被卸载的模型名列表。

    转写阶段用不到 LLM，腾出来的显存给 CUDA；后面的摘要步骤 Ollama 会自动重载
    （qwen3:14b 冷启约十几秒，远比 CPU 转写省下的几分钟划算）。
    """
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as r:
            models = (json.loads(r.read().decode("utf-8")) or {}).get("models") or []
    except Exception:
        return []
    freed = []
    for m in models:
        name = m.get("name") or m.get("model")
        if not name:
            continue
        try:
            data = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
            req = urllib.request.Request(OLLAMA + "/api/generate", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            freed.append(name)
        except Exception:
            pass
    return freed


def _resolve_device():
    """决定 (device, compute_type)，并说明理由。

    auto：优先进 GPU；缺 CUDA 库 / 显存不足 / 曾经失败 → 退 CPU。
    cuda：**强制**尝试 GPU（跳过显存守卫，仅缺库时警告）。
    cpu ：强制 CPU。
    """
    want = WHISPER_DEVICE
    if want == "cpu":
        return "cpu", _COMPUTE_BY_DEVICE["cpu"], "WHISPER_DEVICE=cpu 强制"
    if _CUDA_DISABLED["why"]:
        return "cpu", _COMPUTE_BY_DEVICE["cpu"], "本进程内 GPU 已降级（%s）" % _CUDA_DISABLED["why"]

    dirs = _prepare_cuda_dlls()
    if not dirs:
        why = "未发现 CUDA 运行库(cublas64_12.dll)"
        if want == "cuda":
            print("[video] %s，但仍按 WHISPER_DEVICE=cuda 硬试" % why, flush=True)
            return "cuda", _COMPUTE_BY_DEVICE["cuda"], why
        _CUDA_DISABLED["why"] = why
        return "cpu", _COMPUTE_BY_DEVICE["cpu"], why

    free = _vram_free_mb()
    if free is not None and WHISPER_MIN_FREE_MB > 0 and free < WHISPER_MIN_FREE_MB:
        freed = _ollama_unload_all() if WHISPER_FREE_OLLAMA else []
        if freed:
            time.sleep(1.5)                 # 等显存真正还给驱动
            free = _vram_free_mb()
            print("[video] 显存不足，已让 Ollama 卸载 %s，现空闲 %sMB"
                  % (", ".join(freed), free), flush=True)
        if free is not None and free < WHISPER_MIN_FREE_MB:
            why = "空闲显存 %sMB < %dMB" % (free, WHISPER_MIN_FREE_MB)
            if want == "cuda":
                print("[video] %s，但仍按 WHISPER_DEVICE=cuda 硬试" % why, flush=True)
            else:
                # ⚠️ 这里**故意不写 _CUDA_DISABLED**：显存不足是"软条件"，
                #    服务是长驻的，下次 Ollama 卸载后显存就回来了 → 下次要重新判定。
                #    只有加载报错/推理挂死这类"硬失败"才永久降级。
                print("[video] %s → 本次转写用 CPU（下次会重新判定）" % why, flush=True)
                return "cpu", _COMPUTE_BY_DEVICE["cpu"], why

    return "cuda", _COMPUTE_BY_DEVICE["cuda"], "auto"


def _load_whisper_model(src, device, compute_type, timeout=None):
    """在子线程加载 Whisper 模型（带看门狗）。

    ctranslate2 在 GPU/资源初始化挂死时不抛异常，主线程会永远等——
    所以放到 daemon 线程加载，超时即中止等待并报错。
    """
    from faster_whisper import WhisperModel
    timeout = timeout or WHISPER_LOAD_TIMEOUT
    result = {}

    def _load():
        try:
            result["m"] = WhisperModel(src, device=device, compute_type=compute_type)
        except Exception as e:
            result["err"] = e

    th = threading.Thread(target=_load, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise RuntimeError(f"Whisper 模型加载({device}/{compute_type})超过 {timeout}s 未完成，疑似初始化挂死，已中止")
    if "err" in result:
        raise result["err"]
    return result["m"]


def _get_whisper(model_size):
    """全局单例：同尺寸 + 同设备则复用，否则重建。

    🔴 设备判定**不做粘性缓存**：本服务是长驻进程，Ollama 驻留模型会让显存时多时少，
       某一次显存不够退了 CPU，不代表下次还得退 → 每次调用重新判定。
       只有"硬失败"（加载报错 / 推理挂死）才写进 _CUDA_DISABLED 永久降级，
       避免反复撞同一堵墙。代价是每条任务多一次 nvidia-smi（~0.2s），可忽略。
    """
    src = _resolve_whisper_source(model_size)

    # 已在 GPU 且尺寸对 → 直接复用：显存早占住了，没必要再跑一遍显存守卫
    # （否则会因为"剩余显存不足"把 Ollama 的摘要模型白白卸载又重载）
    if (_WHISPER_CACHE["model"] is not None
            and _WHISPER_CACHE["size"] == model_size
            and _WHISPER_CACHE["device"] == "cuda"):
        return _WHISPER_CACHE["model"]

    device, compute, why = _resolve_device()

    if (_WHISPER_CACHE["model"] is not None
            and _WHISPER_CACHE["size"] == model_size
            and _WHISPER_CACHE["device"] == device):
        return _WHISPER_CACHE["model"]

    try:
        model = _load_whisper_model(src, device, compute)
    except Exception as e:
        if device == "cpu":
            raise
        _CUDA_DISABLED["why"] = "%s: %s" % (type(e).__name__, e)
        print("[video] GPU 加载失败 %r → 回退 CPU 重试" % (e,), flush=True)
        device, compute = "cpu", _COMPUTE_BY_DEVICE["cpu"]
        model = _load_whisper_model(src, device, compute)

    _WHISPER_CACHE.update({"model": model, "size": model_size,
                           "device": device, "compute": compute})
    print("[video] whisper 模型就绪: %s (device=%s compute=%s%s)"
          % (src, device, compute, ("｜" + why) if why else ""), flush=True)
    return _WHISPER_CACHE["model"]


def _note_timeout(kind="转写"):
    """转写/解码在 GPU 上挂死 → 本进程内永久降级 CPU，别让下一条再撞同一堵墙。"""
    if _WHISPER_CACHE.get("device") != "cuda":
        return
    _CUDA_DISABLED["why"] = "%s在 GPU 上超时挂死" % kind
    _WHISPER_CACHE["model"] = None      # 丢掉可疑句柄，下次按新设备重建
    print("[video] ⚠ GPU %s超时 → 本进程后续转写降级 CPU" % kind, flush=True)


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
            set_progress("转写中", "模型加载/语言检测中…")
            # 语言自动检测：任何语种音频都全量转写，不再强制中文
            # （强制 zh 会让英文等外语视频被硬解码成中文碎片，整段失真）
            segs, _ = model.transcribe(video_path, beam_size=5)
            cnt, texts = 0, []
            for s in segs:
                texts.append(s.text)
                cnt += 1
                if cnt % 10 == 0:
                    set_progress("转写中", f"已完成 {cnt} 段语音识别")
            result["text"] = "\n".join(texts).strip()
        except Exception as e:
            import traceback
            traceback.print_exc()  # 完整堆栈进服务日志，便于事后排查
            result["err"] = f"转写出错：{type(e).__name__}: {e}"

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(WHISPER_TIMEOUT)
    if th.is_alive():
        was_cuda = _WHISPER_CACHE.get("device") == "cuda"
        _note_timeout()
        return "", (f"转写超时（>{WHISPER_TIMEOUT}s 未完成，模型或解码疑似挂死），已中止本条"
                    + ("；本进程后续转写改用 CPU" if was_cuda else ""))
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
        was_cuda = _WHISPER_CACHE.get("device") == "cuda"
        _note_timeout("分段转写")
        return [], "", (f"转写超时（>{WHISPER_TIMEOUT}s 未完成）"
                        + ("；本进程后续转写改用 CPU" if was_cuda else ""))
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
    frames_block = ""
    if descs_text and descs_text != "（无关键帧描述）":
        frames_block = f"【关键帧画面描述】\n{descs_text}\n\n"
    prompt = (
        "你是知识库整理助手。基于一段视频的【语音转写】，产出结构化笔记。"
        "请只输出一个 JSON 对象，字段如下（不要输出任何多余文字、不要 Markdown 代码块标记）：\n"
        "{\n"
        '  "标题": "一句话标题（15字内）",\n'
        '  "摘要": "60~160字的摘要（必须大于50字，见下方摘要规则）",\n'
        '  "制作流程": [{"步骤":"1","操作":"本步做什么","用料":"本步用到的原料/工具","参数":"温度/时间/比例等(无则空串)","要点":""}, ...],\n'
        '  "要点": ["要点1","要点2",...],\n'
        '  "章节": [{"时间":"","主题":""}, ...],\n'
        '  "标签": ["标签1","标签2",...],\n'
        '  "关键结论": "可执行的结论或金句（如有）"\n'
        "}\n\n"
        "【摘要】填写规则（极重要）：\n"
        "1. 摘要**必须大于50字**（60~160字为宜）。把视频最有价值的信息浓缩进去：\n"
        "   讲了什么主题、核心观点/做法是什么、有哪些关键数据/人物/结论，读摘要即可了解全片。\n"
        "2. 从【语音转写】中蒸馏，带具体人物/机构/数字/因果关系，\n"
        "   **严禁**「这个视频聊了一个话题」式的空话；转写口语错误请自行纠正为规范书面语。\n"
        "3. 唯一例外：【语音转写】全文本身不足50字时，才允许写更短的摘要。\n"
        "4. 转写可能为繁体/夹杂错别字（语音识别产物），输出一律用**规范简体中文书面语**。\n\n"
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
        "2. 每一步必须基于【语音转写】中的真实信息，**严禁编造**；"
        "信息不足的步骤请如实标注（语音未明确说明），不要脑补步骤名或参数。\n"
        "3. 若为普通内容（非制作方法类），给空数组 []。\n\n"
        f"{frames_block}"
        f"【语音转写】\n{transcript_text}\n"
    )
    raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=1800, keep_alive="10m")
    return _parse_summary_json(raw, source_meta)


def _parse_summary_json(raw, source_meta):
    """容错解析 Ollama 输出的 JSON；先抽取 {...} 块再解析，失败退化为原始文本摘要。

    解析失败时打 _json_broken 标记并保存原始输出，由 summarize() 触发专用摘要重试，
    绝不把模型回答的半截 Markdown 残片当摘要糊弄。
    """
    m = re.search(r"\{.*\}", raw or "", re.S)
    if m:
        raw = m.group(0)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"标题": source_meta.get("title") or "未命名视频",
                "摘要": "", "要点": [], "章节": [],
                "标签": [], "关键结论": "",
                "_json_broken": True, "_raw_output": (raw or "")[:3000]}


def _extract_points(transcript_text, max_chars=9000):
    """分段提取式要点抽取（救援通道的核心）。

    实战教训（qwen3:14b 实测三轮）：
    - 要 JSON 它输出 Markdown 分析文 → 解析失败；
    - 要「150字一段摘要」它能写 2000 字分析文；
    - think 开启时思考块吃光 num_predict 配额 → 正文为空；
    - 「提取要点列表」类任务的服从性远高于「写一段话」类任务。
    所以最终形态：提取式任务（每段 3~5 条要点行）+ think 禁用 + num_predict 硬限
    + 分段提取：把转写切成 ≤2500 字/段，每段独立提取 3~5 条要点后按序合并——
    每一段都必然被模型看过，全片覆盖是结构保证，不赌单次长输入的服从性
    （实测单次喂 6608 字时模型经常只总结前半）。

    返回：按视频顺序的要点句列表（无句读补全）；转写过短或全部失败返回 []。
    """
    text = (transcript_text or "").strip()
    if len(text) < 60:
        return []
    chunks = _split_transcript(text[:max_chars], 2500)
    all_pts = []
    for i, ch in enumerate(chunks):
        prompt = (
            f"以下是一段视频语音转写稿的第 {i+1}/{len(chunks)} 部分。"
            "请从这部分按顺序提取 3~5 个最重要的信息点，每行一条、以「- 」开头：\n"
            "①改写为规范简体中文完整陈述句（转写为繁体口语稿、有错别字，请纠正）；\n"
            "②保留具体人物、著作、数字、因果关系；\n"
            "③除这些行外不要输出任何其他内容——不要建议、不要脚本、不要标题。\n\n"
            f"【转写 第{i+1}/{len(chunks)}部分】\n{ch}\n"
        )
        raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=1200, keep_alive="10m",
                               options={"temperature": 0.6, "num_predict": 500},
                               think=False)
        if raw.startswith("[Ollama 调用失败"):
            continue
        for ln in raw.splitlines():
            s = ln.strip()
            m = re.match(r"^(?:[-*•]|\d{1,2}[.、:：)）])\s*(.+)$", s)
            if m:
                t = re.sub(r"[*_`#]+", "", m.group(1)).strip()
                if len(t) >= 8:
                    all_pts.append(t)
    return all_pts


def _points_to_paragraph(all_pts, max_len=500):
    """要点句按序拼成一段完整摘要（句读补全 + 长度上限）。"""
    out, total = [], 0
    for t in all_pts:
        out.append(t if t.endswith(("。", "！", "？")) else t + "。")
        total += len(out[-1])
        if total >= max_len:
            break
    return "".join(out)


def _quick_summary(transcript_text, max_chars=9000):
    """专用摘要请求（结构化解析失败后的救援通道）。

    分段提取要点后拼成一段覆盖全片的完整摘要（详见 _extract_points）。
    """
    pts = _extract_points(transcript_text, max_chars)
    if not pts:
        return ""
    return _points_to_paragraph(pts)


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


def _ensure_summary_len(data, summarize_text):
    """摘要硬约束守卫（确定性，不依赖模型自觉）：

    转写源文本 ≥50 字时，摘要必须 ≥50 字；不达标则依序补足——
    ① 用要点拼接（含具体细节，质量最好）→ ② 取转写开头的完整句子。
    转写源文本 <50 字时放行（源材料就那么多，摘要短情有可原）。
    """
    sm = (data.get("摘要") or "").strip()
    src = re.sub(r"\s+", "", summarize_text or "")
    if len(sm) >= 50 or len(src) < 50:
        return data
    # ① 要点拼接
    pts = [p.strip() for p in (data.get("要点") or []) if len(p.strip()) >= 6]
    fill = sm
    for p in pts:
        if len(fill) >= 50:
            break
        fill = f"{fill}；{p}" if fill else p
    if len(fill) >= 50:
        data["摘要"] = fill
        return data
    # ② 转写均匀采样兜底：开头/中段/结尾各取完整句拼接（避免只概括开场白）
    # 🔴 教训（2026-10-24）：whisper 转写可能整篇没有句号（仅换行分段），
    #    先去空白再按句号切会得到「一整个巨型句子」→ 整篇转写被塞进摘要。
    #    修复：按标点+换行切句（切句用原文，去空白只用于长度判断），
    #    单句超长再按长度硬切，且总长封顶。
    src_raw = (summarize_text or "").strip()
    if len(re.sub(r"\s+", "", src_raw)) < 50:
        return data
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;\n])", src_raw)
             if len(re.sub(r"\s+", "", s)) >= 15]
    # 无句读/句读稀疏的转写：单句超长时按 40 字硬切，保证「采样」有多个取样点
    fixed = []
    for s in sents:
        while len(s) > 80:
            fixed.append(s[:40])
            s = s[40:]
        fixed.append(s)
    sents = fixed
    if sents:
        step = max(1, len(sents) // 6)
        picks, total = [], 0
        for s in sents[::step]:
            picks.append(s)
            total += len(s)
            if total >= 120:
                break
        joined = "……".join(picks)
        data["摘要"] = joined[:200] + ("……" if len(joined) > 200 else "")
    return data


def _rescue_summary(data, transcript):
    """结构化解析失败（JSON broken / 要点章节全空）时的救援：

    用 _extract_points 分段提取产出**全片要点列表**，再拼成一段完整摘要
    （覆盖全片，非开头残片）——摘要与要点一次同时产出，替代把模型回答的
    半截 Markdown 当摘要的旧行为。救援失败则留空交给守卫兜底。
    """
    broken = data.get("_json_broken")
    structured = bool(data.get("要点") or data.get("章节"))
    if not broken and structured:
        return data
    need_summary = not (data.get("摘要") or "").strip()
    need_points = not data.get("要点")
    if need_summary or need_points:
        pts = _extract_points(transcript)
        if pts:
            if need_summary:
                data["摘要"] = _points_to_paragraph(pts)
            if need_points:
                data["要点"] = pts
    data.pop("_json_broken", None)
    data.pop("_raw_output", None)
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
        "摘要": ("。".join(pts[:6])[:200] if pts else "（蒸馏失败，请查看全文转写）"),
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
    set_progress("总结中" if n == 1 else "蒸馏中",
                 f"转写 {len(transcript or '')} 字" + (f"，分 {n} 段蒸馏" if n > 1 else ""))

    if n == 1:
        data = _summarize_single(transcript[:12000], descs_text, source_meta)
        data["_distill_chunks"] = 1
        data = _rescue_summary(data, transcript)
        # 瞬时故障重试（Ollama 卸载/重载窗口期）：摘要+要点全空时整链重试一次
        # 🔴 教训（2026-10-24）：转写后 Ollama 冷加载期调用全灭，救援提取同样失败，
        #    兜底把整篇转写塞进摘要。重试一次成本 ~20s，能救回绝大多数瞬时故障。
        if not (data.get("摘要") or "").strip() and not data.get("要点"):
            time.sleep(5)
            data = _summarize_single(transcript[:12000], descs_text, source_meta)
            data["_distill_chunks"] = 1
            data = _rescue_summary(data, transcript)
        return _ensure_summary_len(data, transcript)

    # ---- map：逐段蒸馏要点（纯文本协议，容错解析；不强迫模型吐 JSON） ----
    map_items = []   # [(主题, [要点...], 关键结论), ...]
    for i, ch in enumerate(chunks):
        set_progress("蒸馏中", f"第 {i+1}/{n} 段要点提取（qwen3:14b）")
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
        return _ensure_summary_len(data, transcript)

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
        "摘要：<60~160字的整体摘要，**必须大于50字**：主题+核心观点/做法+关键数据结论，"
        "读摘要即可了解全片，严禁空话>\n"
        "要点：\n"
        "- <要点1，保留具体细节>\n"
        "- <要点2>\n"
        "（8~15 条；合并重复，保留全部具体事实细节，严禁空泛概括）\n"
        "关键结论：<全片最核心的可执行结论或金句>\n\n"
        f"【分段要点汇总（共{n}段）】\n{seg_text}\n"
    )
    set_progress("蒸馏中", f"{n} 段要点合并去重")
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
    return _ensure_summary_len(data, transcript)


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
                  bilingual=False, first_frame_only=False, skill_distill=False):
    """主流程：单视频 -> Markdown + 落库。返回结果 dict。

    bilingual: 双语字幕模式——转写语言自动检测，非中文音频产出「原文/中文对照」表
               （中文翻译走本地 qwen3:14b）；中文音频自动退回普通模式并标注。
    first_frame_only: 已废弃（关键帧抓取已取消），保留参数仅为兼容旧调用方。
    skill_distill: 仓颉技能包蒸馏分支（嫁接 cangjie-skill）——总结完成后对转写做
                   方法论判定，命中则额外产出 <slug>_技能包.md（RIA 能力卡），
                   并同步 LifeQA/knowledge/skillpacks/ 供 RAG 问答。
    """
    video_path = str(video_path)
    if not os.path.exists(video_path):
        return {"ok": False, "error": f"视频不存在：{video_path}"}
    stem = Path(video_path).stem or "video"
    slug = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w一-鿿-]", "", stem)[:30]

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
    # 1.5) 关键帧抓取已取消（2026-10-14 用户拍板）：不再抽帧、不再跑 qwen3-vl 视觉理解，
    #      总结完全基于语音转写；extract_keyframes/describe_frames 函数保留但主流水线不再调用。
    #      抽帧耗时大（长视频 ffmpeg 解码 + 逐帧 VL 请求），而转写文本已含全部信息量。
    frames, frame_descs = [], []
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

    # 6) 仓颉技能包蒸馏分支（仅勾选时跑；判定不通过自动跳过，不浪费算力）
    if skill_distill:
        try:
            import skill_distill as sd
            meta_sd = dict(meta)
            meta_sd["slug"] = slug
            r = sd.distill(transcript, meta_sd, write_vault=write_vault)
            result["skillpack"] = {k: v for k, v in r.items() if k != "markdown"}
        except Exception as e:
            result["skillpack"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

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
