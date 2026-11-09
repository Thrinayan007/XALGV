"""Deterministic acceptance checks, separate from model-visible action tools."""

import hashlib
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from .errors import BrowserError
from .schemas import ArtifactCheck


async def verify(session, request):
    results = []
    async with session.operation() as page:
        for check in request.checks:
            passed = False
            error = None
            try:
                if isinstance(check, ArtifactCheck):
                    path = session.artifact_path(check.path)
                    if not path.is_file():
                        raise BrowserError("MISSING_ARTIFACT", "Required artifact does not exist")
                    if path.stat().st_size > 50 * 1024 * 1024:
                        raise BrowserError("ARTIFACT_LIMIT", "Acceptance artifact exceeds the 50 MiB verification limit")
                    content = path.read_bytes()
                    passed = len(content) >= check.min_bytes
                    if check.contains is not None:
                        passed = passed and check.contains in content.decode("utf-8", errors="replace")
                    if check.sha256:
                        passed = passed and hashlib.sha256(content).hexdigest() == check.sha256
                    if check.png or check.width is not None or check.height is not None:
                        from PIL import Image
                        with Image.open(path) as picture:
                            passed = passed and picture.format == "PNG" and picture.width > 0 and picture.height > 0
                            if check.width is not None:
                                passed = passed and picture.width == check.width
                            if check.height is not None:
                                passed = passed and picture.height == check.height
                            picture.verify()
                elif check.kind == "url":
                    passed = page.url == check.expected
                elif check.kind == "url_host":
                    passed = urlsplit(page.url).hostname == check.expected.lower()
                elif check.kind == "url_contains":
                    passed = check.expected in page.url
                elif check.kind == "text":
                    passed = check.expected in await page.locator("body").inner_text(timeout=5000)
                elif check.kind == "tab_count":
                    passed = len(await session.tabs.snapshot()) == int(check.expected)
            except (BrowserError, PlaywrightError, OSError, ValueError) as exc:
                passed = False
                error = str(exc)
            results.append({"check": check.model_dump(), "passed": bool(passed), "error": error})
        result = await session.describe("verify")
        result.success = all(item["passed"] for item in results)
        result.task_verification = "passed" if result.success else "failed"
        result.data = {"checks": results}
        result.output = "\n".join(f"{i+1}. {item['check']['kind']}: {'passed' if item['passed'] else 'failed'}" for i, item in enumerate(results))
        if not result.success:
            result.error = "One or more trusted acceptance checks failed"
            result.error_code = "TASK_INCOMPLETE"
        return result
