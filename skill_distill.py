# -*- coding: utf-8 -*-
"""skill_distill.py — 仓颉要点蒸馏分支（嫁接 cangjie-skill 方法论到 video_distill）

嫁接逻辑（2026-10-24 老吴拍板）：
  crawl4ai/video_distill 是采集层，采集之后的「转录文字要点整理」正是 cangjie-skill
  的主场——其 SKILL.md 明确："书"泛指一切长内容：书籍、长视频转写、播客文字稿。

管线：
  转写全文 → 方法论内容判定（确定性关键词打分 + 灰区 LLM 闸门）
          → RIA 能力卡提取（长转写 map-reduce，复用 video_pipeline 分段与 Ollama 封装）
          → 技能包 Markdown 落 Obsidian 库（<slug>_技能包.md）
          → 可选同步 LifeQA/knowledge/skillpacks/（ingest.py 增量入库即可问答）

输出能力卡结构（RIA-lite，适配本机 qwen3:14b 精度，保留仓颉六段骨架）：
  R 触发场景 / I 操作步骤 / A1 原片案例 / E 验收标准 / B 常见错误与边界 / 来源引文(≤150字)

CLI：
  python skill_distill.py --transcript x.txt --title "标题"          # 单篇蒸馏
  python skill_distill.py --backfill <md文件或目录>                  # 存量笔记回填
  python skill_distill.py --evaluate <技能包.md> [--out eval.json]   # darwin 兼容评测

诚实边界：
  - 本机 14b 出的技能包质量低于官方 cangjie 全流水线（5-agent + 三重验证）；
    这里取其"可执行性优先"的内核，验证降为单闸门。
  - 评测用例格式遵循 darwin-skill 通用评测约定（input/expected/scorer），
    可直接人工喂给 darwin-skill 做自动进化。
"""
import io
import json
import os
import re
import sys
import datetime

import video_pipeline as vp

# 技能包同步目标（LifeQA 知识库，ingest.py 增量入库即问即答）
LIFEQA_KB = r"E:\Workbuddy\LifeQA\knowledge\skillpacks"
# 方法论判定分数阈值：>=HIGH 直接判是，<=LOW 直接判否，中间灰区交给 LLM 闸门
_THRESHOLD_HIGH, _THRESHOLD_LOW = 8, 3

# ---------------- 方法论内容判定（确定性优先，省 LLM 调用） ----------------
_METH_HINTS = [
    "第一步", "第二步", "第三步", "步骤", "流程", "方法", "教程", "怎么做",
    "如何", "框架", "模型", "原则", "策略", "体系", "套路", "心法",
    "清单", "口诀", "公式", "要诀", "判断标准", "避坑", "误区", "实战",
]

def methodology_score(transcript):
    """确定性打分：命中关键词密度（每千字命中数）+ 结构信号（编号步骤）。"""
    if not transcript or len(transcript) < 300:
        return 0
    n = len(transcript) / 1000.0
    hits = sum(transcript.count(k) for k in _METH_HINTS)
    density = hits / max(n, 0.3)
    score = int(density)
    # 结构信号：出现"1. 2. 3."或"第一步/第1步"式编号步骤给加权
    if len(re.findall(r"(?:第[一二三四五六七八九十\d]+[步步]|[1-9][.、）)])\s*\S{4,}", transcript)) >= 3:
        score += 3
    return score


def _llm_is_methodological(transcript, meta):
    """灰区闸门：一次廉价 LLM 判定。返回 (bool, 一句话理由)。"""
    prompt = (
        "判断下面这份视频转写的内容类型。只输出一行，格式严格为：\n"
        "判定：<方法论|叙事|资讯|娱乐> ； 理由：<15字内>\n"
        "「方法论」= 教了可复用的方法/步骤/框架/原则，学会能照做。\n\n"
        f"【标题】{meta.get('title','')}\n【转写节选】\n{transcript[:3000]}\n"
    )
    raw = vp._ollama_generate(vp.SUMMARY_MODEL, prompt, timeout=300)
    m = re.search(r"判定\s*[:：]\s*(方法论|叙事|资讯|娱乐)", raw or "")
    return bool(m and m.group(1) == "方法论"), (raw or "").strip()[:60]


