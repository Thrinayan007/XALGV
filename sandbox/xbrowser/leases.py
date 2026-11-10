"""Server-side fencing for the shared browser API session."""

import asyncio
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserLease:
    token: str
    owner: str
    expires: float


class BrowserLeaseGate:
    def __init__(self):
        self.lock = asyncio.Lock()
        self._lease: BrowserLease | None = None

    def current(self):
        if self._lease and self._lease.expires <= time.monotonic():
            self._lease = None
        return self._lease

    def acquire(self, owner: str, seconds: int):
        if self.current():
            return None
        self._lease = BrowserLease(secrets.token_urlsafe(32), owner, time.monotonic() + seconds)
        return self._lease

    def permits(self, token: str):
        current = self.current()
        return token.isascii() and secrets.compare_digest(current.token, token) if current else not token

    def release(self, token: str):
        if not token or not self.permits(token):
            return False
        self._lease = None
        return True
