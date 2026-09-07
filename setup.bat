@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist venv (
  echo 创建虚拟环境...
  python -m venv venv
)
call venv\Scripts\activate
echo 安装依赖（国内镜像）...
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
echo 安装 Playwright Chromium（网页抓取标签页需要）...
python -m playwright install chromium
echo.
echo 安装完成！运行 launch.bat 启动服务（http://127.0.0.1:8788）
pause
