"""Browser automation endpoints for the sandbox tool server.

Uses Playwright to control a browser inside the sandbox container.
Follows the 'structure first, pixels second' principle — the
accessibility tree is the primary interface, screenshots are fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/tools/browser", tags=["browser"])

# ── Global browser state ────────────────────────────────────────────────

_browser = None
_page = None


async def _ensure_browser():
    """Lazily initialise the browser and a default page."""
    global _browser, _page
    if _browser is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=False,  # Visible on the Xvfb display for noVNC viewing.
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                f"--display={os.environ.get('DISPLAY', ':99')}",
            ],
        )
    if _page is None or _page.is_closed():
        context = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="XALGV-Agent/0.1 (autonomous research assistant)",
        )
        _page = await context.new_page()
    return _page


# ── Request / Response models ────────────────────────────────────────────

class BrowserResponse(BaseModel):
    success: bool
    output: str = ""
    error: str | None = None
    artifacts: list[str] = []


class NavigateRequest(BaseModel):
    url: str
    wait_for: str = "load"


class ContentRequest(BaseModel):
    mode: str = "a11y"  # "a11y", "text", "html"
    selector: str | None = None


class ClickRequest(BaseModel):
    selector: str | None = None
    text: str | None = None
    role: str | None = None


class TypeRequest(BaseModel):
    selector: str
    text: str
    clear_first: bool = True
    press_enter: bool = False


class ScreenshotRequest(BaseModel):
    full_page: bool = False
    selector: str | None = None


class DownloadRequest(BaseModel):
    url: str
    filename: str | None = None


class LoginRequest(BaseModel):
    username_selector: str
    password_selector: str
    username_env: str
    password_env: str
    submit_selector: str | None = None


class SubmitRequest(BaseModel):
    selector: str
    confirmation: str


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/navigate", response_model=BrowserResponse)
async def navigate(req: NavigateRequest) -> BrowserResponse:
    """Navigate to a URL and return page title + accessible summary."""
    try:
        page = await _ensure_browser()
        response = await page.goto(req.url, wait_until=req.wait_for, timeout=30000)

        title = await page.title()
        url = page.url

        # Get a quick accessibility snapshot for context.
        try:
            a11y = await page.accessibility.snapshot()
            summary = _format_a11y_tree(a11y, max_depth=2, max_items=20)
        except Exception:
            summary = f"Page loaded but accessibility tree unavailable."

        status_code = response.status if response else "unknown"
        output = (
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Status: {status_code}\n\n"
            f"Page summary:\n{summary}"
        )
        return BrowserResponse(success=True, output=output[:10000])
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Navigation error: {exc}")


@router.post("/get_content", response_model=BrowserResponse)
async def get_content(req: ContentRequest) -> BrowserResponse:
    """Get page content as accessibility tree, text, or simplified HTML."""
    try:
        page = await _ensure_browser()

        if req.mode == "a11y":
            snapshot = await page.accessibility.snapshot()
            if snapshot is None:
                return BrowserResponse(success=True, output="[Empty accessibility tree]")
            output = _format_a11y_tree(snapshot, max_depth=5, max_items=100)

        elif req.mode == "text":
            if req.selector:
                element = page.locator(req.selector)
                output = await element.inner_text(timeout=5000)
            else:
                output = await page.inner_text("body", timeout=5000)

        elif req.mode == "html":
            if req.selector:
                element = page.locator(req.selector)
                output = await element.inner_html(timeout=5000)
            else:
                # Return simplified HTML — strip scripts, styles, etc.
                output = await page.evaluate("""() => {
                    const clone = document.body.cloneNode(true);
                    clone.querySelectorAll('script, style, noscript, svg, img').forEach(el => el.remove());
                    return clone.innerHTML.replace(/\\s+/g, ' ').trim();
                }""")
        else:
            return BrowserResponse(success=False, error=f"Unknown mode: {req.mode}")

        return BrowserResponse(success=True, output=output[:20000])
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Content error: {exc}")


@router.post("/click", response_model=BrowserResponse)
async def click(req: ClickRequest) -> BrowserResponse:
    """Click an element by selector, text, or role."""
    try:
        page = await _ensure_browser()

        if req.selector:
            await page.click(req.selector, timeout=5000)
            desc = f"Clicked element: {req.selector}"
        elif req.text and req.role:
            await page.get_by_role(req.role, name=req.text).click(timeout=5000)
            desc = f"Clicked {req.role} with text '{req.text}'"
        elif req.text:
            await page.get_by_text(req.text, exact=False).first.click(timeout=5000)
            desc = f"Clicked element with text '{req.text}'"
        else:
            return BrowserResponse(success=False, error="Provide selector, text, or text+role.")

        # Wait briefly for any navigation or DOM updates.
        await page.wait_for_timeout(500)

        title = await page.title()
        return BrowserResponse(success=True, output=f"{desc}\nPage title: {title}\nURL: {page.url}")
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Click error: {exc}")


@router.post("/type", response_model=BrowserResponse)
async def type_text(req: TypeRequest) -> BrowserResponse:
    """Type text into an input element."""
    try:
        page = await _ensure_browser()

        if req.clear_first:
            await page.fill(req.selector, "", timeout=5000)

        await page.type(req.selector, req.text, timeout=5000)

        if req.press_enter:
            await page.press(req.selector, "Enter")
            await page.wait_for_timeout(1000)

        return BrowserResponse(
            success=True,
            output=f"Typed '{req.text[:50]}...' into {req.selector}"
                   + (" and pressed Enter" if req.press_enter else ""),
        )
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Type error: {exc}")


@router.post("/screenshot", response_model=BrowserResponse)
async def browser_screenshot(req: ScreenshotRequest) -> BrowserResponse:
    """Take a screenshot of the browser page."""
    try:
        page = await _ensure_browser()
        screenshot_path = "/tmp/browser_screenshot.png"

        if req.selector:
            element = page.locator(req.selector)
            await element.screenshot(path=screenshot_path, timeout=5000)
        else:
            await page.screenshot(path=screenshot_path, full_page=req.full_page)

        return BrowserResponse(
            success=True,
            output=f"Screenshot saved to {screenshot_path}",
            artifacts=[screenshot_path],
        )
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Screenshot error: {exc}")


def _captcha_present(text: str) -> bool:
    """Detect a challenge and stop; solving or bypassing one is never attempted."""
    markers = ("captcha", "recaptcha", "hcaptcha", "verify you are human", "security check")
    return any(marker in text.lower() for marker in markers)


async def _page_text(page: Any) -> str:
    return await page.inner_text("body", timeout=5000)


@router.post("/extract", response_model=BrowserResponse)
async def extract(req: ContentRequest) -> BrowserResponse:
    """Extract readable page text, metadata, links, and candidate PDFs for research."""
    try:
        page = await _ensure_browser()
        body = await _page_text(page)
        if _captcha_present(body):
            return BrowserResponse(success=False, error="CAPTCHA detected. Human completion is required; the agent will not bypass it.")

        data = await page.evaluate("""() => {
            const meta = name => document.querySelector(`meta[name="${name}"], meta[property="${name}"]`)?.content || "";
            const links = [...document.querySelectorAll('a[href]')].map(a => ({text: (a.innerText || '').trim(), url: a.href}));
            return {
              title: document.title,
              url: location.href,
              description: meta('description') || meta('og:description'),
              author: meta('author') || meta('article:author'),
              published: meta('article:published_time') || meta('date') || '',
              canonical: document.querySelector('link[rel="canonical"]')?.href || location.href,
              links
            };
        }""")
        links = data.pop("links", [])
        pdfs = [link["url"] for link in links if re.search(r"\\.pdf(?:[?#].*)?$", link["url"], re.I)][:20]
        external_domains = {re.sub(r"^www\\.", "", re.split(r"/", link["url"].split("://", 1)[-1])[0]) for link in links if link["url"].startswith("http")}
        # Transparent heuristic, not an assertion of truthfulness.
        score = 40
        score += 15 if data["author"] else 0
        score += 15 if data["published"] else 0
        score += 10 if data["canonical"] else 0
        score += 10 if len(body) > 1500 else 0
        score += 10 if len(external_domains) >= 2 else 0
        payload = {
            "source": data,
            "quality": {
                "score": min(score, 100),
                "method": "metadata/transparency heuristic; not a factual-accuracy guarantee",
                "signals": {"has_author": bool(data["author"]), "has_publication_date": bool(data["published"]), "long_form": len(body) > 1500, "external_link_domains": len(external_domains)},
            },
            "pdf_links": pdfs,
            "text": body[:80000],
        }
        return BrowserResponse(success=True, output=json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Extraction error: {exc}")


@router.post("/download", response_model=BrowserResponse)
async def download(req: DownloadRequest) -> BrowserResponse:
    """Download a public PDF into /workspace/downloads with a size limit."""
    if not req.url.startswith(("https://", "http://")):
        return BrowserResponse(success=False, error="Only http(s) downloads are allowed.")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            async with client.stream("GET", req.url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "pdf" not in content_type and not re.search(r"\\.pdf(?:[?#].*)?$", str(response.url), re.I):
                    return BrowserResponse(success=False, error="Refusing non-PDF download.")
                name = re.sub(r"[^A-Za-z0-9._-]", "_", req.filename or Path(str(response.url).split("?", 1)[0]).name or "document.pdf")
                if not name.lower().endswith(".pdf"):
                    name += ".pdf"
                target_dir = Path("/workspace/downloads")
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / name
                size = 0
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 25 * 1024 * 1024:
                            target.unlink(missing_ok=True)
                            return BrowserResponse(success=False, error="Refusing PDF larger than 25 MB.")
                        handle.write(chunk)
        return BrowserResponse(success=True, output=f"Downloaded PDF to {target}", artifacts=[str(target)])
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Download error: {exc}")


@router.post("/login", response_model=BrowserResponse)
async def login(req: LoginRequest) -> BrowserResponse:
    """Log in using secrets already supplied to the sandbox environment."""
    username, password = os.environ.get(req.username_env), os.environ.get(req.password_env)
    if not username or not password:
        return BrowserResponse(success=False, error="Required credential environment variables are unavailable in the sandbox.")
    try:
        page = await _ensure_browser()
        if _captcha_present(await _page_text(page)):
            return BrowserResponse(success=False, error="CAPTCHA detected. Human completion is required; the agent will not bypass it.")
        await page.fill(req.username_selector, username, timeout=5000)
        await page.fill(req.password_selector, password, timeout=5000)
        if req.submit_selector:
            await page.click(req.submit_selector, timeout=5000)
            await page.wait_for_timeout(1000)
        return BrowserResponse(success=True, output=f"Credentials entered; current URL: {page.url}")
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Login error: {exc}")


@router.post("/submit", response_model=BrowserResponse)
async def submit(req: SubmitRequest) -> BrowserResponse:
    """Submit a form only after an explicit, per-action confirmation phrase."""
    if req.confirmation != "I_CONFIRM_SUBMIT":
        return BrowserResponse(success=False, error="Form submission requires confirmation='I_CONFIRM_SUBMIT'.")
    try:
        page = await _ensure_browser()
        if _captcha_present(await _page_text(page)):
            return BrowserResponse(success=False, error="CAPTCHA detected. Human completion is required; the agent will not bypass it.")
        await page.click(req.selector, timeout=5000)
        await page.wait_for_timeout(1000)
        return BrowserResponse(success=True, output=f"Form submitted; current URL: {page.url}")
    except Exception as exc:
        return BrowserResponse(success=False, error=f"Submit error: {exc}")


# ── Helpers ──────────────────────────────────────────────────────────────

def _format_a11y_tree(
    node: dict[str, Any] | None,
    indent: int = 0,
    max_depth: int = 5,
    max_items: int = 100,
    _count: list[int] | None = None,
) -> str:
    """Format an accessibility tree snapshot into readable text."""
    if node is None:
        return ""
    if _count is None:
        _count = [0]

    if _count[0] >= max_items:
        return ""
    _count[0] += 1

    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")

    parts = []
    prefix = "  " * indent
    label = f"{prefix}[{role}]"
    if name:
        label += f" \"{name}\""
    if value:
        label += f" = \"{value}\""
    parts.append(label)

    if indent < max_depth:
        for child in node.get("children", []):
            child_text = _format_a11y_tree(child, indent + 1, max_depth, max_items, _count)
            if child_text:
                parts.append(child_text)

    return "\n".join(parts)

