# video_distill（旧版）· 交接手册（HANDOVER）

> 交接日期：2026-10-24 ｜ 移交人：WorkBuddy 伙计（主空间）｜ Owner：老吴
> ⚠️ **先读这条：本目录是早期 v3.x 旧版，主力和后续开发都在 `E:\Workbuddy\crawl4ai`（v4.3+）。不要在本目录新增功能、不要再从本目录启动服务。**

## 0. 这是什么 / 为什么还留着

- 本目录曾是主力采集工作台（v3.3→v3.4），2026-10-24 确认已被 `crawl4ai`（v4.2 统一流水线，同源合并进化版）**替代**。判断依据：实际运行进程指向 crawl4ai 目录，且 crawl4ai 功能为本目录的超集。
- **2026-10-24 上午曾发生一次嫁接事故**：伙计把 cangjie 技能包蒸馏分支（P0/P1/P2）错嫁接到本目录，后来才确认主力是 crawl4ai。嫁接代码已**完整移植**到 crawl4ai（见其 HANDOVER §1.1），本目录的残留改动**保留未删、未提交**（无害）。

## 1. 本目录现状（仅供参考，勿在此开发）

| 文件 | 说明 |
|---|---|
| `app.py` / `video_pipeline.py` | v3.x 采集工作台（端口 8788——**与 crawl4ai 冲突，两者只能跑一个，通常谁都不该跑这个**） |
| `skill_distill.py` | 嫁接残留：视频转写→方法论三级判定→RIA-lite 能力卡→技能包；真身已移植 crawl4ai，本副本不再维护 |
| `index.html` | v3.4 UI（含已废弃的「⚡ 技能包蒸馏」开关） |

## 2. 如果非要用本目录（不推荐）

1. 先确认 crawl4ai 的 8788 没在跑（`curl --noproxy '*' http://127.0.0.1:8788/` 应不通）
2. **本项目没有自带 venv**——当时用受管 Python 启动：`C:/Users/Lenovo/.workbuddy/binaries/python/versions/3.13.12/python.exe app.py`
3. 启动前再问自己一遍：为什么不用 crawl4ai？

## 3. 正确去向

- 采集/蒸馏新需求 → `E:\Workbuddy\crawl4ai`（读其 HANDOVER.md）
- 技能包问答 → `E:\Workbuddy\LifeQA`（skillpacks 已入其知识库）
- 长文精读/蒸馏 → `E:\Workbuddy\DeepRead`（distill / persona 模式）

## 4. 处置建议（待老吴拍板，未执行）

- [ ] 本目录整体归档或删除（嫁接残留代码在 crawl4ai 有完整对应物，无独有内容）
- [ ] Gitee landlord2003/video_distill 仓状态与本地残留改动是否同步，需老吴决定
