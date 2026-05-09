"""
Cold Storage scraper.
Next.js App Router streams RSC data as self.__next_f.push(...) script tags.
We use Playwright to receive the full streamed page, then scan all script
elements in the DOM for the initialProducts JSON array using a bracket-counter.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"

# Scans all inline script elements for "initialProducts":[ and extracts the
# array using bracket-counting (safe against nested structures).
_EXTRACT_JS = r"""() => {
    const marker = '"initialProducts":';
    for (const script of document.querySelectorAll('script')) {
        const text = script.textContent || '';
        const idx = text.indexOf(marker);
        if (idx === -1) continue;

        const arrStart = text.indexOf('[', idx + marker.length);
        if (arrStart === -1) continue;

        let depth = 0;
        for (let i = arrStart; i < text.length; i++) {
            if (text[i] === '[') depth++;
            else if (text[i] === ']') {
                depth--;
                if (depth === 0) {
                    try { return JSON.parse(text.slice(arrStart, i + 1)); }
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
        name = (item.get("name") or "").strip()
        if not name:
            continue

        regular = item.get("price")      # shelf/regular price
        promo   = item.get("promoPrice") # active sale price, or null

        if regular is None:
            continue

        current_price  = float(promo)   if promo else float(regular)
        original_price = float(regular) if promo else None
        promo_text     = item.get("discountLabel") or None  # e.g. "10% off"
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
