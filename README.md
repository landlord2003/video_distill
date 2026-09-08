# video_distill —— 微信视频号/抖音 下载 → 整理 → 知识库

把短视频（**抖音 / 抖音视频号 / 微信视频号直链 / 本地 mp4**）自动整理成 Markdown 笔记，写入 **Obsidian 知识库**。
整条链路**本地优先、零外部 API 费用**：

- 视频下载：`yt-dlp`（抖音）/ `ffmpeg`（直链、抓包所得 m3u8）
- 关键帧抽取：`ffmpeg`（由 `imageio-ffmpeg` 提供，无需系统安装）
- 画面理解 / 结构化总结：本机 **Ollama** `qwen3-vl:8b` + `qwen3:14b`
- 语音转写（可选）：本地 `faster-whisper`（装不上 / 模型拉不到时自动降级）

> 合并自 `crawl4ai-app`（网页采集器），复用其 Web UI / 历史库 / Ollama 抽取能力。

---

## 1. 这条流水线做了什么

```
你给链接 / 上传文件
        │
        ▼
[下载层] yt-dlp / ffmpeg 拿到 mp4
        │  → videos_inbox/<id>.mp4
        ▼
[抽帧层] ffmpeg 按时长均匀抽 10 张关键帧（约每 13 秒 1 张）
        │  → videos_frames/<时间戳>\*.jpg
        ▼
[理解层] 逐张发给 Ollama qwen3-vl:8b 做画面描述
        │  （⚠️ 单次只发 1 张，qwen3-vl 多图会 HTTP 400，已规避；
        │    描述会刻意捕捉画面字幕、配料、工具、数字）
        ▼
[总结层] 结合画面描述 + 转写，Ollama qwen3:14b 产出结构化笔记
        │  （含【制作流程】：按视频顺序逐步还原操作/用料/参数/要点）
        ▼
[落库层] 生成自包含 Markdown（关键帧以 base64 内嵌，任何平台可见图片）
           → Obsidian 库 <时间戳>-<id>.md（约 800KB，图片随文件走）
```

### 整理产出（每篇 Markdown 含）

- **标题 / 标签**（自动打 3–6 个主题标签）
- **摘要 / 关键结论**
- **制作流程** ⭐：若视频是制作方法/教程/工艺类，按视频出现顺序逐步还原
  每一步的**操作、用料/工具、关键参数（温度/时间/配比）、成败要点**；
  严格基于画面与转写，不编造，信息不足处如实标注
- **要点 / 章节**（按时间轴拆分）
- **关键帧**：10 张图，每张附一句话画面描述；**以 base64 内嵌进 Markdown**，
  笔记自包含——Obsidian、IMA、任何 Markdown 渲染器都能直接显示图片

### 实际样例（本机已跑通的一条抖音古法香皂视频）

- 入库笔记：`E:\Workbuddy\Claw\07-视频号整理\20260907-223440-7681253710374112538.md`
- 标题：**传统工艺制作香皂过程**；标签：`手工香皂` · `冷制皂` · `钙离子反应` …
- 自动还原出 **9 步完整制作流程**：准备牛油 → 高温煅烧 → 钙离子反应 →
  小火慢熬 → 持续加热 → 皂化反应（生成甘油和脂肪酸钾）→ 桂花香料融合 →
  脱模成型 → 干燥硬化——全部来自视觉模型对 10 帧画面及其字幕的识别

---

## 2. 功能 / 界面

| 标签页 | 作用 |
|---|---|
| 网页抓取 | 输入网址 → crawl4ai 抓正文 → Markdown（crawl4ai-app 原功能） |
| 🎬 视频号/抖音 | 抖音链接批量 / 本地视频上传 → 抽帧 + 视觉理解 + AI 总结 → 入库 |

视频标签页顶部有 **工具状态指示灯**（🟢yt-dlp 🟢ffmpeg 🟡转写），切到该页会自动探测依赖是否齐全。

---

## 3. 安装（Windows / Linux 通用）

```bash
git clone https://gitee.com/landlord2003/video_distill.git
cd video_distill

# 一键装环境
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium      # 仅用到「网页抓取」标签页时需要
```

安装本地模型（整理环节必需，需先装好 Ollama）：
```bash
ollama pull qwen3:14b
ollama pull qwen3-vl:8b
# 可选：语音转写（见第 8 节说明）
# pip install faster-whisper   # 装不上也不影响其它环节
```

---

## 4. 运行

```bash
# 方式一：后台启动（Windows 双击 launch.bat / start.bat）
launch.bat
# 方式二：前台运行看日志
venv\Scripts\python.exe app.py      # Windows
venv/bin/python app.py              # Linux/Mac
```

打开 http://127.0.0.1:8788 → 点「🎬 视频号/抖音」。

> 🔴 **改完代码必须彻底重启服务！** 反复出现 `FileNotFoundError: [WinError 2]` 通常是
> **旧 `app.py` 进程没退、仍占着 8788 端口在跑旧代码**。重启前先杀掉所有旧进程：
> ```bash
> # Windows：结束 8788 端口占用
> for /f "tokens=5" %a in ('netstat -ano ^| findstr :8788') do taskkill /PID %a /F
> # 再启动：
> launch.bat
> ```
> 重启后切到「🎬 视频号/抖音」标签页，看顶部 **工具状态指示灯** 确认加载的是新代码。

