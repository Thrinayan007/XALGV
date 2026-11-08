"""Stable IDs are monotonic even across Chromium generations."""

from typing import Any

from playwright.async_api import Error as PlaywrightError

from .errors import BrowserError
from .schemas import TabInfo


class TabRegistry:
    def __init__(self):
        self._pages: dict[str, Any] = {}
        self._next_id = 1
        self.active: str | None = None

    @property
    def count(self):
        return len(self._pages)

    def register(self, page) -> str:
        for tab_id, existing in self._pages.items():
            if existing is page:
                return tab_id
        tab_id = f"t{self._next_id}"
        self._next_id += 1
        self._pages[tab_id] = page
        self.active = tab_id
        page.on("close", lambda *_: self.remove(tab_id))
        return tab_id

    def remove(self, tab_id: str):
        self._pages.pop(tab_id, None)
        if self.active == tab_id:
            self.active = next(reversed(self._pages), None)

    def clear(self):
        self._pages.clear()
        self.active = None

    def page(self, tab_id: str | None = None):
        target = tab_id if tab_id is not None else self.active
        page = self._pages.get(target)
        if page is None or page.is_closed():
            raise BrowserError("TAB_NOT_FOUND", f"Unknown or closed tab: {target}")
        return page

    async def snapshot(self) -> list[TabInfo]:
        result = []
        for tab_id, page in list(self._pages.items()):
            if page.is_closed():
                self.remove(tab_id)
                continue
            try:
                title = await page.title()
            except PlaywrightError:
                title = ""  # A navigating popup may not yet have a document.
            result.append(TabInfo(tab_id=tab_id, url=page.url, title=title, active=tab_id == self.active))
        return result
