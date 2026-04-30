#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "[AutoDroid] 安装前端依赖..."
  (
    cd "$FRONTEND_DIR"
    npm install
  )
fi

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "[AutoDroid] 构建前端..."
  (
    cd "$FRONTEND_DIR"
    npm run build
  )
fi

LAN_IP=""
if command -v ip >/dev/null 2>&1; then
  LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
fi
if [[ -z "$LAN_IP" ]] && command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi
if [[ -z "$LAN_IP" ]] && command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  if [[ -z "$LAN_IP" ]]; then
    LAN_IP="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
fi

echo "[AutoDroid] 服务启动中..."
echo "[AutoDroid] 本机访问: http://127.0.0.1:${PORT}"
if [[ -n "$LAN_IP" ]]; then
  echo "[AutoDroid] 局域网访问: http://${LAN_IP}:${PORT}"
fi

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
