"""
Don Don Donki scraper.
Donki SG has no direct online store. We scrape their Lazada SG brand page
which lists their products with prices.
"""
import asyncio
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from ._base import _EXTRACT_JS, _UA

_URLS = [
    "https://www.lazada.sg/catalog/?q=don+don+donki+{}&from=input",
    "https://www.lazada.sg/catalog/?q={}&from=input&seller_type=official&brand=don-don-donki",
]

_SKIP_WORDS = {"the","and","for","with","per","from","each","in","of","a","an","to","at","is","it"}


def _is_relevant(name: str, query: str) -> bool:
    name_l  = name.lower()
    q_words = [w for w in query.lower().split() if len(w) > 2 and w not in _SKIP_WORDS]
    if not q_words:
        return True
    return any(w in name_l for w in q_words)


async def search_donki(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
        )
        page = await ctx.new_page()

        raw = []
        for url_tmpl in _URLS:
            url = url_tmpl.format(quote_plus(query))
            try:
                await page.goto(url, wait_until="load", timeout=30_000)
                try:
                    await page.wait_for_selector("[class*='product' i], [class*='item' i]", timeout=8_000)
                except Exception:
                    pass
                await asyncio.sleep(3)
            except Exception as exc:
                print(f"[donki] {url} failed: {exc}")
                continue

            title = await page.title()
            print(f"[donki] loaded: {title} | {page.url}")
            raw = await page.evaluate(_EXTRACT_JS)
            print(f"[donki] DOM extracted {len(raw)} price nodes")

            relevant = [r for r in raw if _is_relevant(r.get("name", ""), query)]
            print(f"[donki] relevant after filter: {len(relevant)}")
            if relevant:
                raw = relevant
                break
            raw = []

        await browser.close()

    products = []
    for item in raw[:limit]:
        name  = (item.get("name") or "").strip()
        price = item.get("price")
        if not name or not price:
            continue
        products.append({
            "name": name, "brand": "", "price": float(price),
            "original_price": None, "promo": None, "unit": "",
            "image": item.get("image", ""), "barcode": None,
            "category": "", "store": "donki",
            "scraped_at": datetime.utcnow(),
        })

    print(f"[donki] parsed {len(products)} products")
    return products
