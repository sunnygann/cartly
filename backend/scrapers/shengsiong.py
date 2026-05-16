"""
Sheng Siong scraper.
Tries, in order:
  1. JSON-LD structured data (schema.org Product markup)
  2. Broad DOM selector sweep with price-text matching
  3. Raw HTML regex for price + nearby name text
"""
import re
import json
import asyncio
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright

_SEARCH_URLS = [
    "https://shengsiong.com.sg/search?q={}",
    "https://shengsiong.com.sg/?s={}",
]
_PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def search_shengsiong(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # Load homepage and use the search box — URL-based search redirects to homepage
        try:
            await page.goto("https://shengsiong.com.sg/", wait_until="load", timeout=25_000)
        except Exception as exc:
            print(f"[sheng] homepage load failed: {exc}")
            await browser.close()
            return []

        # Find, click, clear, then fill the search input
        search_sel = (
            "input[type='search'], input[name='q'], input[name='s'], "
            "input[name='keyword'], input[placeholder*='search' i], "
            "#search, .search-input, [class*='search' i] input"
        )
        search_input = await page.query_selector(search_sel)
        if not search_input:
            print("[sheng] search input not found on homepage")
            await browser.close()
            return []

        await search_input.click()
        await search_input.fill(query)
        await search_input.press("Enter")
        try:
            await page.wait_for_load_state("load", timeout=20_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # If still on homepage or query not in URL, try direct search URLs
        on_homepage = page.url.rstrip("/") in (
            "https://shengsiong.com.sg", "https://www.shengsiong.com.sg"
        )
        query_missing = not any(
            w in page.url.lower()
            for w in query.lower().split()
            if len(w) > 2
        )
        if on_homepage or query_missing:
            print(f"[sheng] search may not have fired, retrying via URL")
            # Try several URL formats — multi-word queries need different encoding
            retry_candidates = [
                f"https://shengsiong.com.sg/search/{quote_plus(query)}",   # spaces → +
                f"https://shengsiong.com.sg/search/{query.replace(' ', '-')}",  # spaces → -
                f"https://shengsiong.com.sg/search?q={quote_plus(query)}",  # query param
            ]
            for retry_url in retry_candidates:
                try:
                    await page.goto(retry_url, wait_until="load", timeout=20_000)
                    await asyncio.sleep(2)
                    title = await page.title()
                    # If the page title mentions the query or shows product results, stop
                    if any(w in title.lower() for w in query.lower().split() if len(w) > 2):
                        break
                    if "online grocery" not in title.lower() and "home" not in title.lower():
                        break
                except Exception:
                    pass

        loaded_url = page.url
        title = await page.title()
        print(f"[sheng] after search: {title} | {loaded_url}")

        # ── 1. JSON-LD structured data ────────────────────────────────────
        json_ld_texts = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                 .map(s => s.textContent)
        """)
        print(f"[sheng] found {len(json_ld_texts)} JSON-LD blocks")

        products = []
        for raw in json_ld_texts:
            try:
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    parsed = _from_json_ld(item)
                    if parsed:
                        products.append(parsed)
            except Exception:
                continue

        if products:
            print(f"[sheng] JSON-LD gave {len(products)} products")
            await browser.close()
            return products

        # ── 2. DOM sweep: every element with a $ price ───────────────────
        # Get all text nodes that contain a price, walk up to find a name
        price_els = await page.query_selector_all(
            "[class*='price' i], [class*='Price' i], [id*='price' i]"
        )
        print(f"[sheng] price elements found: {len(price_els)}")

        for el in price_els:
            try:
                price_text = (await el.inner_text()).strip()
                m = _PRICE_RE.search(price_text)
                if not m:
                    continue

                # Walk up the DOM tree up to 4 levels to find a name sibling
                name = ""
                for _ in range(4):
                    el = await el.evaluate_handle("el => el.parentElement")
                    if not el:
                        break
                    name_el = await el.query_selector(
                        "[class*='name' i], [class*='title' i], h1, h2, h3, h4, h5, a"
                    )
                    if name_el:
                        name = (await name_el.inner_text()).strip()
                        if name:
                            break

                img_el = None
                for _ in range(3):
                    el = await el.evaluate_handle("el => el.parentElement")
                    if not el:
                        break
                    img_el = await el.query_selector("img")
                    if img_el:
                        break

                img_src = await img_el.get_attribute("src") if img_el else ""

                orig_price = None
                try:
                    orig_el = await el.query_selector(
                        'del,[class*="was"],[class*="original"],[class*="before"],[class*="old-price"]'
                    )
                    if orig_el:
                        orig_txt = (await orig_el.inner_text()).strip()
                        om = _PRICE_RE.search(orig_txt)
                        if om and float(om.group(1)) > float(m.group(1)):
                            orig_price = float(om.group(1))
                except Exception:
                    pass

                if name and float(m.group(1)) > 0:
                    products.append({
                        "name":           name[:120],
                        "brand":          "",
                        "price":          float(m.group(1)),
                        "original_price": orig_price,
                        "promo":          None,
                        "unit":           "",
                        "image":          img_src or "",
                        "barcode":        None,
                        "category":       "",
                        "store":          "sheng",
                        "scraped_at":     datetime.utcnow(),
                    })
            except Exception:
                continue

        await browser.close()

    if products:
        print(f"[sheng] DOM sweep gave {len(products)} products")
    else:
        print("[sheng] no products found — site structure needs manual inspection")

    return products


def _from_json_ld(item: dict) -> dict | None:
    t = item.get("@type", "")
    if t not in ("Product", "Offer"):
        return None
    try:
        name  = item.get("name", "").strip()
        offer = item.get("offers") or item
        price = offer.get("price") or offer.get("lowPrice")
        if not name or not price:
            return None
        high = offer.get("highPrice")
        orig = float(high) if high and float(high) > float(price) else None
        image_raw = item.get("image", "")
        image = image_raw if isinstance(image_raw, str) else (image_raw[0] if image_raw else "")
        return {
            "name":           name,
            "brand":          (item.get("brand") or {}).get("name", ""),
            "price":          float(price),
            "original_price": orig,
            "promo":          None,
            "unit":           "",
            "image":          image,
            "barcode":        item.get("gtin13") or item.get("sku"),
            "category":       "",
            "store":          "sheng",
            "scraped_at":     datetime.utcnow(),
        }
    except Exception:
        return None
