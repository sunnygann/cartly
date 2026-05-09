"""
Cold Storage scraper.
The site is Next.js App Router (RSC) — product data is server-rendered into
self.__next_f.push(...) inline scripts. We fetch the HTML directly with httpx
(no browser needed, faster, avoids bot-detection) and extract initialProducts
via bracket-counting rather than fragile regex.
SSL certificate is expired — we pass verify=False.
"""
import json
import httpx
from datetime import datetime
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"

_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _extract_initial_products(html: str) -> list:
    """Find 'initialProducts':[ in the RSC payload and extract the full array."""
    marker = '"initialProducts":'
    idx = html.find(marker)
    if idx == -1:
        return []

    arr_start = html.find('[', idx + len(marker))
    if arr_start == -1:
        return []

    # Walk forward counting brackets to find the matching ]
    depth = 0
    for i in range(arr_start, len(html)):
        ch = html[i]
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[arr_start:i + 1])
                except json.JSONDecodeError:
                    return []
    return []


async def search_coldstorage(query: str, limit: int = 20) -> list[dict]:
    url = _URL.format(query)
    try:
        async with httpx.AsyncClient(verify=False, follow_redirects=True, timeout=28) as client:
            resp = await client.get(url, headers=_HEADERS)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        print(f"[cold] fetch error: {exc}")
        return []

    print(f"[cold] fetched {len(html)} chars")

    raw = _extract_initial_products(html)
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
