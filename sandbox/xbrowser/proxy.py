"""Browser-only egress proxy: validate DNS results and connect to a pinned IP.

TLS stays end-to-end between Chromium and the destination. No certificate
interception, credential logging, or ambient environment proxy configuration.
"""

import asyncio
import re
import socket
from contextlib import suppress
from urllib.parse import urlsplit

from .errors import BrowserError


class EgressProxy:
    def __init__(self, policy, events):
        self.policy = policy
        self.events = events
        self._server = None
        self._tasks = set()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def start(self):
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, limit=65536)
        return self.url

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, reader, writer):
        if len(self._tasks) >= 128:
            writer.close()
            await writer.wait_closed()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream = None
        connected = False
        try:
            async with asyncio.timeout(15):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("latin1").split("\r\n")
                method, target, version = lines[0].split(" ", 2)
                if version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise BrowserError("NAVIGATION_DENIED", "Unsupported proxy protocol")
                if method == "CONNECT" and not re.fullmatch(r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):[0-9]+", target):
                    raise BrowserError("NAVIGATION_DENIED", "Invalid CONNECT authority")
                if method != "CONNECT" and urlsplit(target).scheme != "http":
                    raise BrowserError("NAVIGATION_DENIED", "Plain proxy requests require an absolute HTTP URL")
                if any(line.lower().startswith("upgrade:") for line in lines[1:]):
                    raise BrowserError("NAVIGATION_DENIED", "Protocol upgrades are disabled")
                url = f"https://{target}/" if method == "CONNECT" else target
                ips, port = await self.policy.resolve_addresses(url)
                for ip in ips:
                    try:
                        source, upstream = await asyncio.open_connection(ip, port, flags=socket.AI_NUMERICHOST)
                        break
                    except OSError:
                        if ip == ips[-1]:
                            raise
                if method == "CONNECT":
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                else:
                    parsed = urlsplit(url)
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    clean = []
                    for line in lines[1:]:
                        if not line:
                            continue
                        name = line.split(":", 1)[0].lower()
                        if name not in {"proxy-connection", "proxy-authorization", "connection", "host"}:
                            clean.append(line)
                    clean += [f"Host: {parsed.netloc}", "Connection: close"]
                    upstream.write((f"{method} {path} {version}\r\n" + "\r\n".join(clean) + "\r\n\r\n").encode("latin1"))
                    await upstream.drain()
                connected = True
            # No 15s lifetime cap on active TLS/page transfers; operation deadlines
            # live in the controller. Cleanup closes all tunnels on session stop.
            async def copy(source_reader, destination):
                while data := await source_reader.read(65536):
                    destination.write(data)
                    await destination.drain()

            first = asyncio.create_task(copy(reader, upstream))
            second = asyncio.create_task(copy(source, writer))
            try:
                await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for transfer in (first, second):
                    transfer.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
        except (BrowserError, OSError, ValueError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            if isinstance(exc, BrowserError):
                self.events.append({"type": "navigation_denied", "code": exc.code, "reason": str(exc)})
            if not connected:
                with suppress(OSError):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
        finally:
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            self._tasks.discard(task)
