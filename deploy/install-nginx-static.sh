#!/usr/bin/env bash
# Public asset deployment only; existing TLS/API locations are preserved.
set -euo pipefail
cd "$(dirname "$0")/.."
SITE="";DOMAIN="image.myil.top";PUBLIC_ROOT="/var/www/photo-rescue-static";PORT="8000";BT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) SITE="${2:?missing site config}";shift 2;;
    --domain) DOMAIN="${2:?missing domain}";shift 2;;
    --public-root) PUBLIC_ROOT="${2:?missing public directory}";shift 2;;
    --port) PORT="${2:?missing port}";shift 2;;
    --bt) BT=1;shift;;
    *) echo 'Usage: install-nginx-static.sh --bt [--domain DOMAIN] or --site /absolute/site.conf [--public-root /var/www/photo-rescue-static] [--port 8000]' >&2;exit 2;;
  esac
done
if [[ "$BT" == 1 ]];then
  [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || { echo 'Invalid domain';exit 2; }
  [[ -n "$SITE" ]] || SITE="/www/server/panel/vhost/nginx/$DOMAIN.conf"
  [[ -f "$SITE" ]] || { echo "Site config not found: $SITE; use --site with the actual panel site file";exit 1; }
fi
NGINX="$(command -v nginx || true)"
if [[ -x /www/server/nginx/sbin/nginx ]];then NGINX="/www/server/nginx/sbin/nginx";fi
[[ -n "$NGINX" ]] || { echo 'nginx executable not found';exit 1; }
if [[ "$(id -u)" != 0 ]];then echo 'Run this deployment helper as root/sudo so the public export and site config are writable';exit 1;fi
PY="backend/.venv/bin/python";[[ -x "$PY" ]] || PY="python3"
EXTRA=()
if ! "$NGINX" -V 2>&1 | grep -- '--with-http_gzip_static_module' >/dev/null;then EXTRA+=(--no-gzip-static);fi
"$PY" backend/tools/build_web_assets.py --output "$PUBLIC_ROOT" --public-root "$PUBLIC_ROOT" --nginx-output "$PUBLIC_ROOT/nginx-web.conf" --port "$PORT" "${EXTRA[@]}"
# Keep fallback ASGI delivery current when the host has not enabled the include.
"$PY" backend/tools/build_web_assets.py
if [[ -z "$SITE" ]];then
  printf 'STATIC_READY\nAdd this inside the existing HTTPS server block:\ninclude %s/nginx-web.conf;\nThen run: nginx -t && nginx -s reload\n' "$PUBLIC_ROOT"
  exit 0
fi
"$PY" backend/tools/install_nginx_include.py --site "$SITE" --domain "$DOMAIN" --include "$PUBLIC_ROOT/nginx-web.conf" --nginx "$NGINX"
