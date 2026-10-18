@echo off
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat in the OEM codepage
REM (GBK on zh-CN), so UTF-8 Chinese comments get garbled and break line 1.
cd /d %~dp0
REM If port 8788 already serves, just open the browser; else start it hidden.
powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8788 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  REM windowless starter (pythonw + hidden cmd): immune to window-CLOSE kill
  start "" wscript.exe bg_start.vbs
  timeout /t 5 /nobreak >nul
)
start http://127.0.0.1:8788
