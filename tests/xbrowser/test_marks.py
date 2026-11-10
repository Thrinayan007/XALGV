import pytest

from sandbox.xbrowser import actions
from sandbox.xbrowser.controller import dispatch
from sandbox.xbrowser.errors import BrowserError
from sandbox.xbrowser.observe import observe
from sandbox.xbrowser.schemas import ClickRequest, ObserveRequest, TypeRequest


async def snapshot(browser, page, **kwargs):
    return await observe(browser, page, ObserveRequest(**kwargs))


def element(result, name, *, frame=None):
    return next(item for item in result.data["elements"] if item["name"] == name and
                (frame is None or item["frame_id"] == frame))


def click_request(result, target):
    return ClickRequest(ref=target["ref"], snapshot_id=result.snapshot_id)


async def test_discovers_visible_controls_without_mutating_dom(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        await page.evaluate("""() => {
            window.mutations=[];
            new MutationObserver(records=>window.mutations.push(...records))
                .observe(document,{subtree:true,attributes:true,childList:true});
        }""")
        result = await snapshot(browser, page)
        names = [item["name"] for item in result.data["elements"]]
        assert "Display name" in names and "Save profile" in names
        assert "Shadow action" in names and "Shadow name" in names
        assert "Covered action" not in names and "Hidden action" not in names
        assert "Below viewport" not in names
        assert element(result, "Disabled action")["disabled"]
        assert result.data["counts"]["below"] > 0
        assert await page.evaluate("window.mutations.length") == 0
        assert "secret-do-not-expose" not in result.model_dump_json()
        assert "hidden-secret" not in result.model_dump_json()


async def test_reference_click_type_and_unrelated_updates(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        await page.locator("#clock").evaluate("el=>el.textContent='12:01'")
        field = element(result, "Display name")
        typed = await actions.type_text(browser, page, TypeRequest(
            ref=field["ref"], snapshot_id=result.snapshot_id, text="Ada",
        ))
        assert typed.success and typed.action_verification["value_matches"]
        assert typed.state_changed
        clicked = await actions.click(browser, page, click_request(result, element(result, "Save profile")))
        assert clicked.success and clicked.state_changed
        assert await page.locator("#result").inner_text() == "Saved"


@pytest.mark.parametrize("change", ["replace", "rename", "href", "hide"])
async def test_changed_target_is_rejected_before_dispatch(browser, site, change):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        target = element(result, "Dynamic action")
        scripts = {
            "replace": "el=>el.replaceWith(el.cloneNode(true))",
            "rename": "el=>el.textContent='Different action'",
            "href": "el=>el.setAttribute('href','https://example.com/different')",
            "hide": "el=>el.style.display='none'",
        }
        await page.locator("#dynamic").evaluate(scripts[change])
        with pytest.raises(BrowserError) as error:
            await actions.click(browser, page, click_request(result, target))
        assert error.value.code == "STALE_REF"
        assert await page.evaluate("window.clicked") is None


@pytest.mark.parametrize("duplicate", [False, True])
async def test_hybrid_recovers_only_a_unique_detached_reference(browser, site, duplicate):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        target = element(result, "Dynamic action")
        await page.locator("#dynamic").evaluate("""(el, duplicate) => {
            const replacements = [el.cloneNode(true)];
            if (duplicate) replacements.push(el.cloneNode(true));
            el.replaceWith(...replacements);
        }""", duplicate)
        clicked = await dispatch(browser, "click", ClickRequest(
            ref=target["ref"], snapshot_id=result.snapshot_id,
            semantic_target={"role": "button", "name": "Dynamic action", "frame_id": "f1"},
        ))
        if duplicate:
            assert clicked.error_code == "AMBIGUOUS_TARGET"
            assert clicked.snapshot_id and clicked.snapshot_id != result.snapshot_id
            assert clicked.data["observation"]["elements"]
            assert await page.evaluate("window.clicked") is None
        else:
            assert clicked.success and clicked.action_verification["target_strategy"] == "semantic_recovery"
            assert await page.evaluate("window.clicked") == "original"


async def test_semantic_group_context_and_nested_scroll(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        await page.locator("body").evaluate("""el => el.innerHTML = `
            <fieldset><legend>Billing</legend><button>Save</button></fieldset>
            <fieldset><legend>Shipping</legend>
                <div aria-label="Shipping controls" style="height:30px;overflow:auto">
                    <div style="height:100px"></div>
                    <button onclick="window.clicked='shipping'">Save</button>
                </div>
            </fieldset>`""")
        viewport = await snapshot(browser, page)
        assert element(viewport, "Shipping controls")["scrollable"]
        assert len([item for item in viewport.data["elements"] if item["name"] == "Save"]) == 1
        result = await dispatch(browser, "click", ClickRequest(
            semantic_target={"role": "button", "name": "Save", "nearby_label": "Shipping"},
        ))
        assert result.success and result.action_verification["target_strategy"] == "semantic"
        assert await page.evaluate("window.clicked") == "shipping"


async def test_old_snapshot_tab_and_navigation_are_rejected(browser, site):
    first = await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        old = await snapshot(browser, page)
        current = await snapshot(browser, page)
        with pytest.raises(BrowserError) as error:
            await browser.references.resolve(browser, old.snapshot_id, 1)
        assert error.value.code == "STALE_REF"
    second = await browser.tab_action("new", url=f"{site}/marks.html")
    assert second.tab_id != first.tab_id
    with pytest.raises(BrowserError) as error:
        await browser.references.resolve(browser, current.snapshot_id, 1)
    assert error.value.code == "STALE_REF"
    await browser.tab_action("switch", tab=first.tab_id)
    await browser.open(f"{site}/marks.html")  # Reload with the same URL also invalidates references.
    with pytest.raises(BrowserError) as error:
        await browser.references.resolve(browser, current.snapshot_id, 1)
    assert error.value.code == "STALE_REF"


async def test_shadow_and_cross_origin_frame_references(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        await actions.click(browser, page, click_request(result, element(result, "Shadow action")))
        assert await page.locator("#shadow").get_by_text("Shadow clicked").count() == 1
        await actions.type_text(browser, page, TypeRequest(
            ref=element(result, "Shadow name")["ref"], snapshot_id=result.snapshot_id, text="Shadow value",
        ))
        assert await page.locator("#shadow").locator("input").input_value() == "Shadow value"
        frame_actions = [item for item in result.data["elements"] if item["name"] == "Frame action"]
        assert len(frame_actions) == 2
        for target in frame_actions:
            await actions.type_text(browser, page, TypeRequest(
                ref=element(result, "Frame name", frame=target["frame_id"])["ref"],
                snapshot_id=result.snapshot_id, text="Frame value",
            ))
            await actions.click(browser, page, click_request(result, target))
        for frame in page.frames[1:]:
            assert await frame.get_by_text("Frame clicked").count() == 1
            assert await frame.locator("input").input_value() == "Frame value"


async def test_disabled_refs_are_not_actionable(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        with pytest.raises(BrowserError) as error:
            await actions.click(browser, page, click_request(result, element(result, "Disabled action")))
        assert error.value.code == "TARGET_NOT_ACTIONABLE"


async def test_page_and_frame_occlusion_rechecked_before_action(browser, site):
    await browser.open(f"{site}/marks.html")
    async with browser.operation() as page:
        result = await snapshot(browser, page)
        target = element(result, "Frame action", frame="f2")
        await page.locator("iframe").first.evaluate("""el => {
            const rect=el.getBoundingClientRect(), cover=document.createElement('div');
            cover.style=`position:fixed;left:${rect.x}px;top:${rect.y}px;width:${rect.width}px;height:${rect.height}px;z-index:999`;
            document.body.append(cover);
        }""")
        with pytest.raises(BrowserError) as error:
            await actions.click(browser, page, click_request(result, target))
        assert error.value.code == "TARGET_NOT_ACTIONABLE"
        await page.evaluate("""() => {
            const rect=document.querySelector('#save').getBoundingClientRect();
            const cover=document.createElement('div');
            cover.style=`position:fixed;left:${rect.x}px;top:${rect.y}px;width:${rect.width}px;height:${rect.height}px;z-index:999`;
            document.body.append(cover);
        }""")
        with pytest.raises(BrowserError) as error:
            await actions.click(browser, page, click_request(result, element(result, "Save profile")))
        assert error.value.code == "TARGET_NOT_ACTIONABLE"
