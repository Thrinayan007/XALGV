"""Bounded page-state evidence. A fingerprint is not a task-completion verdict."""

import hashlib
import json

from playwright.async_api import Error as PlaywrightError

CAPTURE = """() => {
        const root = document.scrollingElement || document.documentElement;
        const hash = text => {
            let h = 2166136261;
            for (let i=0; i<text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
            return (h >>> 0).toString(16);
        };
        const roots = [document];
        // Include open shadow trees while bounding traversal and evidence size.
        for (let index=0; index<roots.length && roots.length<32; index++) {
            for (const el of Array.from(roots[index].querySelectorAll('*')).slice(0,12000)) {
                if (el.shadowRoot && roots.length<32) roots.push(el.shadowRoot);
            }
        }
        const controls = roots.flatMap(root => Array.from(root.querySelectorAll('input,textarea,select'))).slice(0,200)
            .map(el => [el.type, el.type === 'password' || /^(cc-|transaction-)/i.test(el.autocomplete || '') ? '' : el.value.slice(0,100), el.checked]);
        const shadows = roots.slice(1).map(root => root.textContent.slice(0,2000)).join('');
        const scrolls = roots.flatMap(root => Array.from(root.querySelectorAll('*')).slice(0,12000))
            .filter(el => el.scrollTop || el.scrollLeft).slice(0,100).map(el => [el.scrollLeft,el.scrollTop]);
        return {url:location.href, title:document.title, x:scrollX, y:scrollY,
            height:innerHeight, width:innerWidth, scroll_height:root.scrollHeight,
            at_top:scrollY <= 1, at_bottom:scrollY + innerHeight >= root.scrollHeight - 1,
            dom:hash((document.body?.innerText || '').slice(0,30000) + shadows + JSON.stringify(controls) + JSON.stringify(scrolls))};
    }"""


async def capture_state(page) -> dict:
    data = await page.evaluate(CAPTURE)
    frames = []
    for frame in page.frames[1:16]:
        try:
            frames.append(await frame.evaluate(CAPTURE))
        except PlaywrightError:
            frames.append({"url": frame.url, "unavailable": True})
    data["frames"] = frames
    data["fingerprint"] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
    return data
