@echo off
setlocal enabledelayedexpansion
title 废片拯救所 · 后端服务
cd /d "%~dp0backend"

REM =================================================================== #
REM  一键启动后端（服务器上双击本文件即可）
REM
REM  自动完成：创建虚拟环境 -> 装依赖 -> 生成 .env -> 起服务
REM  内置守护：进程崩溃 3 秒后自动拉起；连续快速崩溃 5 次才放弃
REM  日志：控制台实时显示，同时追加到 logs\backend_日期.log
REM
REM  停止服务：运行同目录的 stop_backend.bat（或在本窗口按 Ctrl+C 后按 Y）
REM  注意：本服务状态在进程内存里，永远不要给 uvicorn 加 --workers 多进程！
REM =================================================================== #

REM ---------- 可改配置 ----------
set "HOST=0.0.0.0"
set "PORT=8000"
REM ----------------------------

echo.
echo  ============================================
echo   废片拯救所 - 后端服务 (Photo Rescue)
echo  ============================================
echo.

REM ---------- 1. 找 Python（优先项目内虚拟环境） ----------
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    py -3 --version >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo  [错误] 没找到 Python。请安装 Python 3.10+ 并勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

REM ---------- 2. 首次运行：建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo  [初始化 1/2] 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo  [错误] 创建虚拟环境失败，请确认 Python 3.10+ 可用。
        pause
        exit /b 1
    )
    set "PY=.venv\Scripts\python.exe"
) else (
    echo  [OK] 虚拟环境已就绪
)

REM ---------- 3. 依赖缺失时自动安装（换源优先，失败回退官方源） ----------
.venv\Scripts\python.exe -c "import uvicorn, fastapi, cv2, PIL, requests, multipart" >nul 2>nul
if errorlevel 1 (
    echo  [初始化 2/2] 安装依赖（首次约 1~3 分钟，走国内镜像）...
    .venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 .venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
    .venv\Scripts\python.exe -c "import uvicorn, fastapi, cv2, PIL, requests, multipart" >nul 2>nul
    if errorlevel 1 (
        echo  [错误] 依赖安装失败。检查网络后重跑本脚本，或手动执行：
        echo      cd backend ^&^& .venv\Scripts\python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
) else (
    echo  [OK] 依赖已就绪
)

REM ---------- 4. .env 引导（首启密码会打印在日志里） ----------
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo  [OK] 已生成 .env（管理后台首启密码会打印在下方日志中）
    )
)

REM ---------- 5. 日志目录与文件名 ----------
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "DAYSTAMP=%%i"
set "LOGFILE=logs\backend_!DAYSTAMP!.log"

REM ---------- 6. 端口占用检查 ----------
netstat -ano | findstr /C:":%PORT% " | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo  [警告] 端口 %PORT% 已被占用 —— 后端可能已经在运行。
    echo         如需重启：先运行 stop_backend.bat，再运行本脚本。
    echo.
    pause
    exit /b 1
)

echo.
echo  服务地址   : http://127.0.0.1:%PORT%
echo  管理后台   : http://127.0.0.1:%PORT%/admin
echo  接口文档   : http://127.0.0.1:%PORT%/docs
echo  健康检查   : http://127.0.0.1:%PORT%/api/health
echo  日志文件   : backend\%LOGFILE%
echo  停止方式   : 运行 stop_backend.bat，或本窗口 Ctrl+C 后按 Y
echo.
echo  ---------------------------------------------------------------
echo.

REM ---------- 7. 守护循环：崩溃自动拉起 ----------
set /a CRASHES=0

:loop
for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "T0=%%i"

REM 控制台实时显示 + 以 UTF-8 追加写入日志（grep/tail 可直接用）
"%PY%" -m uvicorn main:app --host %HOST% --port %PORT% 2>&1 | "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$input | ForEach-Object { Add-Content -Path '%LOGFILE%' -Value $_ -Encoding UTF8; $_ }"

for /f %%i in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "T1=%%i"
set /a ELAPSED=T1-T0

if !ELAPSED! GEQ 60 set /a CRASHES=0
set /a CRASHES+=1

if !CRASHES! GEQ 5 (
    echo.
    echo  [放弃] 服务连续快速崩溃 !CRASHES! 次，停止拉起。
    echo         请查看 %LOGFILE% 末尾的错误信息。
    echo.
    pause
    exit /b 1
)

echo  [%date% %time%] 后端进程退出（已运行 !ELAPSED! 秒），3 秒后自动重启（连续第 !CRASHES! 次）...
powershell -NoProfile -Command "Add-Content -Path '%LOGFILE%' -Value ('backend exited after !ELAPSED!s, restarting (attempt !CRASHES!/5) at ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding UTF8"
ping -n 4 127.0.0.1 >nul
goto loop
