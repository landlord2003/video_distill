# -*- coding: utf-8 -*-
"""第三步：要点解析（文字类来源）—— 复用本机 Ollama，零外部 API。

标准化三步流水线（全来源统一）：
  ① 抓取   ：7 源统一入口，按内容形态自动分类
             视频/音频 -> 转录路线（抖音/YouTube/B站/Twitter视频推文）
             文字/图片 -> 直接路线（Twitter/微信/小红书/网页）
  ② 归一化 -> Markdown：文字直接入库；视频/音频 faster-whisper 转录；
             图片本地化（视觉理解走 qwen3-vl:8b）
  ③ 解析   ：可选可配 ——
             视频类标配已含（video_pipeline.py 分段蒸馏：标题/摘要/要点/章节/标签）
             文字类由本模块补齐：调本机 qwen3:14b 出「一句话摘要 + 要点 + 标签」，
             注入 md 的「📌 要点解析」段。

输出协议采用纯文本固定格式（与 video_pipeline 蒸馏同策略，容错解析，
不强迫模型吐 JSON）。"""
import os
import re

import video_pipeline as vp

ANALYZE_MODEL = os.environ.get("SUMMARY_MODEL", vp.SUMMARY_MODEL)
# 送入模型的正文字符上限（qwen3:14b 上下文留余量）
MAX_PROMPT_CHARS = 6000
# 正文短于该长度不做解析（推文一句话也值得摘要，阈值放低）
MIN_TEXT_CHARS = 120


def _strip_for_prompt(md: str) -> str:
    """从 md 提取纯正文：去 frontmatter / 图片行 / 引用链接行 / HTML 标签。"""
    s = md or ""
    # frontmatter
    s = re.sub(r"\A---\n.*?\n---\n", "", s, flags=re.S)
    # 图片行、视频引用行、原文链接行
    s = re.sub(r"^!\[.*$", "", s, flags=re.M)
    s = re.sub(r"^> .*$", "", s, flags=re.M)
    s = re.sub(r"^原文[:：].*$", "", s, flags=re.M)
    # HTML 标签与多余空行
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()[:MAX_PROMPT_CHARS]


_PROMPT = (
    "你是知识库整理助手。阅读下面的文章内容，严格按以下格式输出（不要输出任何其他内容）：\n"
    "一句话摘要：<60字以内，概括核心观点>\n"
    "要点：\n"
    "1. <信息完整的句子，提炼具体事实或论点，共6~10条；内容不足则减少条数>\n"
    "标签：<3~5个主题词，用、分隔>\n"
    "\n【文章内容】\n"
)


def analyze_text(text: str) -> dict:
    """正文 -> {summary, points[], tags[], ok}。失败 ok=False（不阻断入库）。"""
    body = (text or "").strip()
    out = {"ok": False, "summary": "", "points": [], "tags": []}
    if len(body) < MIN_TEXT_CHARS:
        out["error"] = f"正文过短（{len(body)} 字），跳过解析"
        return out
    raw = vp._ollama_generate(ANALYZE_MODEL, _PROMPT + body, timeout=420,
                              keep_alive="10m")
    if raw.startswith("[Ollama 调用失败"):
        out["error"] = raw[:200]
        return out
    summary, points, tags = "", [], []
    in_points = False
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"一句话摘要[:：]\s*(.+)", ln)
        if m and not summary:
            summary = m.group(1).strip()
            in_points = False
            continue
        m = re.match(r"标签[:：]\s*(.+)", ln)
        if m:
            tags = [t.strip() for t in re.split(r"[、,，;；/]", m.group(1)) if t.strip()]
            in_points = False
            continue
        if re.match(r"要点[:：]?\s*$", ln):
            in_points = True
            continue
        pm = re.match(r"(\d{1,2})[.、)）]\s*(.+)", ln)
        if pm:
            in_points = True
            if len(points) < 12:
                points.append(pm.group(2).strip())
            continue
        if in_points and ln and not ln.startswith("#"):
            # 要点段落里的续行（模型偶尔不带编号）
            if 0 < len(points) < 12 and len(ln) > 8:
                points.append(ln)
    if not summary and points:
        summary = points[0][:60]
    out.update({"ok": bool(summary or points), "summary": summary,
                "points": points, "tags": tags})
    if not out["ok"]:
        out["error"] = "解析输出为空（模型未按格式返回）"
    return out


def _format_section(res: dict) -> str:
    """解析结果 -> 「📌 要点解析」md 段（无内容返回空串）。"""
    if not res.get("ok"):
        return ""
    lines = ["## 📌 要点解析", ""]
    if res.get("summary"):
        lines += [f"**摘要**：{res['summary']}", ""]
    pts = res.get("points") or []
    if pts:
        lines += [p if re.match(r"^\d{1,2}[.、)）]", p) else f"{i}. {p}"
                  for i, p in enumerate(pts, 1)]
        lines.append("")
    tags = res.get("tags") or []
    if tags:
        lines += ["`" + "` `".join(tags[:5]) + "`", ""]
    return "\n".join(lines)


def analyze_and_decorate(md: str) -> dict:
    """md -> {md(注入解析段后), analyze(结果dict)}。失败时原样返回。"""
    body = _strip_for_prompt(md)
    res = analyze_text(body)
    sec = _format_section(res)
    out = {"md": md, "analyze": res}
    if not sec:
        return out
    lines = md.splitlines()
    # 注入位置：frontmatter 之后的第一个一级标题后；无标题则 frontmatter 后
    insert_at = 0
    fm_end = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i + 1
                break
    insert_at = fm_end
    for i in range(fm_end, len(lines)):
        if lines[i].startswith("# "):
            insert_at = i + 1
            break
    else:
        insert_at = fm_end
    # 空行对齐：标题后空一行再插
    decorated = lines[:insert_at] + ["", sec.rstrip()] + lines[insert_at:]
    out["md"] = "\n".join(decorated).replace("\n\n\n\n", "\n\n\n")
    return out
