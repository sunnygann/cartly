"""
GrabMart (mart.grab.com) scraper.
GrabMart's web app is SPA-heavy and location-gated.  We skip the location
prompt by trying direct search URLs first; if the page loads products, we
extract them with a card-boundary JS sweep identical in principle to the
NTUC/Sheng Siong scrapers.
"""
import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import block_resources

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Try search URLs in order; stop at the first that yields products
_SEARCH_URLS = [
    "https://mart.grab.com/sg/en/search?query={}",
    "https://mart.grab.com/sg/en/search/{}",
    "https://mart.grab.com/sg/en?q={}",
]

_EXTRACT_JS = r"""() => {
    const priceRe   = /^\$?\s*(\d+\.\d{2})$/;
    const strikeSel = 'del,s,strike,[class*="was" i],[class*="original" i],[class*="old" i],[class*="compare" i],[class*="regular" i]';
    const unitRe    = /^\d+(?:\.\d+)?\s*(?:ml|l|kg|g|oz|lb|pcs?|pieces?|pk|pack|tabs?|sachets?)\s*$|^\d+\s*x\s*\d+(?:\.\d+)?\s*(?:ml|l|kg|g)\s*$/i;
    const promoRe   = /buy\s*\d+.*?(?:for|get)\s*[\$\d]|\d+\s+for\s+\$[\d.]+|\d+%\s*off/i;

    const cardMap = new Map();
    const iter = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = iter.nextNode())) {
        const txt = node.textContent.trim();
        const m = txt.match(priceRe);
        if (!m) continue;
        const price = parseFloat(m[1]);
        if (price < 0.10 || price > 999) continue;

        let el = node.parentElement;
        let card = null;
        for (let i = 0; i < 12; i++) {
            if (!el || el === document.body) break;
            if (el.querySelector('img') && el.children.length >= 2 && el.children.length <= 25) {
                card = el;
                break;
            }
            el = el.parentElement;
        }
        if (!card) continue;

        let isStruck = false;
        let p = node.parentElement;
        while (p && p !== card) {
            if (p.matches(strikeSel)) { isStruck = true; break; }
            p = p.parentElement;
        }

        if (!cardMap.has(card)) cardMap.set(card, { prices: [] });
        cardMap.get(card).prices.push({ price, isStruck });
    }

    const results = [];
    for (const [card, data] of cardMap.entries()) {
        const { prices } = data;
        if (!prices.length) continue;

        const struckPrices = prices.filter(p => p.isStruck).map(p => p.price);
        const normalPrices = prices.filter(p => !p.isStruck).map(p => p.price);

        let salePrice, origPrice = null;
        if (normalPrices.length > 0) {
            salePrice = Math.min(...normalPrices);
            if (struckPrices.length > 0) origPrice = Math.max(...struckPrices);
        } else {
            salePrice = Math.min(...struckPrices);
            if (struckPrices.length > 1) origPrice = Math.max(...struckPrices);
        }
        if (!salePrice || salePrice < 0.10) continue;

        // Name
        let name = '';
        for (const sel of ['[class*="name" i]', '[class*="title" i]', 'h1', 'h2', 'h3', 'h4']) {
            for (const el of card.querySelectorAll(sel)) {
                const t = el.textContent.trim();
                if (t.length > 4 && t.length < 200 && !/^\$?[\d.,]+$|add to cart|sold out/i.test(t)) {
                    if (t.length > name.length) name = t;
                }
            }
            if (name) break;
        }
        if (!name) {
            for (const el of card.querySelectorAll('span, p, a')) {
                if (el.children.length > 0) continue;
                const t = el.textContent.trim();
                if (t.length > 8 && t.length < 200 &&
                    !/^\$?[\d.,]+$|add to cart|sold out/i.test(t) && !t.includes('$')) {
                    if (t.length > name.length) name = t;
                }
            }
        }
        if (!name) continue;

        // Unit
        let unit = '';
        for (const el of card.querySelectorAll('span, p, small, div')) {
            if (el.children.length > 0) continue;
            const t = el.textContent.trim();
            if (unitRe.test(t)) { unit = t; break; }
        }
        if (!unit) {
            const sm = name.match(/\b(\d+(?:\.\d+)?\s*(?:ml|l|kg|g))\b/i);
            if (sm) unit = sm[0].trim();
        }

        // Promo
        let promo = null;
        for (const el of card.querySelectorAll('span, div, p')) {
            if (el.children.length > 3) continue;
            const t = el.textContent.trim();
            if (promoRe.test(t) && t.length < 60) { promo = t; break; }
        }
        if (/sold\s*out|unavailable/i.test(card.textContent)) promo = 'SOLD OUT';

        // Image
        let image = '';
        for (const img of card.querySelectorAll('img')) {
            let src = '';
            if (img.srcset) src = img.srcset.split(',')[0].trim().split(/\s+/)[0];
            if (!src && img.dataset.src && !img.dataset.src.startsWith('data:')) src = img.dataset.src;
            if (!src && img.src && !img.src.startsWith('data:')) src = img.src;
            if (src && !/icon|logo|badge|promo|placeholder/i.test(src)) { image = src; break; }
        }

        results.push({ name, price: salePrice, original_price: origPrice, unit, promo, image });
    }
    return results;
}"""


async def search_grabmart(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 390, "height": 844},  # mobile viewport — mart.grab.com is mobile-first
            extra_http_headers={"Accept-Language": "en-SG,en;q=0.9"},
        )
        page = await ctx.new_page()
        await page.route("**/*", block_resources)

        raw = []
        try:
            for url_tmpl in _SEARCH_URLS:
                url = url_tmpl.format(query.replace(" ", "+"))
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                    try:
                        await page.wait_for_function(
                            "() => document.body.innerText.includes('$')", timeout=10_000
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    title = await page.title()
                    print(f"[grab] loaded: {title} | {page.url}")
                    raw = await page.evaluate(_EXTRACT_JS)
                    print(f"[grab] extracted {len(raw)} from {url}")
                    if raw:
                        break
                except Exception as exc:
                    print(f"[grab] {url} failed: {exc}")
                    continue
        except Exception as exc:
            print(f"[grab] error: {exc}")
        finally:
            await ctx.close()
            await browser.close()

    print(f"[grab] extracted {len(raw)} cards total")

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
            "store":          "grab",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[grab] parsed {len(products)} products")
    return products
