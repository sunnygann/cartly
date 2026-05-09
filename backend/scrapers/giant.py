"""
Giant scraper.
Giant.sg uses Algolia InstantSearch (app PFCHI1YM66, index giant_product_live).
Algolia's DSN hostname is geo-restricted to Singapore ISP DNS resolvers.
Railway's DNS (datacenter) can't resolve it, so we query SingNet/StarHub
DNS servers directly via dnspython, then connect to the resolved IP with httpx.
"""
import httpx
from datetime import datetime

_ALGOLIA_APP_ID = "PFCHI1YM66"
_ALGOLIA_API_KEY = "d0c09a40111717aec861992cf8497e71"
_ALGOLIA_INDEX   = "giant_product_live"
_ALGOLIA_HOST    = f"{_ALGOLIA_APP_ID}-dsn.algolia.net"

async def _resolve_via_doh(hostname: str) -> str | None:
    """Resolve via Cloudflare DNS-over-HTTPS (port 443, never blocked)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://1.1.1.1/dns-query",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            for answer in resp.json().get("Answer", []):
                if answer.get("type") == 1:
                    ip = answer["data"]
                    print(f"[giant] DoH resolved {hostname} -> {ip}")
                    return ip
    except Exception as exc:
        print(f"[giant] DoH failed: {exc}")
    return None


async def search_giant(query: str, limit: int = 20, browser=None) -> list[dict]:
    ip = await _resolve_via_doh(_ALGOLIA_HOST)

    if not ip:
        print("[giant] Algolia host not resolvable from this network (requires SG ISP)")
        return []

    headers = {
        "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
        "X-Algolia-API-Key": _ALGOLIA_API_KEY,
        "Content-Type": "application/json",
        "Host": _ALGOLIA_HOST,
    }
    payload = {"query": query, "hitsPerPage": min(limit, 50)}

    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            resp = await client.post(
                f"https://{ip}/1/indexes/{_ALGOLIA_INDEX}/query",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            print(f"[giant] Algolia returned {len(hits)} hits")
    except Exception as exc:
        print(f"[giant] Algolia request failed: {exc}")
        return []

    products, seen = [], set()
    for h in hits:
        name  = (h.get("name") or "").strip()
        price = h.get("price") or 0
        if not name or not price or name in seen:
            continue
        seen.add(name)
        raw_orig = (h.get("was_price") or h.get("compare_at_price") or
                    h.get("original_price") or h.get("comparePrice") or
                    h.get("priceWas") or h.get("price_was"))
        orig = float(raw_orig) if raw_orig and float(raw_orig) > float(price) else None
        promo_text = (h.get("promotion") or h.get("promo_text") or
                      h.get("promotionText") or h.get("promo") or None)
        products.append({
            "name": name, "brand": h.get("brand_name", ""), "price": float(price),
            "original_price": orig, "promo": promo_text, "unit": h.get("size", ""),
            "image": h.get("image_url", ""), "barcode": None,
            "category": "", "store": "giant",
            "scraped_at": datetime.utcnow(),
        })
        if len(products) >= limit:
            break

    print(f"[giant] parsed {len(products)} products")
    return products
