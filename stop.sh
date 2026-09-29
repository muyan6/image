#!/usr/bin/env bash
# ===================================================================== #
#  停止后端
#  用法:  bash stop.sh [端口]     默认 8000
# ===================================================================== #
set -uo pipefail
cd "$(dirname "$0")/backend"

PORT="${1:-8000}"

# 1) PID 文件优先
if [ -f backend.pid ]; then
    PID="$(cat backend.pid)"
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID" && echo "[OK] 已停止 PID $PID"
    fi
    rm -f backend.pid
fi

# 2) 兜底：按端口找监听进程（防止 PID 文件丢失）
sleep 1
if command -v ss >/dev/null 2>&1; then
    PIDS="$(ss -ltnp 2>/dev/null | grep ":${PORT} " | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u || true)"
elif command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -ti ":${PORT}" 2>/dev/null | sort -u || true)"
else
    PIDS=""
fi

if [ -n "$PIDS" ]; then
    for p in $PIDS; do
        kill "$p" 2>/dev/null && echo "[OK] 已停止端口 ${PORT} 上的进程 $p"
    done
fi

sleep 1
if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${PORT} " && echo "[警告] 端口 ${PORT} 仍被占用，请手动检查: ss -ltnp | grep ${PORT}" \
        || echo "[OK] 端口 ${PORT} 已释放"
else
    echo "[OK] 停止命令已执行（本机无 ss/lsof，未能复核端口状态）"
fi
