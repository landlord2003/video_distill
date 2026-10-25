# -*- coding: utf-8 -*-
"""文章要点解析回填：给历史入库但缺「📌 要点解析」段的笔记补跑第三步解析。

- 复用 content_analyze.analyze_and_decorate（qwen3:14b，零外部 API）
- 原地写回 vault_path 对应文件（不新建文件、不改标题）
- 同步更新 articles.md_len，记录中心详情实时读文件即刻生效
- 解析失败自动重试一次（Ollama 冷加载窗口期容错）
用法：
    python backfill_article_analysis.py            # 自动扫描缺解析的文章类记录
    python backfill_article_analysis.py <id> ...   # 指定记录 id
"""
import io
import os
import sys
import time

import content_analyze as ca
import video_pipeline as vp

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawls.db")


def _load_db():
    import sqlite3
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def find_missing(con):
    """扫描 articles 表中缺「要点解析」段的记录（读文件判断）。"""
    rows = con.execute(
        "SELECT id, title, vault_path FROM articles "
        "WHERE status='done' AND vault_path!='' ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        p = r["vault_path"]
        if not p or not os.path.isfile(p):
            continue
        try:
            with io.open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        if "📌 要点解析" not in text and len(text) >= ca.MIN_TEXT_CHARS:
            # 排除纯媒体类笔记（无正文标题结构也可解析，交给长度阈值）
            out.append({"id": r["id"], "title": r["title"], "path": p,
                        "chars": len(text)})
    return out


def backfill_one(rec):
    pid, path = rec["id"], rec["path"]
    with io.open(path, "r", encoding="utf-8") as f:
        md = f.read()
    if "📌 要点解析" in md:
        return {"id": pid, "ok": True, "skipped": "已含要点解析"}
    res, new_md = {}, md
    for attempt in (1, 2):  # 冷加载容错：失败重试一次
        dec = ca.analyze_and_decorate(md)
        res = dec["analyze"]
        if res.get("ok"):
            new_md = dec["md"]
            break
        print(f"  [!] {pid} 第{attempt}次解析失败: {res.get('error','')[:120]}，"
              + ("重试中..." if attempt == 1 else "放弃"))
        if attempt == 1:
            time.sleep(5)
    if not res.get("ok"):
        return {"id": pid, "ok": False, "error": res.get("error", "解析失败")}
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(new_md)
    # 同步 DB md_len，记录中心列表展示保持一致
    con = _load_db()
    with con:
        con.execute("UPDATE articles SET md_len=? WHERE id=?",
                    (len(new_md), pid))
    con.close()
    return {"id": pid, "ok": True, "summary": res.get("summary", "")[:60],
            "points": len(res.get("points") or []),
            "tags": res.get("tags") or [], "md_len": len(new_md)}


def main():
    ids = set(a for a in sys.argv[1:] if not a.startswith("-"))
    con = _load_db()
    targets = []
    if ids:
        for pid in ids:
            r = con.execute("SELECT id, title, vault_path FROM articles WHERE id=?",
                            (pid,)).fetchone()
            if r and r["vault_path"] and os.path.isfile(r["vault_path"]):
                targets.append({"id": r["id"], "title": r["title"],
                                "path": r["vault_path"],
                                "chars": 0})
            else:
                print(f"[x] 记录不存在或缺文件: {pid}")
    else:
        targets = find_missing(con)
    con.close()
    print(f"[i] 待回填 {len(targets)} 条")
    ok_n = fail_n = 0
    for rec in targets:
        print(f"[->] {rec['id']} {rec['title'][:40]}")
        try:
            r = backfill_one(rec)
        except Exception as e:
            r = {"id": rec["id"], "ok": False, "error": str(e)[:200]}
        if r.get("ok"):
            ok_n += 1
            if r.get("skipped"):
                print(f"    跳过：{r['skipped']}")
            else:
                print(f"    ✔ 要点{r.get('points')}条 标签{r.get('tags')} "
                      f"md_len={r.get('md_len')}")
        else:
            fail_n += 1
            print(f"    [x] {r.get('error','')[:160]}")
    print(f"[i] 完成：成功 {ok_n}，失败 {fail_n}")


if __name__ == "__main__":
    main()
