#!/usr/bin/env bash
set -euo pipefail
PY="${1:-python3}"
BACKEND="$(cd "$(dirname "$0")/../backend" && pwd)"
check_font() {
  PYTHONPATH="$BACKEND${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c \
    'from text_overlay import _font; assert bytes(_font(32).getmask("甲")) != bytes(_font(32).getmask("乙"))' \
    >/dev/null 2>&1
}
if check_font; then exit 0; fi
SUDO=()
if [ "$(id -u)" -ne 0 ]; then SUDO=(sudo); fi
if command -v apt-get >/dev/null 2>&1; then
  "${SUDO[@]}" apt-get update -q
  "${SUDO[@]}" apt-get install -y -q fonts-noto-cjk
elif command -v dnf >/dev/null 2>&1; then
  "${SUDO[@]}" dnf install -y -q google-noto-sans-cjk-fonts
elif command -v yum >/dev/null 2>&1; then
  "${SUDO[@]}" yum install -y -q google-noto-sans-cjk-fonts
fi
check_font || { echo '请配置 TEXT_FONT_PATH 为可读取的中文字体文件' >&2; exit 1; }