def is_methodological(transcript, meta):
    """三级判定：确定性高分→是；低分→否；灰区→LLM 闸门。返回 (bool, 依据)。"""
    s = methodology_score(transcript)
    if s >= _THRESHOLD_HIGH:
        return True, f"关键词密度打分 {s} ≥ {_THRESHOLD_HIGH}（确定性判定）"
    if s <= _THRESHOLD_LOW:
        return False, f"关键词密度打分 {s} ≤ {_THRESHOLD_LOW}（确定性判定）"
    ok, why = _llm_is_methodological(transcript, meta)
    return ok, f"灰区（打分 {s}）→ LLM 闸门：{why}"


# ---------------- RIA 能力卡提取 ----------------
_CARD_FIELDS = ("能力名", "触发场景", "操作步骤", "原片案例", "验收标准", "常见错误", "来源引文")

def _map_extract(chunk, idx, n, meta):
    """map 阶段：从一段转写中提取候选方法论单元（JSON 数组协议，容错解析）。"""
    prompt = (
        "你是方法论提取器（仓颉蒸馏法）。下面是一段长视频转写的第 %d/%d 段。\n"
        "只提取**可复用、可照做**的方法论单元：步骤流程/决策框架/判断标准/避坑清单/实操技巧。\n"
        "转写来自语音识别，同音字/错别字请依据上下文纠正。\n"
        "请只输出一个 JSON 数组（不要代码块标记、不要解释），每个元素字段：\n"
        '{"能力名":"10字内","触发场景":"什么情况下用（一句话）",'
        '"操作步骤":["步骤1","步骤2",...]（3~8步，每步是完整可执行的短句）,'
        '"原片案例":"视频里讲到的真实例子（有就写并标大概时间点，没有写空串）",'
        '"验收标准":"做完怎么算成功（可检验的一句话）",'
        '"常见错误":"新手容易踩的坑（没有写空串）",'
        '"来源引文":"支撑该方法的原话，≤150字，无则空串"}\n'
        "本段没有方法论就输出 []。严禁编造转写里没有的步骤。\n\n"
        % (idx + 1, n)
        + f"【标题】{meta.get('title','')}\n【语音转写 第%d/%d段】\n%s" % (idx + 1, n, chunk)
    )
    raw = vp._ollama_generate(vp.SUMMARY_MODEL, prompt, timeout=1200, keep_alive="10m")
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return []
    raw = re.sub(r"^```(?:json)?|```$", "", m.group(0).strip(), flags=re.M).strip()
    try:
        arr = json.loads(raw)
    except Exception:
        return []
    return [c for c in arr if isinstance(c, dict) and c.get("能力名") and c.get("操作步骤")]


def _merge_cards(cands, meta, max_cards=8):
    """reduce 阶段：合并去重候选单元，产出最终能力卡列表。"""
    seen, cards = set(), []
    for c in cands:
        key = re.sub(r"\s", "", str(c.get("能力名", "")))[:10]
        if key in seen:
            # 同名卡片：合并步骤（保序去重），其余字段留先到者
            for old in cards:
                if re.sub(r"\s", "", old["能力名"])[:10] == key:
                    for st in c.get("操作步骤", []):
                        if st not in old["操作步骤"]:
                            old["操作步骤"].append(st)
                    if not old.get("原片案例") and c.get("原片案例"):
                        old["原片案例"] = c["原片案例"]
            continue
        seen.add(key)
        steps = [s for s in (c.get("操作步骤") or []) if isinstance(s, str) and s.strip()]
        if not steps:
            continue
        quote = str(c.get("来源引文") or "")
        if len(quote) > 150:
            quote = quote[:150] + "…"
        cards.append({
            "能力名": str(c.get("能力名"))[:20],
            "触发场景": str(c.get("触发场景") or "").strip(),
            "操作步骤": steps,
            "原片案例": str(c.get("原片案例") or "").strip(),
            "验收标准": str(c.get("验收标准") or "").strip(),
            "常见错误": str(c.get("常见错误") or "").strip(),
            "来源引文": quote,
        })
        if len(cards) >= max_cards:
            break
    return cards


