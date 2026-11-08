"""Local deterministic pages; real Chromium tests require `playwright install chromium`."""

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from sandbox.xbrowser import BrowserSession
from sandbox.xbrowser.safety import BrowserPolicy


@pytest.fixture(scope="session")
def site():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path.startswith("/redirect?"):
                self.send_response(302)
                self.send_header("Location", parse_qs(urlsplit(self.path).query)["target"][0])
                self.end_headers()
                return
            super().do_GET()

    handler = partial(QuietHandler, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
async def browser(tmp_path, site):
    session = BrowserSession(tmp_path, scope="test", headless=True)
    port = site.rsplit(":", 1)[1]
    session.policy = BrowserPolicy(local_origins=(f"http://127.0.0.1:{port}", f"http://localhost:{port}"))
    yield session
    await session.control("stop")
