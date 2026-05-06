"""
Giant scraper.
Giant.sg uses Algolia InstantSearch (app PFCHI1YM66, index giant_product_live).
Product data is fully client-side rendered via Algolia — there is NO server-side
product data in the HTML. The Algolia cluster DNS is geo-restricted to Singapore
ISP networks; it returns NXDOMAIN from public DNS resolvers globally.

This scraper works correctly when the backend is deployed on a Singapore server
(e.g. DigitalOcean SGP1, Render Singapore). Locally it returns empty results.
"""
import asyncio
import socket
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_ALGOLIA_APP_ID = "PFCHI1YM66"
_ALGOLIA_API_KEY = "d0c09a40111717aec861992cf8497e71"
_ALGOLIA_INDEX   = "giant_product_live"

# Confirm DNS resolves before launching Playwright (saves 30s on failure)
def _algolia_reachable() -> bool:
    try:
        socket.getaddrinfo(f"{_ALGOLIA_APP_ID}-dsn.algolia.net", 443)
        return True
    except OSError:
        return False


# Called in-browser via page.evaluate(); only reaches here if DNS resolves.
_ALGOLIA_FETCH_JS = f"""async () => {{
    var url = 'https://{_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query';
    var resp = await fetch(url, {{
        method: 'POST',
        headers: {{
            'X-Algolia-Application-Id': '{_ALGOLIA_APP_ID}',
            'X-Algolia-API-Key': '{_ALGOLIA_API_KEY}',
            'Content-Type': 'application/json'
        }},
        body: JSON.stringify({{ query: GIANT_QUERY, hitsPerPage: 25 }})
    }});
    var data = await resp.json();
    return (data.hits || []).map(function(h) {{
        return {{
            name: h.name || '',
            price: h.price || 0,
            image: h.image_url || '',
            brand: h.brand_name || '',
            size: h.size || ''
        }};
    }});
}}"""


async def search_giant(query: str, limit: int = 20) -> list[dict]:
    if not _algolia_reachable():
        print("[giant] Algolia DNS not reachable from this network — deploy to Singapore for Giant results")
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://giant.sg/", wait_until="load", timeout=20_000)
        except Exception as exc:
            print(f"[giant] page load failed: {exc}")
            await browser.close()
            return []

        # Inject query variable then call Algolia via in-page fetch
        js = _ALGOLIA_FETCH_JS.replace("GIANT_QUERY", f'"{query}"')
        try:
            hits = await page.evaluate(js)
        except Exception as exc:
            print(f"[giant] Algolia fetch failed: {exc}")
            await browser.close()
            return []

        await browser.close()

    print(f"[giant] Algolia returned {len(hits)} hits")
    products, seen = [], set()
    for h in hits:
        name  = (h.get("name") or "").strip()
        price = h.get("price") or 0
        if not name or not price or name in seen:
            continue
        seen.add(name)
        products.append({
            "name": name, "brand": h.get("brand", ""), "price": float(price),
            "original_price": None, "promo": None, "unit": h.get("size", ""),
            "image": h.get("image", ""), "barcode": None,
            "category": "", "store": "giant",
            "scraped_at": datetime.utcnow(),
        })
        if len(products) >= limit:
            break

    print(f"[giant] parsed {len(products)} products")
    return products