def distill_cards(transcript, meta):
    """转写全文 → 能力卡列表（长转写 map-reduce，短转写单段直取）。"""
    chunks = vp._split_transcript(transcript or "")
    n = len(chunks)
    cands = []
    for i, ch in enumerate(chunks):
        cands.extend(_map_extract(ch, i, n, meta))
    if not cands and n > 1:  # 分段全空时截断兜底直取一次
        cands = _map_extract(transcript[:11000], 0, 1, meta)
    return _merge_cards(cands, meta)


# ---------------- 技能包 Markdown ----------------
def _slug_id(meta):
    base = re.sub(r"[^\w一-鿿-]", "", str(meta.get("title") or "untitled"))[:20] or "untitled"
    return "sp-" + datetime.datetime.now().strftime("%Y%m%d") + "-" + base


def build_skillpack_md(meta, cards, verdict_reason, transcript_len):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "---",
        f"capability_id: {_slug_id(meta)}",
        "type: skillpack",
        "origin: video_distill × cangjie-skill (RIA-lite)",
        f"source: {meta.get('url') or meta.get('source', '')}",
        f"title: {meta.get('title', '')}",
        f"distilled_at: {now}",
        f"intents: [技能包, 方法论, {'教程'}]",
        "eval_format: darwin-compatible",
        "---",
        "",
        f"# 技能包：{meta.get('title') or '未命名'}",
        "",
        f"> 来源：{meta.get('url') or meta.get('source','')}  ",
        f"> 蒸馏时间：{now} ｜ 转写 {transcript_len} 字 ｜ 方法论判定：{verdict_reason}  ",
        "> 蒸馏方法：仓颉 RIA-lite（cangjie-skill 嫁接分支，本机 qwen3:14b，单闸门验证）",
        "",
        f"共 **{len(cards)}** 张能力卡。每张卡：R 触发场景 → I 操作步骤 → A 原片案例 → E 验收标准 → B 常见错误。",
        "",
    ]
    for i, c in enumerate(cards, 1):
        lines += [f"## 能力卡 {i}：{c['能力名']}", ""]
        lines.append(f"- **R 触发场景**：{c['触发场景'] or '（未标注）'}")
        lines.append("- **I 操作步骤**：")
        for j, s in enumerate(c["操作步骤"], 1):
            lines.append(f"  {j}. {s}")
        if c["原片案例"]:
            lines.append(f"- **A 原片案例**：{c['原片案例']}")
        lines.append(f"- **E 验收标准**：{c['验收标准'] or '（未标注，建议人工补充）'}")
        if c["常见错误"]:
            lines.append(f"- **B 常见错误**：{c['常见错误']}")
        if c["来源引文"]:
            lines.append(f"- **来源引文**：>{c['来源引文']}")
        lines.append("")
    lines += ["## 评测用例（darwin 兼容）", "",
              "运行 `python skill_distill.py --evaluate <本文件>` 生成/更新 eval JSON，",
              "评测用例按各卡的「验收标准」自动生成，可直接喂给 darwin-skill 做自动进化。", ""]
    return "\n".join(lines)


