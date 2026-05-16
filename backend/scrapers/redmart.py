"""
RedMart scraper.
RedMart is hosted on Lazada SG. The hash-based search URL
(search/#q=...) returns random page products, not search results.
Use the catalog query-param URL instead, which properly filters by term.
Results are post-filtered to keep only products whose names contain at
least one meaningful word from the search query.
"""
import asyncio
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from ._base import _EXTRACT_JS, _UA

# Proper query-param URL loads real search results; hash URL is a fallback only
_URLS = [
    "https://www.lazada.sg/catalog/?q={}&from=input&seller_type=official",
    "https://redmart.lazada.sg/catalog/?q={}",
    "https://redmart.lazada.sg/search/#q={}&from=input",
]

# Words so common they can't tell us whether a product is relevant
_SKIP_WORDS = {
    "the", "and", "for", "with", "per", "from", "each", "in", "of",
    "a", "an", "to", "at", "is", "it",
}


def _is_relevant(name: str, query: str) -> bool:
    """Return True if the product name contains at least one query word."""
    name_l  = name.lower()
    q_words = [w for w in query.lower().split() if len(w) > 2 and w not in _SKIP_WORDS]
    if not q_words:
        return True  # can't tell — keep it
    return any(w in name_l for w in q_words)


async def search_redmart(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
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
                # Lazada is heavy — wait for product cards to appear
                try:
                    await page.wait_for_selector(
                        "[class*='product' i], [class*='item' i], [data-sku]",
                        timeout=10_000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(3)
            except Exception as exc:
                print(f"[red] {url} failed: {exc}")
                continue

            title = await page.title()
            print(f"[red] loaded: {title} | {page.url}")
            raw = await page.evaluate(_EXTRACT_JS)
            print(f"[red] DOM extracted {len(raw)} price nodes")

            # Only keep products whose names actually mention the query
            relevant = [r for r in raw if _is_relevant(r.get("name", ""), query)]
            print(f"[red] relevant after filter: {len(relevant)}")
            if relevant:
                raw = relevant
                break
            # If no relevant results, try next URL
            raw = []

        await browser.close()

    products = []
    for item in raw:
        name  = (item.get("name") or "").strip()
        price = item.get("price")
        if not name or not price:
            continue
        orig = item.get("original_price")
        products.append({
            "name": name, "brand": "", "price": float(price),
            "original_price": float(orig) if orig else None, "promo": item.get("promo") or None, "unit": "",
            "image": item.get("image", ""), "barcode": None,
            "category": "", "store": "red",
            "scraped_at": datetime.utcnow(),
        })

    print(f"[red] parsed {len(products)} products")
    return products
