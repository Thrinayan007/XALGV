#!/bin/bash
set -Eeuo pipefail

# A container restart preserves /tmp but gives services a new PID namespace.
display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
[[ "$display_number" =~ ^[0-9]+$ ]] || { echo "Invalid DISPLAY" >&2; exit 1; }
if ! xwininfo -root -display "$DISPLAY" >/dev/null 2>&1; then
    rm -f -- "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}"
fi

services=()
server_pid=""
cleanup() {
    trap - EXIT TERM INT
    # Let Playwright close its persistent profile while the display is alive.
    if [[ -n "$server_pid" ]]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    if ((${#services[@]})); then
        kill "${services[@]}" 2>/dev/null || true
        wait "${services[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

wait_for() {
    local process="$1"
    shift
    for ((attempt=0; attempt<100; attempt++)); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        kill -0 "$process" 2>/dev/null || { echo "Required desktop service exited" >&2; return 1; }
        sleep 0.1
    done
    echo "Desktop service readiness timed out" >&2
    return 1
}

echo "[entrypoint] Starting Xvfb on $DISPLAY..."
Xvfb "$DISPLAY" -screen 0 "$XVFB_RESOLUTION" -ac +extension GLX +render -noreset &
xvfb_pid=$!
services+=("$xvfb_pid")
wait_for "$xvfb_pid" xwininfo -root -display "$DISPLAY"

openbox &
services+=("$!")

x11vnc -display "$DISPLAY" -forever -nopw -shared -listen 127.0.0.1 -rfbport 5900 &
vnc_pid=$!
services+=("$vnc_pid")
wait_for "$vnc_pid" python3 /opt/tool_server/healthcheck.py --vnc-only

websockify --web=/usr/share/novnc/ 6080 127.0.0.1:5900 &
services+=("$!")
python3 -m uvicorn tool_server:app --host 0.0.0.0 --port 9000 --app-dir /opt/tool_server --timeout-graceful-shutdown 5 &
server_pid=$!
services+=("$server_pid")

# A failed desktop service must restart the container, rather than leave a
# healthy API with an unusable display/VNC connection.
set +e
wait -n "${services[@]}"
status=$?
set -e
echo "[entrypoint] Required service exited (status $status); restarting stack" >&2
exit 1
