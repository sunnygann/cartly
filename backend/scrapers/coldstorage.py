"""
Cold Storage scraper.
The site uses Next.js App Router RSC streaming. Product data is pushed into
window.__next_f as executed JS. We read entry[1] (the unescaped string) from
each push entry and bracket-count to extract initialProducts JSON cleanly.
SSL certificate is expired — we pass ignore_https_errors=True.
Paginates up to 4 pages (80 results) to capture all search results.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_BASE_URL = "https://www.coldstorage.com.sg/search?q={}&page={}"

_EXTRACT_JS = r"""() => {
    // window.__next_f holds executed RSC push entries.
    // entry[1] is the already-unescaped string, so "initialProducts": is literal.
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

_SOLD_OUT_FIELDS = ("soldOut", "isSoldOut")
_IN_STOCK_FIELDS = ("inStock", "isAvailable", "available")
_OUT_OF_STOCK_VALUES = ("OutOfStock", "SOLD_OUT", "out_of_stock", "NOT_AVAILABLE")


def _is_sold_out(item: dict) -> bool:
    for field in _SOLD_OUT_FIELDS:
        if item.get(field):
            return True
    for field in _IN_STOCK_FIELDS:
        val = item.get(field)
        if val is not None and val is False:
            return True
    if item.get("availability") in _OUT_OF_STOCK_VALUES:
        return True
    return False


async def search_coldstorage(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        all_raw = []
        page_num = 1
        max_pages = 4

        while page_num <= max_pages:
            ctx = await browser.new_context(
                user_agent=_UA,
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 900},
            )
            pg = await ctx.new_page()

            url = _BASE_URL.format(query, page_num)
            try:
                await pg.goto(url, wait_until="load", timeout=28_000)
                await asyncio.sleep(2)
            except Exception as exc:
                print(f"[cold] page {page_num} load error: {exc}")
                await ctx.close()
                break

            title = await pg.title()
            print(f"[cold] page {page_num}: {title} | {pg.url}")

            raw = await pg.evaluate(_EXTRACT_JS)
            await ctx.close()

            print(f"[cold] page {page_num}: {len(raw)} items")

            if not raw:
                break

            all_raw.extend(raw)

            if len(raw) < 20:
                break

            page_num += 1

        await browser.close()

    print(f"[cold] RSC extracted {len(all_raw)} products total")

    products = []
    for item in all_raw:
        name    = (item.get("name") or "").strip()
        regular = item.get("price")
        promo   = item.get("promoPrice")

        if not name or regular is None:
            continue

        sold_out       = _is_sold_out(item)
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
