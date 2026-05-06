"""
Don Don Donki scraper.
Donki SG has no direct online store. Products are available via GrabMart
at their merchant page. We navigate to the merchant, search within it,
and extract prices from the rendered DOM.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _EXTRACT_JS, _UA

_MERCHANT_URL = "https://mart.grab.com/sg/en/merchant/4-C4CKC8NAVK2ETJ"


async def search_donki(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
        )
        page = await ctx.new_page()

        try:
            await page.goto(_MERCHANT_URL, wait_until="load", timeout=30_000)
            await asyncio.sleep(3)
        except Exception as exc:
            print(f"[donki] page load failed: {exc}")
            await browser.close()
            return []

        # Try to find and use search input within the merchant page
        search_sel = "input[type='search'], input[placeholder*='search' i], input[placeholder*='Search' i]"
        search_input = await page.query_selector(search_sel)
        if search_input:
            await search_input.click()
            await search_input.fill(query)
            await search_input.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await asyncio.sleep(2)

        title = await page.title()
        print(f"[donki] loaded: {title} | {page.url}")

        raw = await page.evaluate(_EXTRACT_JS)
        print(f"[donki] DOM extracted {len(raw)} price nodes")
        await browser.close()

    products = []
    q_words = [w for w in query.lower().split() if len(w) > 2]
    for item in raw[:limit * 2]:
        name  = (item.get("name") or "").strip()
        price = item.get("price")
        if not name or not price:
            continue
        if q_words and not any(w in name.lower() for w in q_words):
            continue
        products.append({
            "name": name, "brand": "", "price": float(price),
            "original_price": None, "promo": None, "unit": "",
            "image": item.get("image", ""), "barcode": None,
            "category": "", "store": "donki",
            "scraped_at": datetime.utcnow(),
        })
        if len(products) >= limit:
            break

    print(f"[donki] parsed {len(products)} products")
    return products
