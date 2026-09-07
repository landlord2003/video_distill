# video_distill —— 微信视频号/抖音 下载 → 整理 → 知识库

把短视频（**抖音 / 抖音视频号 / 微信视频号直链 / 本地 mp4**）自动整理成 Markdown 笔记，
写入 Obsidian 知识库。整条链路**本地优先、零外部 API 费用**：

- 视频下载：`yt-dlp`（抖音）/ `ffmpeg`（直链、抓包所得 m3u8）
- 关键帧抽取：`ffmpeg`（imageio-ffmpeg 提供，无需系统安装）
- 画面理解 / 结构化总结：本机 **Ollama** `qwen3-vl:8b` + `qwen3:14b`
- 语音转写（可选）：本地 `faster-whisper`（装不上自动降级）

> 合并自 `crawl4ai-app`（网页采集器），复用其 Web UI / 历史库 / Ollama 抽取能力。

---

## 1. 功能

| 标签页 | 作用 |
|---|---|
| 网页抓取 | 输入网址 → crawl4ai 抓正文 → Markdown（crawl4ai-app 原功能） |
| 🎬 视频号/抖音 | 抖音链接批量 / 本地视频上传 → 抽帧 + 视觉理解 + AI 总结 → 入库 |

视频整理产出（Markdown）：**标题 / 标签 / 摘要 / 要点 / 章节 / 关键帧（含画面描述）/ 语音转写全文**。

## 2. 安装（Windows / Linux 通用）

```bash
git clone https://gitee.com/landlord2003/video_distill.git
cd video_distill

# 一键装环境（Windows 双击 setup.bat；Linux/Mac 执行 setup.sh）
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium      # 仅用到「网页抓取」标签页时需要
```

安装本地模型（整理环节需要）：
```bash
ollama pull qwen3:14b
ollama pull qwen3-vl:8b
# 可选：语音转写
# pip install faster-whisper   # 装不上也不影响其它环节
```

## 3. 运行

```bash
# 方式一：后台启动并自动开浏览器（Windows）
launch.bat
# 方式二：前台运行（看日志）
start.bat          # Windows
# 或
venv/bin/python app.py   # Linux/Mac
```

打开 http://127.0.0.1:8788 → 点「🎬 视频号/抖音」。

## 4. 怎么把视频弄到手（获取层）

| 平台 | 获取方式 | 说明 |
|---|---|---|
| **抖音 / 抖音视频号** | App/网页 → 视频「分享」→「复制链接」→ 粘贴到工具 | 支持 `v.douyin.com/xxx` 短链、视频直链 `douyin.com/video/<id>`、以及**搜索页/分享页链接（含 `modal_id`）自动改写** |
| **视频号直链** | 抓包（Charles/mitmproxy）拿 `.m3u8/.mp4` → 粘贴 | 需你绕过微信 SSL Pinning，微信一更新可能失效 |
| **本地文件** | 微信「保存到手机」/ 抖音「保存本地」→ 上传 | 最稳，立即可用 |

> ⚠️ **抖音反爬**：现多数视频需浏览器 cookie。工具会自动依次尝试
> `--cookies-from-browser chrome / edge / chromium`，**请在你已登录抖音的浏览器环境下运行本服务**。
> 仍失败时可导出 `cookies.txt` 后改用直链下载。

> ⚠️ **关于「自动监控某视频号」**：抖音/视频号均无公开 feed/API，无法像 RSS 那样自动抓某号新视频；
> 真正自动化需逆向协议（灰产+封号），本项目不提供。你只需做「复制链接 / 存相册」动作，其余全自动。

## 5. 落库配置

默认写入 `E:\Workbuddy\Claw\07-视频号整理`（本机主库）。其它机器用环境变量覆盖：

```bash
set VIDEO_VAULT_DIR=D:\your\vault\视频号整理    # Windows
export VIDEO_VAULT_DIR=/path/to/vault           # Linux/Mac
```

未设置且默认目录不存在时，自动回退到项目内 `distilled/` 目录，保证开箱即用。

## 6. 接口（便于二次开发 / 自动化）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/video` | body: `{"urls":[...], "write_vault":true}`，批量下载+整理 |
| POST | `/api/video/upload` | multipart 上传本地视频文件 |
| GET  | `/api/video/history` | 视频整理历史 |

## 7. 项目结构

```
app.py                # Web 服务（标准库 http.server，零额外依赖）
index.html            # 前端（网页抓取 + 视频号/抖音 两个标签页）
video_downloader.py   # 获取层：抖音(yt-dlp) / 直链(ffmpeg) / 本地文件
video_pipeline.py     # 整理流水线：抽帧→视觉理解→AI总结→Markdown→入库
wechat_dat.py         # 微信 .dat 缓存解密（图片类，可选工具）
vendor/               # 前端依赖（marked / dompurify）
docs/设计方案.md       # 原实施方案
```

## 8. 已知边界

- 语音转写依赖 `faster-whisper` 模型权重（首次需联网下载）；拉取失败自动降级。
- 微信视频号无公开接口，**不提供协议逆向/自动监控**；获取以「复制链接 / 存相册 / 抓包直链」为准。
- IMA 知识库直写待接入 IMA MCP 连接器；当前产出标准 `.md`，可手动导入 IMA。
