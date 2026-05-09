"""
Cold Storage scraper.
The site uses Next.js App Router RSC streaming. Product data is pushed into
window.__next_f as executed JS. We read entry[1] (the unescaped string) from
each push entry and bracket-count to extract initialProducts JSON cleanly.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"

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


async def search_coldstorage(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        url = _URL.format(query)
        try:
            await page.goto(url, wait_until="load", timeout=28_000)
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"[cold] page load error: {exc}")
            await browser.close()
            return []

        title = await page.title()
        print(f"[cold] loaded: {title} | {page.url}")

        raw = await page.evaluate(_EXTRACT_JS)
        await browser.close()

    print(f"[cold] RSC extracted {len(raw)} products")

    products = []
    for item in raw[:limit]:
        name    = (item.get("name") or "").strip()
        regular = item.get("price")       # shelf/regular price
        promo   = item.get("promoPrice")  # active sale price, or null

        if not name or regular is None:
            continue

        current_price  = float(promo)   if promo else float(regular)
        original_price = float(regular) if promo else None
        promo_text     = item.get("discountLabel") or None
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