---

## 5. 怎么把视频弄到手（获取层）

| 平台 | 获取方式 | 说明 |
|---|---|---|
| **抖音 / 抖音视频号** | App/网页 → 视频「分享」→「复制链接」→ 粘贴到工具 | 支持 `v.douyin.com/xxx` 短链、直链 `douyin.com/video/<id>`、以及**搜索页/分享页链接（含 `modal_id`）自动改写** |
| **视频号直链** | 抓包（Charles/mitmproxy）拿 `.m3u8/.mp4` → 粘贴 | 需绕过微信 SSL Pinning，微信更新可能失效 |
| **本地文件** | 微信「保存到手机」/ 抖音「保存本地」→ 上传 | 最稳，立即可用，零 cookie 依赖 |

> ⚠️ **抖音反爬**：现多数视频需浏览器 cookie。工具按以下顺序**自动三级兜底**：
> 1. **`cookies.txt` 文件 + UA/Referer 指纹（最稳，推荐）**——见下方「导出 cookie」；
> 2. 浏览器实时 cookie：`--cookies-from-browser chrome / edge / chromium / brave`
>    （需该浏览器已登录抖音；且**Chrome 不能在下载时运行**，否则 cookie 库被占用而失败）；
> 3. **playwright 拦截直链（`douyin_auto.py`，终极兜底）**——yt-dlp 的抖音提取器要前端实时生成的
>    msToken（不写 cookie 库、导不出），静态 cookie 方案全失败时自动启用：playwright 无头打开
>    视频页（复用系统 Edge + 注入 cookies.txt），让抖音前端自己发 detail 请求，拦截响应拿
>    无水印直链再由 ffmpeg 下载。此路径还能拿到视频真实标题。
>
> 💡 **关键**：只要导出过一次 `cookies.txt`，三级链路全自动，无需人工干预；
> 若连 cookie 都没有，仍可走「App 保存本地 → 本地上传」（零 cookie 依赖）。

### 导出 cookie（一次性，最稳方案，支持 Edge / Chrome）

1. 用**已登录抖音**的 **Edge 或 Chrome** 打开抖音网页（`douyin.com`）；
2. 安装浏览器插件 **「Get cookies.txt LOCALLY」**（或「Cookie-Editor」导出 Netscape 格式）；
3. 停在 `douyin.com` 标签页 → 点插件 → **Export** → 选 **Netscape (cookies.txt)** 格式；
4. 文件命名为 `cookies.txt`，放到本项目根目录，或设置环境变量：
   ```bash
   set VIDEO_COOKIES=C:\path\to\cookies.txt    # Windows
   export VIDEO_COOKIES=/path/to/cookies.txt   # Linux/Mac
   ```
5. 重启服务后即可直接下载，无需浏览器实时在线。

> 🔴 **cookies.txt 含登录态，已被 .gitignore 忽略，切勿提交！**

> ⚠️ **关于「自动监控某视频号」**：抖音/视频号均无公开 feed/API，无法像 RSS 那样自动抓某号新视频；
> 真正自动化需逆向协议（灰产 + 封号风险），本项目不提供。你只需做「复制链接 / 存相册」动作，其余全自动。

---

## 6. 落库配置（入库位置 / 怎么看结果）

### 文件落到哪

| 类型 | 默认位置（本机） | 说明 |
|---|---|---|
| 下载的原始视频 | `E:\Workbuddy\crawl4ai\videos_inbox\<id>.mp4` | 一条约 18 MB |
| 抽出的全部关键帧 | `E:\Workbuddy\crawl4ai\videos_frames\<时间戳>\*.jpg` | 全量帧 + 压缩内嵌版 |
| **入库笔记（Markdown）** | `E:\Workbuddy\Claw\07-视频号整理\<时间戳>-<id>.md` | 你要看的主产物，**图片 base64 内嵌、自包含** |
| 高清关键帧备份 | `E:\Workbuddy\Claw\07-视频号整理\frames\<id>_NNN.jpg` | 原图备份（笔记本身不依赖它） |

> ⚠️ 注意区分两个位置：
> - `crawl4ai/videos_inbox/` 和 `crawl4ai/videos_frames/` 是**工作中间产物**（下载缓存 + 全量抽帧）。
> - `Claw/07-视频号整理/` 才是**真正进知识库的成品**。
>
> 💡 笔记里的关键帧以 **base64 内嵌**（压缩到 640px），不依赖本地路径——
> 所以把 `.md` 导入 **IMA** 或发给任何人，**图片都能直接显示**，不会再出现
> 「相对路径 `frames/xxx.jpg` 在别的平台打不开」的问题。

### 怎么看结果

