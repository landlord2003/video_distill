# crawl4ai 工具台 · 交接手册（HANDOVER）

> 交接日期：2026-10-24 ｜ 移交人：WorkBuddy 伙计（主空间）｜ Owner：老吴
> **这是多平台采集蒸馏的主力工具台（v4.3）**。目录名叫 `crawl4ai` 但它早已不只是网页采集器——README 标题就是「video_distill —— 多平台内容采集 → 蒸馏 → 知识库（+ VOA 英语学习）」。
> ⚠️ **血泪教训（2026-10-24）**：`E:\Workbuddy\video_distill` 是早期 v3.x 旧版（已停用），别被"video_distill"这个名字骗了——**改代码前先确认运行中进程指向哪个目录**（`Get-NetTCPConnection -LocalPort 8788` → PID → 进程命令行），不靠目录名/README 推断。当天曾因搞反两个目录，错杀老吴正在跑的服务并改错项目，已补救。

## 0. Owner 偏好（必须遵守）

- 中文，直接给结论，不废话；无人值守：不问、用默认并显式声明、不重复已完成
- **汇报前必须读源码/实测核验**；诚实上报疏忽与风险；改界面必须实测
- 本地优先，零外部付费 API；安全 Git——data/、venv/、运行时产物不入仓
- 对失败样本显式标注原因，避免后台重试死循环

## 1. 项目现状（v4.3，2026-10-24）

**统一流水线**：抖音 / 微信视频号 / YouTube / Twitter·X / 公众号 / 小红书 / B站 / VOA英语 / 通用网页 / 直链 / 本地文件 → 下载 → 转写/解析 → 蒸馏 → Markdown 落 Obsidian 库（`E:\Workbuddy\Claw\07-视频号整理` 等）。全链本机 Ollama（qwen3-vl:8b 画面理解 / qwen3:14b 总结），零外部 API。

| 文件 | 职责 |
|---|---|
| `app.py`（2087 行） | Web 服务（纯标准库 ThreadingHTTPServer，端口 **8788**）+ 全部路由：网页抓取/视频管线/文章管线/抖音主页/音乐/VOA |
| `video_pipeline.py`（1166 行） | 视频核心管线：下载→whisper 转写→关键帧理解→map-reduce 结构化总结→Markdown |
| `skill_distill.py`（**2026-10-24 新增，嫁接 cangjie-skill**） | 技能包蒸馏分支，见下节 |
| `article_ingest.py / twitter_ingest.py / voa_ingest.py / content_analyze.py` | 文章/推特长文/VOA/要点解析 |
| `douyin_profile.py / douyin_auto.py` | 抖音主页批量采集 |
| `venv/` | 自带虚拟环境（**启动必须用它**：`venv\Scripts\python.exe app.py`） |

### 1.1 技能包蒸馏分支（v4.3 新增，嫁接 cangjie-skill P0/P1/P2）

