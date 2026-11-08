"""Read-only DOM discovery and bounded, snapshot-scoped handle ownership."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Error as PlaywrightError

from .errors import BrowserError

# Only fixed, application-owned JavaScript runs here. Nothing supplied by the model
# is evaluated. No DOM attributes, event listeners, or global variables are installed.
INSPECT = r"""el => {
    const clean = value => (value || '').replace(/\s+/g, ' ').trim();
    const root = el.getRootNode();
    const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => root.getElementById?.(id)?.textContent || '').join(' ');
    const tag = el.localName;
    const type = el.getAttribute('type') || '';
    const implicit = {a:el.hasAttribute('href')?'link':'', button:'button', textarea:'textbox',
        select:el.multiple?'listbox':'combobox', summary:'button',
        input:({checkbox:'checkbox',radio:'radio',range:'slider',number:'spinbutton',
            button:'button',submit:'button',reset:'button',search:'searchbox'})[type] || 'textbox'};
    const role = el.getAttribute('role') || implicit[tag] || (el.isContentEditable?'textbox':'generic');
    const labels = Array.from(el.labels || []).map(label => label.textContent).join(' ');
    const name = clean(clean(labelled) || el.getAttribute('aria-label') || clean(labels) || el.getAttribute('alt') ||
        (['button','submit','reset'].includes(type)?el.value:'') ||
        (['input','textarea','select'].includes(tag)?'':el.textContent) ||
        el.getAttribute('title') || el.getAttribute('placeholder') || el.querySelector('img[alt]')?.getAttribute('alt'));
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const visible = el.isConnected && rect.width > 0 && rect.height > 0 &&
        el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) &&
        style.visibility !== 'hidden' && style.visibility !== 'collapse' && style.display !== 'none' &&
        style.opacity !== '0' && type !== 'hidden' && !el.closest('[inert]');
    const in_viewport = rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
    // Test a point within the visible portion, then pierce open shadow roots.
    const x = (Math.max(0,rect.left)+Math.min(innerWidth,rect.right))/2;
    const y = (Math.max(0,rect.top)+Math.min(innerHeight,rect.bottom))/2;
    let hit = in_viewport ? document.elementFromPoint(x,y) : null;
    while (hit?.shadowRoot) {
        const deeper = hit.shadowRoot.elementFromPoint(x,y);
        if (!deeper || deeper === hit) break;
        hit = deeper;
    }
    let covered = in_viewport;
    for (let current=hit; current; current=current.parentNode || current.host) {
        if (current === el) { covered=false; break; }
    }
    const disabled = !!el.disabled || el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true';
    return {role, name:name.slice(0,80), tag, type, visible, in_viewport, covered, disabled,
        checked:['checkbox','radio'].includes(type)?el.checked:
            (el.hasAttribute('aria-checked')?el.getAttribute('aria-checked')==='true':null),
        value:(type === 'password' || /^(cc-|transaction-)/i.test(el.autocomplete || '') ||
            /password|secret|token|card|cvv|cvc/i.test(el.name || el.id || '')) ? null :
            (['input','textarea','select'].includes(tag)?el.value.slice(0,40):null),
        bbox:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
        identity:JSON.stringify([tag,role,name.slice(0,500),type,el.id,el.getAttribute('href'),
            el.getAttribute('formaction'),el.getAttribute('onclick'),el.getAttribute('contenteditable')])};
}"""

COLLECT = """({scope, limit}) => {
    const inspect = INSPECT;
    const selector = 'a[href],button,input,textarea,select,summary,[role],[onclick],[tabindex],[contenteditable]';
    const roles = new Set(['button','link','tab','menuitem','menuitemcheckbox','menuitemradio',
        'checkbox','radio','textbox','searchbox','combobox','listbox','option','switch','slider','spinbutton','treeitem']);
    const elements=[], info=[];
    let above=0, below=0, covered=0, visited=0, truncated=false;
    const walk = root => {
        for (const el of root.querySelectorAll('*')) {
            if (++visited > 12000) { truncated=true; return; }
            if (el.matches(selector) && (el.matches('a[href],button,input,textarea,select,summary,[onclick]') ||
                roles.has(el.getAttribute('role')) || el.tabIndex >= 0 || el.isContentEditable)) {
                const data=inspect(el);
                if (data.visible) {
                    if (data.bbox.y + data.bbox.height <= 0) above++;
                    else if (data.bbox.y >= innerHeight) below++;
                    if (data.in_viewport && data.covered) covered++;
                    if ((scope === 'page' || data.in_viewport) && !data.covered) {
                        if (elements.length < limit) { elements.push(el); info.push(data); }
                        else truncated=true;
                    }
                }
            }
            if (el.shadowRoot) walk(el.shadowRoot);
        }
    };
    walk(document);
    return {elements, meta:{info, above, below, covered, truncated}};
}""".replace("INSPECT", INSPECT)


@dataclass
class Reference:
    handle: Any
    info: dict
    frame_id: str


@dataclass
class Snapshot:
    snapshot_id: str
    session_id: str
    generation: int
    tab_id: str
    refs: dict[int, Reference] = field(default_factory=dict)
    valid: bool = True

    async def dispose(self):
        self.valid = False
        for reference in self.refs.values():
            with suppress(PlaywrightError):
                await reference.handle.dispose()
        self.refs.clear()


class ReferenceRegistry:
    def __init__(self):
        self._snapshots: OrderedDict[str, Snapshot] = OrderedDict()
        self._counter = 0

    def invalidate(self, tab_id: str):
        if tab_id in self._snapshots:
            self._snapshots[tab_id].valid = False

    async def clear(self):
        for snapshot in self._snapshots.values():
            await snapshot.dispose()
        self._snapshots.clear()

    async def create(self, session_id: str, generation: int, tab_id: str) -> Snapshot:
        for old_tab, snapshot in list(self._snapshots.items()):
            if not snapshot.valid or old_tab == tab_id:
                await snapshot.dispose()
                del self._snapshots[old_tab]
        while len(self._snapshots) >= 8:
            _, oldest = self._snapshots.popitem(last=False)
            await oldest.dispose()
        self._counter += 1
        snapshot = Snapshot(f"s{self._counter}-{session_id[:12]}", session_id, generation, tab_id)
        self._snapshots[tab_id] = snapshot
        return snapshot

    async def resolve(self, session, snapshot_id: str, ref: int):
        snapshot = self._snapshots.get(session.tabs.active)
        if (snapshot is None or not snapshot.valid or snapshot.snapshot_id != snapshot_id or
                snapshot.session_id != session.session_id or snapshot.generation != session.generation):
            raise BrowserError("STALE_REF", "Snapshot does not match this active tab/session; call browser_observe")
        reference = snapshot.refs.get(ref)
        if reference is None:
            raise BrowserError("INVALID_REF", "Reference was not reported in this snapshot; call browser_observe")
        try:
            current = await reference.handle.evaluate(INSPECT)
        except PlaywrightError as exc:
            raise BrowserError("STALE_REF", "Target document changed; call browser_observe") from exc
        if not current["visible"] or current["identity"] != reference.info["identity"]:
            raise BrowserError("STALE_REF", "Target was removed, hidden, or changed identity; call browser_observe")
        if current["disabled"] or current["covered"]:
            raise BrowserError("TARGET_NOT_ACTIONABLE", "Target is disabled or covered; call browser_observe")
        frame = await reference.handle.owner_frame()
        try:
            while frame and frame.parent_frame:
                host = await frame.frame_element()
                try:
                    geometry = await host.evaluate(INSPECT)
                    if not geometry["visible"] or geometry["covered"]:
                        raise BrowserError("TARGET_NOT_ACTIONABLE", "Target frame is hidden or covered; call browser_observe")
                finally:
                    await host.dispose()
                frame = frame.parent_frame
        except PlaywrightError as exc:
            raise BrowserError("STALE_REF", "Target frame changed; call browser_observe") from exc
        return reference.handle


async def discover(page, *, scope: str, limit: int = 120):
    """Walk supported frames, retaining handles but never mutating the document."""
    found: list[Reference] = []
    try:
        return await _discover(page, scope=scope, limit=limit, found=found)
    except BaseException:
        for reference in found:
            with suppress(PlaywrightError):
                await reference.handle.dispose()
        raise


async def _discover(page, *, scope, limit, found):
    counts = {"above": 0, "below": 0, "covered": 0}
    warnings = []
    viewport = await page.evaluate("() => ({width:innerWidth,height:innerHeight})")
    for index, frame in enumerate(page.frames[:16], 1):
        if len(found) >= limit:
            warnings.append("Element limit reached")
            break
        batch = array = None
        try:
            offset = {"x": 0, "y": 0}
            if frame.parent_frame:
                ancestor = frame
                while ancestor.parent_frame:
                    host = await ancestor.frame_element()
                    try:
                        geometry = await host.evaluate(INSPECT)
                        if not geometry["visible"] or geometry["covered"] or (
                                scope == "viewport" and not geometry["in_viewport"]):
                            break
                    finally:
                        await host.dispose()
                    ancestor = ancestor.parent_frame
                else:
                    ancestor = None
                if ancestor is not None:
                    continue
                host = await frame.frame_element()
                try:
                    offset = await host.bounding_box() or offset
                finally:
                    await host.dispose()
            batch = await frame.evaluate_handle(COLLECT, {"scope": scope, "limit": limit - len(found)})
            meta = await batch.evaluate("batch => batch.meta")
            for key in counts:
                counts[key] += meta[key]
            if meta["truncated"]:
                warnings.append(f"f{index}: discovery limit reached")
            array = await batch.get_property("elements")
            properties = await array.get_properties()
            for key, handle in properties.items():
                info = meta["info"][int(key)]
                info["bbox"]["x"] += offset["x"]
                info["bbox"]["y"] += offset["y"]
                box = info["bbox"]
                info["in_viewport"] = info["in_viewport"] and (
                    box["y"] < viewport["height"] and box["y"] + box["height"] > 0 and
                    box["x"] < viewport["width"] and box["x"] + box["width"] > 0
                )
                found.append(Reference(handle.as_element(), info, f"f{index}"))
        except PlaywrightError:
            warnings.append(f"f{index}: frame unavailable")
        finally:
            if array:
                await array.dispose()
            if batch:
                await batch.dispose()
    if len(page.frames) > 16:
        warnings.append("Frame limit reached")
    found.sort(key=lambda item: (not item.info["in_viewport"], round(item.info["bbox"]["y"] / 8),
                                 item.info["bbox"]["x"]))
    return found, counts, warnings
