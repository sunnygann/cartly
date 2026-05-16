"""
Cold Storage scraper.
Uses two sources:
  1. window.__next_f RSC stream (initialProducts) — 30 in-stock items pre-rendered on load.
  2. RSC fetch responses during scroll — additional items (including sold-out) loaded lazily.
Products deduplicated by productId. inventoryStatus field used for sold-out detection.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
import json
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"

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


def _parse_scroll_response(body: bytes) -> list[dict]:
    """Extract products from an RSC fetch response body (scroll-loaded items)."""
    results = []
    try:
        text = body.decode("utf-8", "replace")
        for line in text.split("\n"):
            if not line or ":" not in line or '"products"' not in line:
                continue
            colon_idx = line.index(":")
            payload = line[colon_idx + 1:]
            if not payload.startswith("{"):
                continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("products"), list):
                results.extend(data["products"])
    except Exception:
        pass
    return results


async def search_coldstorage(query: str) -> list[dict]:
    collected: list[dict] = []
    seen_ids: set = set()
    pending_responses: list = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        pg = await ctx.new_page()

        async def handle_response(resp):
            if "coldstorage.com.sg/search" not in resp.url or resp.status != 200:
                return
            ct = resp.headers.get("content-type", "")
            if not any(x in ct for x in ("x-component", "text/plain", "application/json")):
                return
            try:
                body = await resp.body()
                pending_responses.append(body)
            except Exception:
                pass

        pg.on("response", handle_response)

        url = _URL.format(query)
        try:
            await pg.goto(url, wait_until="load", timeout=28_000)
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"[cold] page load error: {exc}")
            await ctx.close()
            await browser.close()
            return []

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

        # Scroll to trigger lazy-loading of additional items (sold-out etc.)
        await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)

        await ctx.close()
        await browser.close()

    # Source 2: scroll-triggered RSC fetch responses
    for body in pending_responses:
        for item in _parse_scroll_response(body):
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

        current_price  = float(promo)   if promo else float(regular)
        original_price = float(regular) if promo else None
        promo_text     = "SOLD OUT" if sold_out else (item.get("discountLabel") or None)
        image          = item.get("image") or ""

        products.append({
            "name":           name,
            "brand":          "",
            "price":          current_price,
            "original_price": original_price,
            "promo":          promo_text,
            "unit":           "",
            "image":          image,
            "barcode":        None,
            "category":       "",
            "store":          "cold",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[cold] parsed {len(products)} products")
    return products
