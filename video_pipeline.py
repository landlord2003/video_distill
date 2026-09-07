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
SUMMARY_MODEL = "qwen3:14b"
VISION_MODEL = "qwen3-vl:8b"
TRANSCRIBE_MODEL = "tiny"                            # faster-whisper 模型尺寸

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
def _ollama_generate(model, prompt, images=None, timeout=180):
    """调用本机 Ollama /api/generate，返回文本。images: 本地图片路径列表。"""
    payload = {"model": model, "prompt": prompt, "stream": False}
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
def transcribe(video_path, model_size=TRANSCRIBE_MODEL):
    """faster-whisper 转写。不可用则返回 ('', 原因)。"""
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        return "", f"faster-whisper 未安装：{e}"
    try:
        # device auto 在 Blackwell(RTX50x0) 上会落到 CPU 若 CUDA 不可用；int8 省显存
        model = WhisperModel(model_size, device="auto", compute_type="int8")
        segs, _ = model.transcribe(video_path, beam_size=5, language="zh")
        text = "\n".join(s.text for s in segs).strip()
        return text, None
    except Exception as e:
        return "", f"转写出错：{type(e).__name__}: {e}"


# ---------------- 关键帧 ----------------
def extract_keyframes(video_path, out_dir, n=4):
    """均匀抽 n 帧，返回图片路径列表（失败返回 []）。不依赖 ffprobe/时长解析。"""
    ff = get_ffmpeg()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    pat = str(out_dir / f"{stem}_%03d.jpg")
    # 每 ~2 秒抽 1 帧（fps=1/2），不依赖视频时长
    subprocess.run([ff, "-y", "-i", video_path, "-vf", "fps=1/2", "-q:v", "3", pat],
                   capture_output=True)
    files = sorted(out_dir.glob(f"{stem}_*.jpg"))
    if not files:
        single = str(out_dir / f"{stem}_001.jpg")
        subprocess.run([ff, "-y", "-ss", "0.5", "-i", video_path,
                        "-frames:v", "1", "-q:v", "3", single], capture_output=True)
        files = sorted(out_dir.glob(f"{stem}_*.jpg"))
    if not files:
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
def describe_frames(frame_paths, max_frames=4):
    if not frame_paths:
        return []
    sel = frame_paths[:max_frames]
    prompt = ("你是一名视频内容分析助手。下面是一段视频中抽取的几张关键帧画面。"
              "请逐张用一句话（中文）描述每张画面中可见的关键信息（人物、场景、文字、物体、动作）。"
              "请严格按如下格式输出，不要多余解释：\n"
              "帧1：<描述>\n帧2：<描述>\n...")
    out = _ollama_generate(VISION_MODEL, prompt, images=sel, timeout=240)
    # 解析 帧N：... 行
    descs = []
    for line in out.splitlines():
        m = re.match(r"^\s*帧\s*\d+\s*[:：]\s*(.*)$", line)
        if m:
            descs.append(m.group(1).strip())
    return descs


# ---------------- 结构化总结 ----------------
def summarize(transcript, frame_descs, source_meta):
    descs_text = "\n".join(f"- {d}" for d in frame_descs) if frame_descs else "（无关键帧描述）"
    transcript_text = (transcript[:6000] if transcript else "（无转写文本）")
    prompt = (
        "你是知识库整理助手。基于一段视频的【语音转写】与【关键帧画面描述】，"
        "产出结构化笔记。请只输出一个 JSON 对象，字段如下（不要输出任何多余文字、不要 Markdown 代码块标记）：\n"
        "{\n"
        '  "标题": "一句话标题（15字内）",\n'
        '  "摘要": "100字内的一句话摘要",\n'
        '  "要点": ["要点1","要点2",...],\n'
        '  "章节": [{"时间":"","主题":""}, ...],\n'
        '  "标签": ["标签1","标签2",...],\n'
        '  "关键结论": "可执行的结论或金句（如有）"\n'
        "}\n\n"
        f"【关键帧画面描述】\n{descs_text}\n\n"
        f"【语音转写】\n{transcript_text}\n"
    )
    raw = _ollama_generate(SUMMARY_MODEL, prompt, timeout=300)
    # 解析 JSON（容错：去掉代码块标记）
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception:
        # 退化：把原始输出当摘要
        data = {"标题": source_meta.get("title") or "未命名视频",
                "摘要": raw[:200], "要点": [], "章节": [],
                "标签": [], "关键结论": ""}
    return data


# ---------------- 拼装 Markdown ----------------
def build_markdown(meta, transcript, frame_descs, summary, frames):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    title = summary.get("标题") or meta.get("title") or "未命名视频"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 来源：{meta.get('source','')}  ")
    lines.append(f"> 原始链接：{meta.get('url','')}  ")
    lines.append(f"> 整理时间：{now}  ")
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
            rel = os.path.basename(fp)
            lines.append(f"![关键帧{i+1}](frames/{rel})")
            if i < len(frame_descs):
                lines.append(f"> {frame_descs[i]}")
            lines.append("")
    if transcript:
        lines.append("## 语音转写全文")
        lines.append("")
        lines.append(transcript)
    lines.append("")
    return "\n".join(lines)


# ---------------- 写入 Obsidian 库 ----------------
def write_vault(md, slug):
    os.makedirs(VAULT, exist_ok=True)
    fname = f"{slug}.md"
    path = os.path.join(VAULT, fname)
    # 复制关键帧到库内 frames/
    return path


def process_video(video_path, url="", source="", write_vault=True, model_size=TRANSCRIBE_MODEL):
    """主流程：单视频 -> Markdown + 落库。返回结果 dict。"""
    video_path = str(video_path)
    if not os.path.exists(video_path):
        return {"ok": False, "error": f"视频不存在：{video_path}"}
    stem = Path(video_path).stem or "video"
    slug = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w一-鿿-]", "", stem)[:30]
    frames_dir = os.path.join(FRAMES, slug)
    os.makedirs(frames_dir, exist_ok=True)

    meta = {"source": source or "视频号/抖音", "url": url, "title": stem}

    # 1) 转写
    transcript, terr = transcribe(video_path, model_size)
    # 2) 关键帧
    frames = extract_keyframes(video_path, frames_dir, n=4)
    # 3) 视觉理解
    frame_descs = describe_frames(frames)
    # 4) 结构化总结
    summary = summarize(transcript, frame_descs, meta)
    # 5) 拼装
    md = build_markdown(meta, transcript, frame_descs, summary, frames)

    result = {
        "ok": True,
        "slug": slug,
        "title": summary.get("标题") or stem,
        "video_path": video_path,
        "transcript_len": len(transcript),
        "transcript_error": terr,
        "frames": len(frames),
        "tags": summary.get("标签", []),
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
