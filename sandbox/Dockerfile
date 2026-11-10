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
   # GUI / display
    xvfb openbox xterm feh xdg-utils x11-utils x11vnc xdotool wmctrl scrot \
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

# ── Install Playwright browsers ──────────────────────────────────────────
# RUN python3 -m playwright install --with-deps chromium
# Install browsers in a shared directory accessible to agent.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN mkdir -p /ms-playwright \
    && python3 -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# ── Tool server ──────────────────────────────────────────────────────────
COPY tool_server.py /opt/tool_server/tool_server.py
COPY healthcheck.py /opt/tool_server/healthcheck.py
COPY browser_server.py /opt/tool_server/browser_server.py
COPY xbrowser /opt/tool_server/xbrowser
RUN python3 -c "import hashlib,json,pathlib,urllib.request; config=json.loads(pathlib.Path('/opt/tool_server/xbrowser/tokenizer_config.json').read_text()); data=urllib.request.urlopen(config['url'],timeout=120).read(); assert hashlib.sha256(data).hexdigest()==config['sha256'], 'Tokenizer checksum mismatch'; pathlib.Path('/opt/tool_server/xbrowser-tokenizer.json').write_bytes(data)"
ENV XBROWSER_TOKENIZER_PATH=/opt/tool_server/xbrowser-tokenizer.json
COPY entrypoint.sh /opt/tool_server/entrypoint.sh
RUN sed -i 's/\r$//' /opt/tool_server/entrypoint.sh && chmod +x /opt/tool_server/entrypoint.sh \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# ── Display config ───────────────────────────────────────────────────────
ENV DISPLAY=:99
ENV XVFB_RESOLUTION=1280x720x24

# ── Run as non-root ──────────────────────────────────────────────────────
USER agent
WORKDIR /workspace

EXPOSE 9000 6080

ENTRYPOINT ["/opt/tool_server/entrypoint.sh"]
