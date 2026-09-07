#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "创建虚拟环境..."
  python3 -m venv venv
fi
source venv/bin/activate
echo "安装依赖..."
pip install -r requirements.txt
echo "安装 Playwright Chromium（网页抓取标签页需要）..."
python -m playwright install chromium
echo
echo "安装完成！运行: venv/bin/python app.py  (http://127.0.0.1:8788)"
