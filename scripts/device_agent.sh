#!/usr/bin/env bash
# AutoDroid 设备接入助手启动器（macOS/Linux）
# 首次使用：编辑本文件，把 SERVER 和 TOKEN 换成你的平台地址与 API Token
set -euo pipefail

SERVER="http://192.168.1.10:8000"
TOKEN="adk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
NAME="$(hostname -s 2>/dev/null || hostname)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] 未找到 python3，请先安装 Python 3.8+"
  exit 1
fi

DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/device_agent.py" --server "$SERVER" --token "$TOKEN" --name "$NAME"
