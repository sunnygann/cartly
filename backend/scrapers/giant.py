"""
Giant scraper.
Giant.sg uses Algolia InstantSearch (app PFCHI1YM66, index giant_product_live).
Algolia's DSN hostname is geo-restricted to Singapore ISP DNS resolvers.
Railway's DNS (datacenter) can't resolve it, so we query SingNet/StarHub
DNS servers directly via dnspython, then connect to the resolved IP with httpx.
"""
import asyncio
import httpx
from datetime import datetime

_ALGOLIA_APP_ID = "PFCHI1YM66"
_ALGOLIA_API_KEY = "d0c09a40111717aec861992cf8497e71"
_ALGOLIA_INDEX   = "giant_product_live"
_ALGOLIA_HOST    = f"{_ALGOLIA_APP_ID}-dsn.algolia.net"

# Singapore ISP DNS servers — geo-aware, return valid Algolia IPs for SG networks
_SG_DNS = ["165.21.83.88", "165.21.100.88", "203.142.78.133", "203.142.78.134"]


def _resolve_via_sg_dns(hostname: str) -> str | None:
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = _SG_DNS
        resolver.timeout = 5
        resolver.lifetime = 8
        ip = str(resolver.resolve(hostname, "A")[0])
        print(f"[giant] resolved {hostname} -> {ip}")
        return ip
    except Exception as exc:
        print(f"[giant] SG DNS failed: {exc}")
        return None


async def search_giant(query: str, limit: int = 20) -> list[dict]:
    loop = asyncio.get_event_loop()
    ip = await loop.run_in_executor(None, _resolve_via_sg_dns, _ALGOLIA_HOST)

    if not ip:
        print("[giant] could not resolve Algolia host via SG DNS")
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
