FROM debian:bookworm-slim

LABEL maintainer="XALGV" \
      description="Sandboxed Linux workstation for the XALGV autonomous agent"

# ── System packages ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core utilities
    bash coreutils procps curl wget ca-certificates \
    # Dev tools
    python3 python3-pip python3-venv \
    nodejs npm \
    git build-essential \
    # GUI / display
    xvfb openbox x11-utils x11vnc xdotool wmctrl scrot \
    # Accessibility
    at-spi2-core \
    # noVNC for remote viewing
    novnc websockify \
    # Office
    libreoffice-writer libreoffice-calc libreoffice-impress --no-install-recommends \
    # Cleanup
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ── Non-root user ────────────────────────────────────────────────────────
RUN useradd -ms /bin/bash agent && mkdir -p /workspace && chown agent:agent /workspace

# ── Python dependencies for the tool server ──────────────────────────────
COPY tool_server_requirements.txt /opt/tool_server/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /opt/tool_server/requirements.txt

# ── Tool server ──────────────────────────────────────────────────────────
COPY tool_server.py /opt/tool_server/tool_server.py
COPY entrypoint.sh /opt/tool_server/entrypoint.sh
RUN chmod +x /opt/tool_server/entrypoint.sh

# ── Display config ───────────────────────────────────────────────────────
ENV DISPLAY=:99
ENV XVFB_RESOLUTION=1280x720x24

# ── Run as non-root ──────────────────────────────────────────────────────
USER agent
WORKDIR /workspace

EXPOSE 9000 6080

ENTRYPOINT ["/opt/tool_server/entrypoint.sh"]
