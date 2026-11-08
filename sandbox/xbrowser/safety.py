"""Sandbox-owned capabilities. Request bodies cannot grant additional authority."""

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import BrowserError

CORE_ACTIONS = frozenset({"open", "observe", "click", "type", "press", "scroll", "tabs", "wait", "screenshot"})


def public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if address in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(int(address) & 0xffffffff)
        if embedded and not public_address(str(embedded)):
            return False
        if address.teredo and not all(public_address(str(item)) for item in address.teredo):
            return False
    return True


@dataclass(frozen=True)
class BrowserPolicy:
    allowed_actions: frozenset[str] = field(default_factory=lambda: CORE_ACTIONS)
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    local_origins: tuple[str, ...] = ()  # Trusted, exact fixture/service exceptions; never supplied by the model.

    @classmethod
    def from_env(cls):
        def values(name):
            return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())
        origins = []
        for origin in values("XBROWSER_LOCAL_ORIGINS"):
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("XBROWSER_LOCAL_ORIGINS must contain exact HTTP(S) origins")
            host = parsed.hostname.lower()
            origins.append(f"{parsed.scheme}://{'['+host+']' if ':' in host else host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}")
        return cls(
            allowed_actions=frozenset(values("XBROWSER_ALLOWED_ACTIONS")) if "XBROWSER_ALLOWED_ACTIONS" in os.environ else CORE_ACTIONS,
            allowed_domains=values("XBROWSER_ALLOWED_DOMAINS"), denied_domains=values("XBROWSER_DENIED_DOMAINS"),
            local_origins=tuple(origins),
        )

    def check(self, action: str):
        if action not in self.allowed_actions:
            raise BrowserError("ACTION_DENIED", f"Browser action disabled by sandbox policy: {action}")

    def destination(self, url: str) -> tuple[str, int, bool]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname or parsed.username or parsed.password:
            raise BrowserError("NAVIGATION_DENIED", "Only HTTP(S) destinations without embedded credentials are allowed")
        host = parsed.hostname.rstrip(".").encode("idna").decode().lower()
        if "%" in host:
            raise BrowserError("NAVIGATION_DENIED", "Scoped network addresses are forbidden")
        port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
        scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
        origin = f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"
        local = origin in self.local_origins

        def matches(pattern):
            pattern = pattern.lower().rstrip(".")
            return host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:])

        if any(matches(pattern) for pattern in self.denied_domains):
            raise BrowserError("NAVIGATION_DENIED", "Destination is blocked by domain policy")
        if self.allowed_domains and not any(matches(pattern) for pattern in self.allowed_domains) and not local:
            raise BrowserError("NAVIGATION_DENIED", "Destination is outside the domain allowlist")
        if not local and port not in {80, 443}:
            raise BrowserError("NAVIGATION_DENIED", "Destination port is not permitted")
        return host, port, local

    async def resolve(self, url: str) -> tuple[str, int]:
        ips, port = await self.resolve_addresses(url)
        return ips[0], port

    async def resolve_addresses(self, url: str) -> tuple[list[str], int]:
        host, port, local = self.destination(url)
        try:
            addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM,
            ), 5)
        except (OSError, TimeoutError) as exc:
            raise BrowserError("NAVIGATION_DENIED", "Destination could not be resolved") from exc
        ips = list(dict.fromkeys(item[4][0] for item in addresses))
        if not ips or (not local and any(not public_address(address) for address in ips)):
            raise BrowserError("NAVIGATION_DENIED", "Private, loopback, reserved and metadata addresses are forbidden")
        # The proxy connects to this literal IP, never re-resolving the hostname.
        return ips, port
