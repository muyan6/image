@echo off
setlocal
title 废片拯救所 · 停止后端

REM 停止后端: 先结束守护窗口(否则 3 秒后会自动拉起), 再按端口结束进程
REM 用法: 双击运行; 或命令行带端口参数: stop_backend.bat 8000

set "PORT=%~1"
if not defined PORT set "PORT=8000"

echo.
echo  [1/2] 结束守护窗口 (run_backend.bat 的自动重启循环) ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -match 'run_backend.bat' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo  [2/2] 结束端口 %PORT% 上的后端进程 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /T /F >nul 2>&1
)

ping -n 3 127.0.0.1 >nul
netstat -ano | findstr /C:":%PORT% " | findstr "LISTENING" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [OK] 后端已停止, 端口 %PORT% 已释放。
) else (
    echo.
    echo  [警告] 端口 %PORT% 仍有进程监听, 请手动检查: netstat -ano ^| findstr "%PORT%"
)

echo.
pause
