#!/usr/bin/env bash
# ===================================================================== #
#  一次性部署脚本（云服务器上跑一次就够）
#
#  做 5 件事：
#    1. 检查 python3 / venv，创建 backend/.venv 并装依赖（自动补 libGL）
#    2. 生成并安装 systemd 服务（开机自启 + 崩溃 3 秒自动拉起）
#    3. 启动服务并做健康检查
#
#  用法（推荐 root 或有 sudo 的用户执行）:
#      bash deploy/install-service.sh [端口]      # 默认 8000
#
#  之后日常更新只需要在项目根目录:
#      ./.update
#
#  日志查看:  journalctl -u photo-rescue -f
# ===================================================================== #
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8000}"
SERVICE_NAME="photo-rescue"
SUDO=""
[ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
RUN_USER="${SUDO_USER:-$(id -un)}"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[失败]\033[0m %s\n' "$*"; exit 1; }

command -v systemctl >/dev/null 2>&1 || die "本机没有 systemd，请直接用 start.sh/stop.sh 方式运行"
[ -d .git ] || die "请在 git clone 出来的项目目录里运行"

# ---- 1. 环境 ----
step "1/4 准备虚拟环境与依赖"
if [ ! -x backend/.venv/bin/python ]; then
    python3 -m venv backend/.venv || die "创建 venv 失败：Debian/Ubuntu 需要 apt install python3-venv python3-pip"
fi
PY="backend/.venv/bin/python"
"$PY" -m pip install -q --disable-pip-version-check -r backend/requirements.txt \
    -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    || "$PY" -m pip install -q --disable-pip-version-check -r backend/requirements.txt

if ! "$PY" -c "import cv2" >/dev/null 2>&1; then
    echo "补装 OpenCV 系统依赖（libGL）..."
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get install -y -q libgl1 libglib2.0-0 || true
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y -q mesa-libGL glib2 || true
    elif command -v yum >/dev/null 2>&1; then
        $SUDO yum install -y -q mesa-libGL glib2 || true
    fi
    "$PY" -c "import cv2" >/dev/null 2>&1 \
        || die "cv2 仍不可用：可把 requirements.txt 的 opencv-python 换成 opencv-python-headless 后重跑"
fi
ok "依赖就绪"

# ---- 2. 生成 systemd 单元 ----
step "2/4 写入 systemd 服务（${SERVICE_NAME}）"
APP_DIR="$(pwd)"
UNIT="$SUDO tee /etc/systemd/system/${SERVICE_NAME}.service >/dev/null"
$UNIT <<EOF
[Unit]
Description=Photo Rescue Backend (废片拯救所)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}/backend
ExecStart=${APP_DIR}/backend/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
ok "单元文件已写入 /etc/systemd/system/${SERVICE_NAME}.service"

# ---- 3. 启动并设开机自启 ----
step "3/4 启动服务并设为开机自启"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME" >/dev/null
$SUDO systemctl restart "$SERVICE_NAME"
ok "已启动并加入开机自启"

# ---- 4. 健康检查 ----
step "4/4 健康检查"
for _ in $(seq 1 15); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        echo ""
        echo "============================================="
        echo " 部署完成"
        echo "   服务地址 : http://127.0.0.1:${PORT}"
        echo "   管理后台 : http://127.0.0.1:${PORT}/admin"
        echo "   看日志   : journalctl -u ${SERVICE_NAME} -f"
        echo "   日常更新 : 项目根目录执行 ./.update"
        echo "============================================="
        exit 0
    fi
    sleep 2
done

$SUDO journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
die "服务未就绪，按上面日志排查"
