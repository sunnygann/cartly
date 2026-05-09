"""
Cold Storage scraper.
Extracts product data from the __NEXT_DATA__ JSON payload embedded in the page,
which contains accurate price/promoPrice/discountLabel fields — no DOM walking needed.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"

# Extract initialProducts from __NEXT_DATA__ JSON embedded in the page.
# Falls back to scanning other script tags if the path differs.
_EXTRACT_JS = r"""() => {
    const nextEl = document.getElementById('__NEXT_DATA__');
    if (nextEl) {
        try {
            const data = JSON.parse(nextEl.textContent);
            const pp = data?.props?.pageProps;
            if (Array.isArray(pp?.initialProducts)) return pp.initialProducts;
            if (Array.isArray(pp?.products))        return pp.products;
        } catch(e) {}
    }
    // Fallback: scan all inline scripts for the initialProducts array
    for (const s of document.querySelectorAll('script:not([src])')) {
        const t = s.textContent;
        if (!t.includes('initialProducts')) continue;
        try {
            const m = t.match(/"initialProducts"\s*:\s*(\[[\s\S]*?\])\s*[,}]/);
            if (m) return JSON.parse(m[1]);
        } catch(e) {}
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

    print(f"[cold] JSON extracted {len(raw)} products")

    products = []
    for item in raw[:limit]:
        name = (item.get("name") or "").strip()
        if not name:
            continue

        regular = item.get("price")        # always present
        promo   = item.get("promoPrice")   # null when no active promo

        if regular is None:
            continue

        current_price   = float(promo)    if promo    else float(regular)
        original_price  = float(regular)  if promo    else None
        promo_text      = item.get("discountLabel") or None   # e.g. "10% off"

        # Image: prefer direct CDN URL over Next.js proxy
        image = item.get("image") or ""

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
