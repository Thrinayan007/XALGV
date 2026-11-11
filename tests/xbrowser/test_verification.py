from pathlib import Path

from sandbox.xbrowser.controller import execute
from sandbox.xbrowser.schemas import ScreenshotRequest, VerificationRequest
from sandbox.xbrowser.verification import verify


async def test_task_checks_require_all_artifacts_and_contents(browser, site):
    await browser.open(f"{site}/first.html")
    screenshot = await execute(browser, "screenshot", ScreenshotRequest(path="one.png", marks=True))
    assert screenshot.success and screenshot.snapshot_id and screenshot.data["elements"]
    assert Path(screenshot.artifacts[0]).read_bytes().startswith(b"\x89PNG")
    checks = [{"kind": "artifact", "path": "one.png", "png": True},
              {"kind": "artifact", "path": "missing.png", "png": True},
              {"kind": "url", "expected": f"{site}/first.html"},
              {"kind": "text", "expected": "XBrowser first page"}]
    failed = await verify(browser, VerificationRequest(checks=checks))
    assert not failed.success and failed.task_verification == "failed"
    assert failed.data["checks"][0]["passed"] and not failed.data["checks"][1]["passed"]
    (browser.workspace / "missing.png").write_bytes(Path(screenshot.artifacts[0]).read_bytes())
    passed = await verify(browser, VerificationRequest(checks=checks))
    assert passed.success and passed.task_verification == "passed"
    dimension_failure = await verify(browser, VerificationRequest(checks=[
        {"kind": "artifact", "path": "one.png", "width": 1, "height": 1},
    ]))
    assert not dimension_failure.success
    (browser.workspace / "fake.png").write_text("not an image")
    corrupt = await verify(browser, VerificationRequest(checks=[{"kind": "artifact", "path": "fake.png", "png": True}]))
    assert not corrupt.success


async def test_task_checks_confine_paths_and_verify_report_contents(browser):
    (browser.workspace / "report.md").write_text("All five sources were recorded.")
    result = await verify(browser, VerificationRequest(checks=[
        {"kind": "artifact", "path": "report.md", "contains": "five sources"},
        {"kind": "artifact", "path": "../escape.txt"},
        {"kind": "artifact", "path": ".xbrowser/test/profile/secret"},
    ]))
    assert not result.success
    assert [item["passed"] for item in result.data["checks"]] == [True, False, False]
