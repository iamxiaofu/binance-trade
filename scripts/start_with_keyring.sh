#!/usr/bin/env bash
# binance-trade 的 systemd 启动 wrapper：
# 1) 确保主进程有 DBUS_SESSION_BUS_ADDRESS（user session bus 在 /run/user/0/bus）。
#    我们跑在 system instance systemd 下，不会自动注入这个变量，但 user
#    session bus socket 默认存在（dbus-broker user scope）。
# 2) 启动 gnome-keyring-daemon --login（unattended server 场景传空密码），
#    把 GNOME_KEYRING_CONTROL 等导出给主进程。
# 3) exec 主进程，继承所有环境。
set -euo pipefail
LOG_PREFIX="[start_with_keyring]"
cleanup() {
    [[ -n "${KEYRING_PID:-}" ]] && kill -0 "$KEYRING_PID" 2>/dev/null && kill "$KEYRING_PID" 2>/dev/null || true
}
trap cleanup EXIT
# 没 dbus-daemon / dbus-broker 就降级跳过
if ! command -v dbus-daemon >/dev/null 2>&1 && ! command -v dbus-broker >/dev/null 2>&1; then
    echo "$LOG_PREFIX no dbus binary, fallback to no-keyring" >&2
    exec "$@"
fi
# 选 bus：systemd 注入了就用，否则用默认 user session socket
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    if [[ -S /run/user/0/bus ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/0/bus"
    elif [[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus" ]]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus"
    else
        echo "$LOG_PREFIX no session bus socket, fallback to no-keyring" >&2
        exec "$@"
    fi
fi
if ! command -v gnome-keyring-daemon >/dev/null 2>&1; then
    echo "$LOG_PREFIX gnome-keyring-daemon not installed, fallback to no-keyring" >&2
    exec "$@"
fi
export GNOME_KEYRING_CONTROL="${GNOME_KEYRING_CONTROL:-/run/user/0/keyring}"
export SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-/run/user/0/keyring/ssh}"
mkdir -p "$GNOME_KEYRING_CONTROL" 2>/dev/null || true
echo "" | gnome-keyring-daemon --daemonize --login --components=secrets >/dev/null 2>&1 || {
    echo "$LOG_PREFIX gnome-keyring-daemon failed to start" >&2
    exec "$@"
}
for i in 1 2 3 4 5 10 20; do
    [[ -S "$GNOME_KEYRING_CONTROL/control" ]] && break
    sleep 0.05
done
echo "$LOG_PREFIX keyring ready: DBUS=$DBUS_SESSION_BUS_ADDRESS CTRL=$GNOME_KEYRING_CONTROL" >&2
exec "$@"
