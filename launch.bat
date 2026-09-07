@echo off
cd /d %~dp0
REM 端口已占用(服务在跑)则直接开浏览器；否则后台启动后再开
powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8788 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  start "" /min cmd /c "venv\Scripts\python.exe app.py"
  timeout /t 3 /nobreak >nul
)
start http://127.0.0.1:8788
