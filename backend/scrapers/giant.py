"""
Giant scraper.
Giant.sg uses Algolia InstantSearch (app PFCHI1YM66, index giant_product_live).
Calls Algolia directly via httpx using fallback nodes (-1/-2/-3.algolianet.com)
which are not geo-restricted, bypassing the DSN DNS restriction entirely.
"""
import httpx
from datetime import datetime

_ALGOLIA_APP_ID = "PFCHI1YM66"
_ALGOLIA_API_KEY = "d0c09a40111717aec861992cf8497e71"
_ALGOLIA_INDEX   = "giant_product_live"

_ALGOLIA_HOSTS = [
    f"{_ALGOLIA_APP_ID}-dsn.algolia.net",
    f"{_ALGOLIA_APP_ID}-1.algolianet.com",
    f"{_ALGOLIA_APP_ID}-2.algolianet.com",
    f"{_ALGOLIA_APP_ID}-3.algolianet.com",
]

_HEADERS = {
    "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
    "X-Algolia-API-Key": _ALGOLIA_API_KEY,
    "Content-Type": "application/json",
}


async def search_giant(query: str, limit: int = 20) -> list[dict]:
    payload = {"query": query, "hitsPerPage": min(limit, 50)}

    async with httpx.AsyncClient(timeout=15) as client:
        hits = []
        for host in _ALGOLIA_HOSTS:
            url = f"https://{host}/1/indexes/{_ALGOLIA_INDEX}/query"
            try:
                resp = await client.post(url, headers=_HEADERS, json=payload)
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
                print(f"[giant] Algolia via {host} returned {len(hits)} hits")
                break
            except Exception as exc:
                print(f"[giant] {host} failed: {exc}")
                continue

    if not hits:
        print("[giant] all Algolia hosts failed")
        return []

    products, seen = [], set()
    for h in hits:
        name  = (h.get("name") or "").strip()
        price = h.get("price") or 0
        if not name or not price or name in seen:
            continue
        seen.add(name)
        products.append({
            "name": name, "brand": h.get("brand_name", ""), "price": float(price),
            "original_price": None, "promo": None, "unit": h.get("size", ""),
            "image": h.get("image_url", ""), "barcode": None,
            "category": "", "store": "giant",
            "scraped_at": datetime.utcnow(),
        })
        if len(products) >= limit:
            break

    print(f"[giant] parsed {len(products)} products")
    return products
