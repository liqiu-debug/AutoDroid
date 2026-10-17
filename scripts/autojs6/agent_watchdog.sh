#!/usr/bin/env bash
# AutoDroid 设备端 Agent 看门狗（电脑侧运行）
#
# 作用：手机通过 USB/无线 adb 连接期间，发现 AutoJs6 里的 Agent 脚本未在运行
# 就自动重新拉起（Agent 被系统清理、误杀、脚本异常退出时自愈）。
#
# 用法：
#   ./agent_watchdog.sh [serial]
# 可选环境变量：
#   AGENT_PATH  手机上的 Agent 脚本路径（默认见下）
#   INTERVAL    检查间隔秒数（默认 60）
#   ADB_BIN     adb 可执行文件（默认 PATH 中的 adb）
#   LOGFILE     日志文件（默认 /tmp/autodroid_watchdog.log）
set -u

SERIAL="${1:-}"
AGENT_PATH="${AGENT_PATH:-/storage/emulated/0/脚本/autodroid/autodroid_agent.js}"
INTERVAL="${INTERVAL:-60}"
ADB_BIN="${ADB_BIN:-adb}"
LOGFILE="${LOGFILE:-/tmp/autodroid_watchdog.log}"

adb_cmd() {
    if [ -n "$SERIAL" ]; then "$ADB_BIN" -s "$SERIAL" "$@"; else "$ADB_BIN" "$@"; fi
}

log() { echo "[$(date '+%F %T')] $*" >> "$LOGFILE"; }

log "watchdog 启动（间隔 ${INTERVAL}s，设备 ${SERIAL:-默认}，脚本 ${AGENT_PATH}）"
while true; do
    state="$(adb_cmd get-state 2>/dev/null | tr -d '\r')"
    if [ "$state" != "device" ]; then
        sleep "$INTERVAL"
        continue
    fi
    if adb_cmd shell dumpsys activity services org.autojs.autojs6 2>/dev/null | grep -q "ScriptExecutionIntentService"; then
        sleep "$INTERVAL"
        continue
    fi
    log "Agent 未运行，唤醒屏幕并重新拉起"
    adb_cmd shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
    adb_cmd shell wm dismiss-keyguard >/dev/null 2>&1
    adb_cmd shell am start -n org.autojs.autojs6/org.autojs.autojs.external.shortcut.ShortcutActivity --es path "$AGENT_PATH" >> "$LOGFILE" 2>&1
    sleep "$INTERVAL"
done
