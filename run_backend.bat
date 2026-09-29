@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0backend"

echo [Photo Rescue Backend]
echo.

REM 优先使用项目内的虚拟环境
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM 首次运行自动装依赖
if not exist ".venv\Scripts\python.exe" (
    if not exist "requirements.txt" goto :skipdeps
    echo [1/2] 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :novenv
    set "PY=.venv\Scripts\python.exe"
    echo [2/2] 安装依赖（首次较慢）...
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :pipfail
)
:skipdeps

if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo [提示] 未找到 .env，已从 .env.example 复制一份。
        echo        填入 FAL_KEY 后重新运行即可启用 AI 修复。
        copy /y ".env.example" ".env" >nul
    )
)

echo.
echo 服务地址: http://127.0.0.1:8000
echo 接口文档: http://127.0.0.1:8000/docs
echo 健康检查: http://127.0.0.1:8000/api/health
echo.
echo 按 Ctrl+C 停止服务。
echo.

"%PY%" -m uvicorn main:app --host 0.0.0.0 --port 8000
pause
exit /b 0

:novenv
echo.
echo [错误] 创建虚拟环境失败。请确认已安装 Python 3.9+ 并加入 PATH。
pause
exit /b 1

:pipfail
echo.
echo [错误] 依赖安装失败。请检查网络，或手动执行：
echo     cd backend
echo     python -m venv .venv
echo     .venv\Scripts\python -m pip install -r requirements.txt
pause
exit /b 1