1. **打开 Obsidian**，库指向 `E:\Workbuddy\Claw`（Vault B）。
2. 在文件树进 `07-视频号整理/`，点开任意 `*-<id>.md`。
3. 笔记含标题、标签、摘要、制作流程、要点、章节、10 张带描述的关键帧——阅读视图下图片直接渲染。
4. 想全局检索某主题（如「手工香皂」），用 Obsidian 搜索 / 标签筛选即可。
5. **导入 IMA**：直接把这个 `.md` 文件导入即可——图片 base64 内嵌在文件里，
   不依赖本地路径，IMA 里也能看到图。

### 更换落库目录（其它机器）

默认写入 `E:\Workbuddy\Claw\07-视频号整理`（本机主库）。其它机器用环境变量覆盖：
```bash
set VIDEO_VAULT_DIR=D:\your\vault\视频号整理    # Windows
export VIDEO_VAULT_DIR=/path/to/vault           # Linux/Mac
```
未设置且默认目录不存在时，自动回退到项目内 `distilled/` 目录，保证开箱即用。

---

## 7. 接口（便于二次开发 / 自动化）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/video` | body: `{"urls":[...], "write_vault":true}`，批量下载+整理 |
| POST | `/api/video/upload` | multipart 上传本地视频文件 |
| GET  | `/api/video/history` | 视频整理历史 |
| GET  | `/api/video/health` | 工具可用性自检（yt-dlp / ffmpeg / whisper / vault 状态） |

命令行直跑：
```bash
cd E:\Workbuddy\crawl4ai
venv\Scripts\python.exe video_downloader.py "https://www.douyin.com/video/xxxxx"
venv\Scripts\python.exe video_pipeline.py videos_inbox/xxxxx.mp4
```

---

## 8. 已知边界 & 排错

- **语音转写（faster-whisper）当前在本机环境不可用**：模型权重（`tiny` 的 `model.bin`，约 75 MB）
  首次需联网从 HuggingFace CDN 下载，而本机网络对该 CDN 返回空响应体，拉到的文件为 0 字节。
  → 已自动降级：即使无转写，笔记仍含「关键帧 + 视觉描述 + AI 总结」，完整可用。
  → **启用转写的两种办法**：① 在能正常访问 HF 的机器上把 `model.bin` 放到
  `~/.cache/huggingface/hub/models--Systran--faster-whisper-tiny/snapshots/<hash>/model.bin`；
  ② 改用 Ollama 的 `whisper` 系列模型替代（需改 `video_pipeline.py` 的转写实现）。
- **视觉理解必须逐张发图**：`qwen3-vl` 单次请求超过 3 张图会返回 HTTP 400。代码已改为逐张单图请求，
  若你换用其它视觉模型，可重新评估是否批量发送。
- **制作流程还原依赖帧密度 + 画面字幕**：无转写时，步骤还原来自 10 帧画面及其字幕文字
  （教程类视频通常烧有步骤字幕，识别率不错）；若视频画面信息少，步骤会标注「画面未明确展示」，
  不编造。开启语音转写后还原度会显著提升。
- **笔记体积**：base64 内嵌 10 张压缩帧后单篇约 800KB；若嫌大可调小
  `video_pipeline.py` 的 `N_KEYFRAMES` 或 `_embed_image` 的压缩参数。
- **微信视频号无公开接口**：不提供协议逆向 / 自动监控；获取以「复制链接 / 存相册 / 抓包直链」为准。
- **IMA 知识库直写待接入**：当前产出标准 `.md`，可手动导入 IMA「吴总知识库」或本地 Obsidian。
- **抖音 cookie 过期**：登录态一般撑数天到数周；某天再报 cookie 失败，重新导出 `cookies.txt` 覆盖即可。

---

## 9. 项目结构

```
app.py                # Web 服务（标准库 http.server，零额外依赖）
index.html            # 前端（网页抓取 + 视频号/抖音 两个标签页）
video_downloader.py   # 获取层：抖音三级兜底(yt-dlp+UA/Referer → 浏览器cookie → playwright直链) / 直链(ffmpeg) / 本地文件
douyin_auto.py        # playwright 拦截抖音 aweme/detail 拿无水印直链 + ffmpeg 下载（可导入复用，也可独立 CLI）
video_pipeline.py     # 整理流水线：抽帧→逐帧视觉理解→AI总结→Markdown→入库
wechat_dat.py         # 微信 .dat 缓存解密（图片类，可选工具）
vendor/               # 前端依赖（marked / dompurify）
docs/设计方案.md       # 原实施方案
```

---

## 10. 已验证环境

- Windows 11 + Python 3.13 venv + 本机 Ollama（`qwen3:14b` / `qwen3-vl:8b`）
- 实测通过：抖音搜索页链接（含 `modal_id`）→ 自动改写 → 下载 → 抽帧 → 视觉理解 → 总结 → 入库 Obsidian
- 实测样例：古法香皂教学视频自动产出 **9 步完整制作流程**（含皂化反应、香料融合等细节），
  10 帧关键帧 base64 内嵌，Obsidian / IMA 均可显示图片
- 当前状态：下载 ✅ / 抽帧 ✅ / 视觉理解 ✅ / 制作流程还原 ✅ / 总结 ✅ / 转写 ⚠️（网络限制降级）
