"""
Cold Storage scraper.
Their SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _EXTRACT_JS, _UA

_URLS = [
    "https://www.coldstorage.com.sg/search?q={}",
    "https://www.coldstorage.com.sg/search/?q={}",
    "https://www.coldstorage.com.sg/catalogsearch/result/?q={}",
]


async def search_coldstorage(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            ignore_https_errors=True,          # expired SSL cert
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        raw = []
        for url_tmpl in _URLS:
            url = url_tmpl.format(query)
            try:
                await page.goto(url, wait_until="load", timeout=28_000)
                await asyncio.sleep(2)
            except Exception as exc:
                print(f"[cold] {url} failed: {exc}")
                continue

            title = await page.title()
            print(f"[cold] loaded: {title} | {page.url}")
            raw = await page.evaluate(_EXTRACT_JS)
            print(f"[cold] DOM extracted {len(raw)} price nodes")
            if raw:
                break

        await browser.close()

    products = []
    for item in raw[:limit]:
        name  = (item.get("name") or "").strip()
        price = item.get("price")
        if not name or not price:
            continue
        orig = item.get("original_price")
        products.append({
            "name": name, "brand": "", "price": float(price),
            "original_price": float(orig) if orig else None, "promo": None, "unit": "",
            "image": item.get("image", ""), "barcode": None,
            "category": "", "store": "cold",
            "scraped_at": datetime.utcnow(),
        })

    print(f"[cold] parsed {len(products)} products")
    return products
