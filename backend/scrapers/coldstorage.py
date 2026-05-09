"""
Cold Storage scraper.
The site is Next.js App Router (RSC) — product data is server-rendered into
self.__next_f.push(...) inline scripts. We pull the HTML, regex-extract the
initialProducts JSON array, and map price/promoPrice directly.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import re
import json
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA

_URL = "https://www.coldstorage.com.sg/search?q={}"


async def search_coldstorage(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        url = _URL.format(query)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=28_000)
        except Exception as exc:
            print(f"[cold] page load error: {exc}")
            await browser.close()
            return []

        title = await page.title()
        print(f"[cold] loaded: {title} | {page.url}")
        html = await page.content()
        await browser.close()

    # initialProducts lives in an RSC self.__next_f.push(...) script block
    m = re.search(r'"initialProducts":(\[.*?\]),"filters"', html, re.DOTALL)
    if not m:
        print("[cold] initialProducts not found in RSC payload")
        return []

    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        print(f"[cold] JSON parse error: {exc}")
        return []

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
