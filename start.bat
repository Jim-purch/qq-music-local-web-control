@echo off
rem 办公室点唱机一键启动：首次运行自动创建虚拟环境并安装依赖
setlocal
set PYTHONUTF8=1
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 scripts\bootstrap.py
) else (
    python scripts\bootstrap.py
)
pause
