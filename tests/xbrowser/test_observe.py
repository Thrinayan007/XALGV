import pytest

from sandbox.xbrowser.errors import BrowserError
from sandbox.xbrowser.observe import observe
from sandbox.xbrowser.schemas import ObserveRequest


@pytest.mark.parametrize("budget", [800, 1600, 2400])
async def test_observation_is_bounded_and_only_reported_refs_resolve(browser, budget):
    async with browser.operation() as page:
        await page.set_content("<title>Large page</title>" + "".join(
            f'<button style="display:block">Action {i} ' + "x" * 100 + "</button>" for i in range(200)
        ))
        result = await observe(browser, page, ObserveRequest(scope="page", max_chars=budget, text=True))
        assert len(result.output) <= budget
        assert result.data["output_chars"] == len(result.output)
        assert result.data["counts"]["omitted"] > 0
        assert result.state_after and result.snapshot_id in result.output
        for item in result.data["elements"]:
            assert f"[{item['ref']}]" in result.output
        with pytest.raises(BrowserError) as error:
            await browser.references.resolve(browser, result.snapshot_id, len(result.data["elements"]) + 1)
        assert error.value.code == "INVALID_REF"


async def test_page_scope_exposes_below_viewport_and_scroll_state(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await observe(browser, page, ObserveRequest(scope="page", max_elements=100, max_chars=8000))
        below = next(item for item in result.data["elements"] if item["name"] == "Below viewport")
        target = await browser.references.resolve(browser, result.snapshot_id, below["ref"])
        await target.click(timeout=5000)  # Playwright scrolls the exact retained element into view.
        after = await observe(browser, page, ObserveRequest())
        assert after.state_after != result.state_after
        assert after.data["scroll"]["y"] > 0


async def test_snapshot_identity_changes_after_restart(browser, site):
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        old = await observe(browser, page, ObserveRequest())
    await browser.control("restart")
    await browser.open(f"{site}/first.html")
    async with browser.operation() as page:
        current = await observe(browser, page, ObserveRequest())
        assert current.snapshot_id != old.snapshot_id
        with pytest.raises(BrowserError) as error:
            await browser.references.resolve(browser, old.snapshot_id, 1)
        assert error.value.code == "STALE_REF"


async def test_multilingual_observation_respects_measured_token_budget(browser):
    from sandbox.xbrowser.token_budget import token_count
    async with browser.operation() as page:
        await page.set_content("<title>多语言页面</title>" + "".join(
            f'<button style="display:block">操作 {i} 中文 العربية हिन्दी ' + "界" * 50 + "</button>" for i in range(100)
        ))
        result = await observe(browser, page, ObserveRequest(scope="page", max_elements=100, max_chars=8000, text=True))
        actual = token_count(result.output)
        if actual is not None:
            assert actual == result.data["token_count"] <= 700
        else:
            assert result.data["tokenizer"] == "character_estimate"
        for item in result.data["elements"]:
            assert f"[{item['ref']}]" in result.output
