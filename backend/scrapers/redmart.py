"""
RedMart scraper.
RedMart is hosted on Lazada SG. The hash-based search URL
(search/#q=...) returns random page products, not search results.
Use the catalog query-param URL instead, which properly filters by term.
Results are post-filtered to keep only products whose names contain at
least one meaningful word from the search query.
"""
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from ._base import _EXTRACT_JS, _UA, block_resources, _is_relevant

# Proper query-param URL loads real search results; hash URL is a fallback only
_URLS = [
    "https://www.lazada.sg/catalog/?q={}&from=input&seller_type=official",
    "https://redmart.lazada.sg/catalog/?q={}",
    "https://redmart.lazada.sg/search/#q={}&from=input",
]


async def search_redmart(query: str, browser=None) -> list[dict]:
    own_browser = browser is None
    _pw = None
    if own_browser:
        _pw = await async_playwright().start()
        browser = await _pw.chromium.launch(headless=True)

    ctx = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 900},
        extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
    )
    page = await ctx.new_page()
    await page.route("**/*", block_resources)
    raw = []

    try:
        for url_tmpl in _URLS:
            url = url_tmpl.format(quote_plus(query))
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Lazada is heavy — wait for product cards to appear
                try:
                    await page.wait_for_selector(
                        "[class*='product' i], [class*='item' i], [data-sku]",
                        timeout=4_000,
                    )
                except Exception:
                    pass
            except Exception as exc:
                print(f"[red] {url} failed: {exc}")
                continue

            title = await page.title()
            print(f"[red] loaded: {title} | {page.url}")

            # Quick check before expensive full-DOM extraction
            no_results = await page.evaluate("""() => {
                const t = document.body.innerText.toLowerCase();
                return !t.includes('$') || t.includes('0 results') || t.includes('no results found');
            }""")
            if no_results:
                print(f"[red] no results detected, skipping URL")
                continue

            raw = await page.evaluate(_EXTRACT_JS)
            print(f"[red] DOM extracted {len(raw)} price nodes")

            relevant = [r for r in raw if _is_relevant(r.get("name", ""), query)]
            print(f"[red] relevant after filter: {len(relevant)}")
            if relevant:
                raw = relevant
                break
            raw = []

    except Exception as exc:
        print(f"[red] error: {exc}")
    finally:
        asyncio.ensure_future(ctx.close())
        if own_browser and _pw:
            await browser.close()
            await _pw.stop()

    products = []
    for item in raw:
        name  = (item.get("name") or "").strip()
        price = item.get("price")
        if not name or not price:
            continue
        orig = item.get("original_price")
        products.append({
            "name": name, "brand": "", "price": float(price),
            "original_price": float(orig) if orig else None,
            "promo": item.get("promo") or None, "unit": "",
            "image": item.get("image", ""), "barcode": None,
            "category": "", "store": "red",
            "scraped_at": datetime.utcnow(),
        })

    print(f"[red] parsed {len(products)} products")
    return products