- **触发**：视频类来源选项区勾「⚡ 技能包蒸馏」（`skill: true`）；本地文件上传走 `?skill=1` query 参数
- **管线**（`skill_distill.py`）：转写全文 → **方法论三级判定**（关键词密度打分 ≥8 直通 / ≤3 跳过 / 灰区一次 LLM 闸门）→ **RIA-lite 能力卡提取**（R 触发场景 / I 操作步骤 / A 原片案例 / E 验收标准 / B 常见错误 + 来源引文 ≤150 字；长转写 map-reduce 复用 `video_pipeline` 的 `_split_transcript`/`_ollama_generate`）→ `<slug>_技能包.md`
- **双库落盘**：Obsidian 库 + 自动同步 `E:\Workbuddy\LifeQA\knowledge\skillpacks\`（LifeQA 的 ingest.py 递归扫描 knowledge/，增量入库后即可 RAG 问答）
- **CLI**（在 crawl4ai 目录用 venv 跑）：
  - `python skill_distill.py --transcript x.txt --title 标题` 单篇蒸馏
  - `python skill_distill.py --backfill <笔记文件或目录>` 存量回填（解析笔记里「## 语音转写全文」或双语表的中文列，≥500 字才处理）
  - `python skill_distill.py --evaluate <技能包.md> [--out eval.json]` 按验收标准出题→模型执行→rubric 1~5 打分 → darwin 兼容 eval JSON
- **实测记录**（2026-10-24）：合成教程转写全流程通过；存量库 284 篇中 190 篇可回填抽取；资讯类（无人机新规，打分 0）正确跳过；灰区样本（打分 6→LLM 闸门判方法论）回填出「追溯逻辑原点」卡；`--evaluate` 49s 完成 5/5
- **诚实边界**：本机自评是"运动员兼裁判"，有乐观偏差；正式进化把 eval JSON 交给 darwin-skill 盲测

## 2. 服务与启动

- 启动：`cd E:\Workbuddy\crawl4ai && venv\Scripts\python.exe app.py`（或 `launch.bat`：端口占用则只开浏览器，否则后台启动；`start.bat` 前台跑）
- **防双实例守卫**：启动时探测 8788，已有实例直接 abort——看到 `[abort] 端口 8788 已有实例` 不是故障
- 健康检查：`curl --noproxy '*' http://127.0.0.1:8788/`（**必须 --noproxy**，见下）
- 前端 `index.html`（版本号 v4.3）：统一流水线 UI，按来源类型动态渲染选项区

## 3. 环境坑（勿重踩）

| 坑 | 解法 |
|---|---|
| **Windows 注册表死代理**（127.0.0.1:64309）劫持本机请求 | curl 一律 `--noproxy '*'`；app.py 启动时已自动清代理环境变量 |
| **Chromium 动态加载页只抓到壳** | CrawlerRunConfig 已加 `wait_until="networkidle"` + `delay_before_return_html=2.0`（2026-10-08 修，实测 3,483→137 万字符）——这是通用能力，别删 |
| **git-bash 后台命令 cd 前缀可能失效** | 长任务用 `env -C <绝对路径> <命令>` 指定工作目录；重定向文件用绝对路径（shell 重定向发生在 shell 自己的 cwd） |
| whisper 模型本地缓存 | `whisper_models/` 本地目录，ModelScope 镜像下载过；缺了会自动回退 tiny |
| qwen3-vl 多图 HTTP 400 | 逐张发，别批量 |
| **Ollama 串行** | 视频管线全程持 `_video_lock` 串行；12GB VRAM 跑不动并发 |
| 端口 8788 被占 | 只有一个真身（本目录）。若怀疑占端口的是旧版，先查进程命令行再动手 |

## 4. 与其他系统的关系

- **LifeQA**（`E:\Workbuddy\LifeQA`）：技能包同步目标，`ingest.py` 增量入库即可问答
- **DeepRead**（`E:\Workbuddy\DeepRead`，端口 8790）：同源兄弟项目（本工具台早期由此分叉方向相反的两个），文章要点解析与其 quick 模式思想同源；两服务可同时跑（端口不同）
- **darwin-skill**：`--evaluate` 产物 eval JSON 兼容其评测格式，可直接喂给 darwin 做自动进化
- **Gitee**：历史仓 landlord2003/crawl4ai-app / video_distill；**v4.3 改动尚未提交**（skill_distill.py + app.py/video_pipeline.py/index.html 四处改动在工作区）

## 5. 验收标准（改完任何东西）

1. `--noproxy '*'` curl 首页 200，版本号字符串正确
2. `venv\Scripts\python.exe -m py_compile app.py video_pipeline.py skill_distill.py` 通过
3. 勾「⚡ 技能包蒸馏」提交一条教程类视频链接（或 `--transcript` 喂合成文本），确认：判定理由出现在输出、技能包落 Obsidian + LifeQA 双库
4. 资讯类内容确认被跳过（不烧 LLM 算力）
5. 汇报附实测数据，不猜

## 6. 待办

- [ ] v4.3 改动 git 化（先备份 data/ 与 venv/，仅提交源码）
- [ ] article 管线（公众号/网页长文）接同一 skill_distill 分支（方法论长文同样适用，P1.5）
- [ ] 全量存量回填收益有限（库内容以哲学/资讯叙事为主，方法论占比低），等新采集自然积累