# ---------------- 主入口 / 同步 ----------------
def distill(transcript, meta, write_vault=True, sync_lifeqa=True):
    """转写 → 判定 → 技能包。返回 dict（不抛异常，失败给 error/reason）。"""
    out = {"ok": False, "is_methodological": False}
    try:
        ok, why = is_methodological(transcript, meta)
        out["is_methodological"], out["verdict"] = ok, why
        if not ok:
            out["reason"] = f"非方法论内容，跳过技能包蒸馏（{why}）"
            return out
        cards = distill_cards(transcript, meta)
        if not cards:
            out["reason"] = "判定为方法论内容，但未能提取出能力卡（提取器空返回）"
            return out
        md = build_skillpack_md(meta, cards, why, len(transcript))
        out["cards"] = len(cards)
        out["markdown"] = md
        out["ok"] = True
        if write_vault:
            os.makedirs(vp.VAULT, exist_ok=True)
            path = os.path.join(vp.VAULT, str(meta.get("slug") or _slug_id(meta)) + "_技能包.md")
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(md)
            out["vault_path"] = path
            if sync_lifeqa and os.path.isdir(os.path.dirname(LIFEQA_KB)):
                try:
                    os.makedirs(LIFEQA_KB, exist_ok=True)
                    with io.open(os.path.join(LIFEQA_KB, os.path.basename(path)), "w",
                                 encoding="utf-8") as f:
                        f.write(md)
                    out["lifeqa_path"] = os.path.join(LIFEQA_KB, os.path.basename(path))
                except Exception as e:
                    out["lifeqa_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------- P1：存量笔记回填 ----------------
_TRANSCRIPT_HEADS = ("## 语音转写全文", "## 双语字幕（原文 / 中文对照）")

def extract_transcript_from_note(md_text):
    """从存量蒸馏笔记里取转写全文；双语笔记取中文翻译列。返回 (transcript, kind)。"""
    for head in _TRANSCRIPT_HEADS:
        idx = md_text.find(head)
        if idx == -1:
            continue
        body = md_text[idx + len(head):]
        nxt = body.find("\n## ")
        body = body[:nxt] if nxt != -1 else body
        if "双语字幕" in head:
            zh = [ln.split("|")[-1].strip() for ln in body.splitlines() if ln.strip().startswith("|") and "---" not in ln]
            body = "\n".join(z for z in zh if z and z != "中文翻译")
        body = body.strip()
        if len(body) >= 500:
            return body, head
    return "", ""


def backfill(path, sync_lifeqa=True):
    """对存量蒸馏笔记（单文件或目录）补跑技能包蒸馏。返回逐条结果。"""
    files = []
    if os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.endswith(".md")]
    elif os.path.isfile(path):
        files = [path]
    results = []
    for fp in files:
        if "技能包" in os.path.basename(fp):
            continue
        try:
            with io.open(fp, "r", encoding="utf-8") as f:
                md = f.read()
        except Exception as e:
            results.append({"file": fp, "ok": False, "error": str(e)})
            continue
        tr, kind = extract_transcript_from_note(md)
        if not tr:
            results.append({"file": fp, "ok": False, "reason": "未找到≥500字的转写全文段落，跳过"})
            continue
        title = (re.search(r"^# (.+)$", md, re.M) or [None, os.path.basename(fp)])[1]
        src = (re.search(r"^> 来源：(.+)$", md, re.M) or [None, ""])[1]
        meta = {"title": title.strip(), "url": src.strip(), "source": "backfill",
                "slug": os.path.splitext(os.path.basename(fp))[0] + "-backfill"}
        r = distill(tr, meta, write_vault=True, sync_lifeqa=sync_lifeqa)
        r["file"] = fp
        results.append(r)
    return results


# ---------------- P2：darwin 兼容评测 ----------------
def parse_skillpack(md_text):
    """从技能包 md 解出能力卡列表（与 build 的结构对应）。"""
    cards, cur = [], None
    for ln in md_text.splitlines():
        m = re.match(r"^## 能力卡 \d+：(.+)$", ln)
        if m:
            cur = {"能力名": m.group(1).strip(), "触发场景": "", "操作步骤": [],
                   "原片案例": "", "验收标准": "", "常见错误": ""}
            cards.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith("- **R 触发场景**："):
            cur["触发场景"] = ln.split("：", 1)[-1].strip()
        elif re.match(r"^\s+\d+\.\s+\S", ln):
            cur["操作步骤"].append(re.sub(r"^\s+\d+\.\s+", "", ln).strip())
        elif ln.startswith("- **A 原片案例**："):
            cur["原片案例"] = ln.split("：", 1)[-1].strip()
        elif ln.startswith("- **E 验收标准**："):
            cur["验收标准"] = ln.split("：", 1)[-1].strip()
        elif ln.startswith("- **B 常见错误**："):
            cur["常见错误"] = ln.split("：", 1)[-1].strip()
    return [c for c in cards if c["操作步骤"]]


