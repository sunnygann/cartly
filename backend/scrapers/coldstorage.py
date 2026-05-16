"""
Cold Storage scraper.
Uses two sources:
  1. window.__next_f RSC stream (initialProducts) — 30 in-stock items pre-rendered on load.
  2. RSC fetch responses during scroll — additional items loaded lazily.
Products deduplicated by productId. inventoryStatus field used for sold-out detection.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA, block_resources

_URL = "https://www.coldstorage.com.sg/search?q={}"
_MAX_SCROLLS = 50

_EXTRACT_JS = r"""() => {
    for (const entry of (window.__next_f || [])) {
        if (!Array.isArray(entry) || typeof entry[1] !== 'string') continue;
        const content = entry[1];
        const marker = '"initialProducts":';
        const idx = content.indexOf(marker);
        if (idx === -1) continue;

        const arrStart = content.indexOf('[', idx + marker.length);
        if (arrStart === -1) continue;

        let depth = 0;
        for (let i = arrStart; i < content.length; i++) {
            if (content[i] === '[') depth++;
            else if (content[i] === ']') {
                depth--;
                if (depth === 0) {
                    try { return JSON.parse(content.slice(arrStart, i + 1)); }
                    catch (e) { return []; }
                }
            }
        }
    }
    return [];
}"""


def _find_products(obj) -> list | None:
    """Recursively search for a 'products' key containing a non-empty list."""
    if isinstance(obj, dict):
        v = obj.get("products")
        if isinstance(v, list) and v:
            return v
        for val in obj.values():
            found = _find_products(val)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_products(item)
            if found is not None:
                return found
    return None


_T_PREFIX = re.compile(r'^T[0-9a-fA-F]+,')


def _parse_scroll_response(body: bytes) -> list[dict]:
    """Extract products from an RSC fetch response body."""
    results = []
    try:
        text = body.decode("utf-8", "replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line or '"products"' not in line:
                continue
            # Strip RSC chunk-ID prefix "N:"
            colon_idx = line.find(":")
            if colon_idx == -1:
                continue
            payload = line[colon_idx + 1:]
            # Handle Next.js RSC text-blob format "Tlen," or "T0xlen,"
            if _T_PREFIX.match(payload):
                payload = payload[payload.index(",") + 1:]
            if not payload:
                continue
            # Find first JSON start character
            if payload[0] not in ('{', '['):
                for ch in ('{', '['):
                    idx = payload.find(ch)
                    if idx != -1:
                        payload = payload[idx:]
                        break
                else:
                    continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            products = _find_products(data)
            if products:
                results.extend(products)
    except Exception:
        pass
    return results


async def search_coldstorage(query: str, browser=None) -> list[dict]:
    collected: list[dict] = []
    seen_ids: set = set()
    pending_responses: list = []

    own_browser = browser is None
    _pw = None
    if own_browser:
        _pw = await async_playwright().start()
        browser = await _pw.chromium.launch(headless=True)

    ctx = await browser.new_context(
        user_agent=_UA,
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 900},
    )
    pg = await ctx.new_page()
    await pg.route("**/*", block_resources)

    rsc_event = asyncio.Event()

    async def handle_response(resp):
        if "coldstorage.com.sg" not in resp.url or resp.status != 200:
            return
        ct = resp.headers.get("content-type", "")
        # Capture RSC/JSON/text responses; skip obvious non-data types
        if any(x in ct for x in ("html",)) and "products" not in resp.url:
            return
        try:
            body = await resp.body()
            if b'"products"' in body:
                pending_responses.append(body)
                rsc_event.set()
        except Exception:
            pass

    pg.on("response", handle_response)

    try:
        url = _URL.format(query)
        await pg.goto(url, wait_until="domcontentloaded", timeout=28_000)

        # Wait for initial RSC stream
        try:
            await pg.wait_for_function(
                """() => (window.__next_f || []).some(
                    e => Array.isArray(e) && typeof e[1] === 'string'
                      && e[1].includes('"initialProducts"')
                )""",
                timeout=4_000,
            )
        except Exception:
            pass

        title = await pg.title()
        print(f"[cold] loaded: {title} | {pg.url}")

        # Source 1: initial RSC stream
        initial_raw = await pg.evaluate(_EXTRACT_JS)
        for item in initial_raw:
            pid = item.get("productId")
            if pid not in seen_ids:
                seen_ids.add(pid)
                collected.append(item)
        print(f"[cold] initial RSC: {len(initial_raw)} items")

        # Source 2: scroll to trigger lazy-loading.
        # Use JS-driven scroll that properly fires IntersectionObserver.
        # After each RSC event, wait 300ms for DOM to render before re-reading height.
        scroll_height = await pg.evaluate("() => document.body.scrollHeight")
        current_y = 0
        consecutive_no_rsc = 0

        for scroll_n in range(_MAX_SCROLLS):
            rsc_event.clear()
            prev_count = len(pending_responses)

            # Advance scroll position; overshoot slightly to ensure sentinel enters viewport
            next_y = current_y + 900
            await pg.evaluate(f"window.scrollTo({{top: {next_y}, behavior: 'instant'}})")
            current_y = next_y

            try:
                await asyncio.wait_for(rsc_event.wait(), timeout=3.0)
                # Wait for DOM to render new items before measuring new height
                await asyncio.sleep(0.3)
                scroll_height = await pg.evaluate("() => document.body.scrollHeight")
                consecutive_no_rsc = 0
                print(f"[cold] scroll {scroll_n + 1}: RSC received, height={scroll_height}")
            except asyncio.TimeoutError:
                consecutive_no_rsc += 1
                if consecutive_no_rsc >= 6:
                    print(f"[cold] no RSC for 6 consecutive scrolls at y={current_y}, stopping")
                    break
                # Re-read height in case page grew without triggering our event
                new_h = await pg.evaluate("() => document.body.scrollHeight")
                if new_h != scroll_height:
                    scroll_height = new_h
                    consecutive_no_rsc = 0

            # If we've scrolled past the page, scroll to actual bottom once more
            if current_y > scroll_height:
                await pg.evaluate(f"window.scrollTo({{top: {scroll_height}, behavior: 'instant'}})")

        print(f"[cold] scroll done: {len(pending_responses)} RSC bodies captured")

    except Exception as exc:
        print(f"[cold] error: {exc}")
    finally:
        await ctx.close()
        if own_browser and _pw:
            await browser.close()
            await _pw.stop()

    # Merge all scroll-triggered RSC responses
    for body in pending_responses:
        items = _parse_scroll_response(body)
        for item in items:
            pid = item.get("productId")
            if pid not in seen_ids:
                seen_ids.add(pid)
                collected.append(item)

    print(f"[cold] total collected: {len(collected)} products")

    products = []
    for item in collected:
        name    = (item.get("name") or "").strip()
        regular = item.get("price")
        promo   = item.get("promoPrice")

        if not name or regular is None:
            continue

        inventory = (item.get("inventoryStatus") or "").lower()
        sold_out  = bool(inventory) and inventory not in ("in stock", "available")

        if sold_out:
            current_price  = float(regular)
            original_price = None
        else:
            current_price  = float(promo) if promo else float(regular)
            original_price = float(regular) if promo else None
        promo_text = "SOLD OUT" if sold_out else (item.get("discountLabel") or None)

        products.append({
            "name":           name,
            "brand":          "",
            "price":          current_price,
            "original_price": original_price,
            "promo":          promo_text,
            "unit":           "",
            "image":          item.get("image") or "",
            "barcode":        None,
            "category":       "",
            "store":          "cold",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[cold] parsed {len(products)} products")
    return products
