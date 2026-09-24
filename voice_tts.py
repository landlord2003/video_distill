#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VoiceStudio 本地 TTS 桥接（voice_distill 工具用）

对接本机 VoiceStudio 后端（debpalash/VoiceStudio，OpenAI 兼容 API）：
  - GET  http://127.0.0.1:3900/health            健康检查（含 device）
  - GET  http://127.0.0.1:3900/profiles          音色 profile 列表
  - POST http://127.0.0.1:3900/v1/audio/speech   OpenAI 兼容 TTS（input ≤4096 字）

设计：
  - 零重依赖：urllib 直连，不走系统代理（--noproxy 语义）
  - 后端未启动时所有函数返回明确错误，不抛堆栈
  - 长文本 >4096 字自动按句子边界切段逐段合成，返回多段
"""
import json
import re
import urllib.request

VS_BASE = "http://127.0.0.1:3900"
MAX_CHARS = 4096          # 单次合成上限（后端 SpeechRequest 硬限制）
CHUNK_CHARS = 2000        # 自动分段阈值：留余量防标点统计差异
TIMEOUT_HEALTH = 4
TIMEOUT_GEN = 600         # 首次合成可能懒加载模型（~2.4GB），放宽


def _opener():
    """绕过一切系统/环境代理，直连本机后端。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health():
    """返回 (up: bool, info: dict)。info 含 device/version/status。"""
    try:
        r = _opener().open(VS_BASE + "/health", timeout=TIMEOUT_HEALTH)
        return True, json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return False, {"error": str(e)}


def list_voices():
    """返回 (voices, err)。voices: [{id,name,language,personality}...]"""
    try:
        r = _opener().open(VS_BASE + "/profiles", timeout=TIMEOUT_HEALTH)
        d = json.loads(r.read().decode("utf-8", "ignore"))
        items = d if isinstance(d, list) else d.get("profiles", d.get("items", []))
        voices = [{
            "id": p.get("id", ""),
            "name": p.get("name", p.get("id", "")),
            "language": p.get("language", ""),
            "personality": p.get("personality", ""),
        } for p in items if isinstance(p, dict)]
        return voices, None
    except Exception as e:
        return [], str(e)


def _split_text(text: str):
    """>CHUNK_CHARS 的长文按句末标点切分（中文句号问号叹号分号 + 英文句点）。"""
    if len(text) <= CHUNK_CHARS:
        return [text]
    parts, buf = [], ""
    # 按句子切（保留标点）
    for seg in re.split(r"(?<=[。！？；.!?])", text):
        if not seg:
            continue
        if len(buf) + len(seg) > CHUNK_CHARS and buf:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        parts.append(buf)
    return parts


def synthesize(text: str, voice: str = "demo0001", fmt: str = "mp3", steps: int = 16):
    """合成语音。返回 (audio_bytes, ext, n_chunks, err)。

    fmt: mp3/wav（mp3 体积小，前端直接能播）
    steps: 8=草稿 16=均衡 32=高质量
    长文本自动分段，顺序拼接为单一音频流。
    """
    text = (text or "").strip()
    if not text:
        return None, "", 0, "文本为空"
    chunks = _split_text(text)
    audio = b""
    for i, ch in enumerate(chunks):
        payload = json.dumps({
            "model": "omnivoice",
            "input": ch,
            "voice": voice or "demo0001",
            "response_format": fmt,
            "steps": steps,
        }).encode("utf-8")
        req = urllib.request.Request(
            VS_BASE + "/v1/audio/speech", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            r = _opener().open(req, timeout=TIMEOUT_GEN)
            audio += r.read()
        except Exception as e:
            return None, "", i, f"第 {i + 1}/{len(chunks)} 段合成失败: {e}"
    return audio, fmt, len(chunks), None


if __name__ == "__main__":
    up, info = health()
    print("health:", up, info)
    if up:
        vs, err = list_voices()
        print("voices:", vs, err)
        b, ext, n, e2 = synthesize("本地语音合成桥接测试。", steps=8)
        print("gen:", len(b) if b else None, ext, n, e2)