def evaluate(skillpack_path, out_path=None):
    """P2 评测：按各卡验收标准生成测试输入 → 模型照卡执行 → rubric 打分。

    输出 darwin 兼容 JSON：{"capability_id", "cases":[{"input","expected","output","score","notes"}]}
    评分 1~5：5=完全满足验收标准且步骤完整；3=基本可用有缺步；1=无法执行。
    """
    with io.open(skillpack_path, "r", encoding="utf-8") as f:
        md = f.read()
    m = re.search(r"^capability_id:\s*(.+)$", md, re.M)
    cid = m.group(1).strip() if m else os.path.basename(skillpack_path)
    cards = parse_skillpack(md)
    if not cards:
        return {"ok": False, "error": "未解析出能力卡"}
    cases = []
    for c in cards:
        steps_txt = "\n".join(f"{i}. {s}" for i, s in enumerate(c["操作步骤"], 1))
        task = (
            f"场景：{c['触发场景'] or '按该能力的典型使用场景'}。\n"
            f"任务：请严格按下面这张能力卡执行，完成后核对验收标准「{c['验收标准'] or '步骤全部落实'}」。\n\n"
            f"【能力卡：{c['能力名']}】\n{steps_txt}\n\n"
            "请直接给出执行结果（不要复述步骤）。"
        )
        raw = vp._ollama_generate(vp.SUMMARY_MODEL, task, timeout=900, keep_alive="10m")
        judge_prompt = (
            "你是评测裁判。根据【验收标准】给下面的执行结果打分（1~5 整数）。\n"
            "5=完全满足验收标准且关键步骤无缺失；4=满足但有瑕疵；3=基本可用但缺1步以上；\n"
            "2=勉强相关；1=无法执行或跑题。只输出一行：\n评分：<数字> ； 理由：<20字内>\n\n"
            f"【验收标准】{c['验收标准'] or '步骤全部落实且结果可用'}\n"
            f"【执行结果】\n{(raw or '')[:2500]}\n"
        )
        jraw = vp._ollama_generate(vp.SUMMARY_MODEL, judge_prompt, timeout=600)
        jm = re.search(r"评分\s*[:：]\s*([1-5])", jraw or "")
        rj = re.search(r"理由\s*[:：]\s*(.+)$", jraw or "", re.M)
        cases.append({
            "capability": c["能力名"],
            "input": task.split("\n\n")[0],
            "expected": c["验收标准"] or "步骤全部落实且结果可用",
            "output": (raw or "")[:2000],
            "score": int(jm.group(1)) if jm else 0,
            "notes": (rj.group(1).strip() if rj else (jraw or "")[:60]),
        })
    avg = round(sum(c["score"] for c in cases) / len(cases), 2) if cases else 0
    result = {"ok": True, "capability_id": cid, "source": skillpack_path,
              "avg_score": avg, "n_cases": len(cases), "cases": cases,
              "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    if not out_path:
        out_path = os.path.splitext(skillpack_path)[0] + "_eval.json"
    with io.open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    result["eval_path"] = out_path
    return result


# ---------------- CLI ----------------
def main():
    argv = sys.argv[1:]
    if "--backfill" in argv:
        path = argv[argv.index("--backfill") + 1]
        rs = backfill(path)
        for r in rs:
            print(json.dumps({k: v for k, v in r.items() if k != "markdown"},
                             ensure_ascii=False))
        return
    if "--evaluate" in argv:
        path = argv[argv.index("--evaluate") + 1]
        out = None
        if "--out" in argv:
            out = argv[argv.index("--out") + 1]
        r = evaluate(path, out)
        print(json.dumps({k: v for k, v in r.items() if k != "cases"},
                         ensure_ascii=False, indent=2))
        for c in r.get("cases", []):
            print(f"  [{c['score']}/5] {c['capability']} — {c['notes']}")
        return
    if "--transcript" in argv:
        tf = argv[argv.index("--transcript") + 1]
        title = ""
        if "--title" in argv:
            title = argv[argv.index("--title") + 1]
        with io.open(tf, "r", encoding="utf-8") as f:
            tr = f.read()
        r = distill(tr, {"title": title or os.path.basename(tf), "source": "CLI"})
        print(json.dumps({k: v for k, v in r.items() if k != "markdown"},
                         ensure_ascii=False, indent=2))
        return
    print(__doc__)


if __name__ == "__main__":
    main()
