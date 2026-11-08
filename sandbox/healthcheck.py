"""Health includes the live desktop and VNC backend, not just FastAPI."""

import socket
import subprocess
import sys
import urllib.request


def main():
    try:
        with socket.create_connection(("127.0.0.1", 5900), timeout=1) as connection:
            if not connection.recv(12).startswith(b"RFB "):
                return 1
        if "--vnc-only" in sys.argv:
            return 0
        subprocess.run(["xwininfo", "-root"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1, check=True)
        for url in ("http://127.0.0.1:9000/health", "http://127.0.0.1:6080/vnc.html"):
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status != 200:
                    return 1
        return 0
    except (OSError, subprocess.SubprocessError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
