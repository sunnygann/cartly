"""
Amazon Fresh (amazon.sg) scraper.
Uses Playwright with Amazon's well-known search-result card structure.
Does NOT block resources — Amazon's bot detection flags unusually fast/bare loads.
"""
import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright

_SEARCH_URL = "https://www.amazon.sg/s?k={}&i=amazonfresh"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_EXTRACT_JS = r"""() => {
    const unitRe  = /\b(\d+(?:\.\d+)?\s*(?:ml|l|kg|g|oz|lb|pcs?|pieces?|pk|pack|count|ct)\b(?:\s*x\s*\d+)?)/i;
    const promoRe = /\d+%\s*off|save\s*\$[\d.]+|deal|coupon|\d+\s+for\s+\$[\d.]+/i;

    const cards = document.querySelectorAll('[data-component-type="s-search-result"]');
    const results = [];

    for (const card of cards) {
        // Name
        const nameEl = card.querySelector('h2 a span, [data-cy="title-recipe"] span, h2 span');
        if (!nameEl) continue;
        const name = nameEl.textContent.trim();
        if (!name || name.length < 3 || name.length > 250) continue;

        // Current price — first .a-offscreen inside any .a-price
        let price = null;
        for (const pe of card.querySelectorAll('.a-price .a-offscreen')) {
            const t = pe.textContent.trim().replace(/[^\d.]/g, '');
            const v = parseFloat(t);
            if (v > 0.1 && v < 5000) { price = v; break; }
        }
        if (!price) continue;

        // Original/crossed-out price
        let origPrice = null;
        for (const oe of card.querySelectorAll('.a-text-price .a-offscreen, .a-text-strike .a-offscreen')) {
            const t = oe.textContent.trim().replace(/[^\d.]/g, '');
            const v = parseFloat(t);
            if (v > price) { origPrice = v; break; }
        }

        // Image
        const imgEl = card.querySelector('img.s-image, img[class*="s-image"]');
        const image = imgEl ? (imgEl.src || '') : '';

        // Unit from title
        let unit = '';
        const unitM = name.match(unitRe);
        if (unitM) unit = unitM[0].trim();

        // Promo badge (percentage off, coupon, deal labels)
        let promo = null;
        for (const el of card.querySelectorAll('.a-badge-text, [class*="badge"], [class*="deal"], span')) {
            if (el.children.length > 0) continue;
            const t = el.textContent.trim();
            if (promoRe.test(t) && t.length < 60) { promo = t; break; }
        }

        results.push({ name, price, original_price: origPrice, unit, promo, image });
    }
    return results;
}"""


async def search_amazon(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
        )
        page = await ctx.new_page()

        raw = []
        try:
            url = _SEARCH_URL.format(query.replace(" ", "+"))
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_selector(
                    '[data-component-type="s-search-result"]', timeout=12_000
                )
            except Exception:
                pass
            await asyncio.sleep(1.5)
            title = await page.title()
            print(f"[amazon] loaded: {title}")
            raw = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            print(f"[amazon] error: {exc}")
        finally:
            await ctx.close()
            await browser.close()

    print(f"[amazon] extracted {len(raw)} cards")

    products = []
    seen: set = set()
    for item in raw:
        name = re.sub(r"\s+", " ", (item.get("name") or "").strip())
        price = item.get("price")
        if not name or not price:
            continue
        key = f"{name.lower()}|{price}"
        if key in seen:
            continue
        seen.add(key)
        orig = item.get("original_price")
        products.append({
            "name":           name,
            "brand":          "",
            "price":          float(price),
            "original_price": float(orig) if orig else None,
            "promo":          item.get("promo") or None,
            "unit":           (item.get("unit") or "").strip(),
            "image":          item.get("image", ""),
            "barcode":        None,
            "category":       "",
            "store":          "amazon",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[amazon] parsed {len(products)} products")
    return products
