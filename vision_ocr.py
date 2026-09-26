#!/usr/bin/env python3
"""本地图片文字识别（Ollama qwen3-vl，零 API 费用）。

用途：小红书等平台的图片型笔记（正文全在图里，desc 为空），
抓取后逐图 OCR 拼成文字段，让三步流水线的「要点解析」有文字可用。
"""

import base64
import json
import os
import urllib.request

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("OCR_MODEL", "qwen3-vl:8b")

_PROMPT = (
    "请把图片中的文字完整地转录出来，按原文段落顺序输出，保持原有分段。"
)

# 实测：temperature=0 会让 qwen3-vl 对部分图片陷入空响应（稳定复现），
# 默认温度反而正常；空响应时再重试一次兜底偶发失败


def ocr_image(path: str, timeout: int = 180, retries: int = 1) -> str:
    """单图 OCR，返回识别文字（失败返回空串）。"""
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(
                OLLAMA.rstrip("/") + "/api/generate",
                data=json.dumps({
                    "model": MODEL,
                    "prompt": _PROMPT,
                    "images": [b64],
                    "stream": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "ignore"))
            t = (d.get("response") or "").strip()
            if t:
                return t
        except Exception:
            pass
    return ""


def ocr_images(paths, progress=None):
    """批量 OCR，返回与输入等长的文字列表（失败项为空串）。"""
    out = []
    for i, p in enumerate(paths, 1):
        if progress:
            try:
                progress(i, len(paths))
            except Exception:
                pass
        out.append(ocr_image(p) if p and os.path.isfile(p) else "")
    return out
