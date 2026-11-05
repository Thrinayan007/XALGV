#!/bin/bash
set -e

# ── Start Xvfb (virtual display) ────────────────────────────────────────
echo "[entrypoint] Starting Xvfb on display ${DISPLAY} at ${XVFB_RESOLUTION}..."
Xvfb ${DISPLAY} -screen 0 ${XVFB_RESOLUTION} -ac +extension GLX +render -noreset &
sleep 1

# ── Start Openbox window manager ────────────────────────────────────────
echo "[entrypoint] Starting Openbox..."
openbox &
sleep 0.5

# ── Start x11vnc for remote viewing ─────────────────────────────────────
echo "[entrypoint] Starting x11vnc..."
x11vnc -display ${DISPLAY} -forever -nopw -shared -rfbport 5900 &

# ── Start noVNC web client ──────────────────────────────────────────────
echo "[entrypoint] Starting noVNC on port 6080..."
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# ── Start the tool server ───────────────────────────────────────────────
echo "[entrypoint] Starting tool server on port 9000..."
exec python3 -m uvicorn tool_server:app --host 0.0.0.0 --port 9000 --app-dir /opt/tool_server
