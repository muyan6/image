#!/usr/bin/env bash
# ===================================================================== #
#  启动后端（无 systemd 时的兜底方式；装了 systemd 建议用
#  deploy/install-service.sh，开机自启 + 崩溃自动拉起都由它管）
#
#  用法:  bash start.sh [端口]     默认 8000
# ===================================================================== #
set -euo pipefail
cd "$(dirname "$0")/backend"

PORT="${1:-8000}"
HOST="${HOST:-0.0.0.0}"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/backend_$(date +%Y%m%d).log"

# 已在跑就不重复起
if curl -sf -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "[OK] 后端已在运行（端口 ${PORT}），无需重复启动"
    exit 0
fi

if [ ! -x .venv/bin/python ]; then
    echo "[初始化] 创建虚拟环境 ..."
    python3 -m venv .venv
    .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
        -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
        || .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
fi

echo "启动后端（端口 ${PORT}，日志: backend/${LOG_FILE}）..."
bash ../deploy/ensure-fonts.sh "$PWD/.venv/bin/python"
nohup .venv/bin/python -m uvicorn main:app --host "$HOST" --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &
echo $! > backend.pid
echo "PID: $(cat backend.pid)"

# 等待就绪
for _ in $(seq 1 10); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo "[OK] 服务健康: http://127.0.0.1:${PORT}"
        exit 0
    fi
    sleep 2
done

echo "[失败] 20 秒内未就绪，最近日志："
tail -n 20 "$LOG_FILE"
exit 1
