"""
NTUC FairPrice scraper.
The product API requires auth we don't have, so we load the search page
with Playwright and extract products using a DOM text-walker that locates
$X.XX price nodes and walks up the tree to find the product name + image.
No CSS class names needed — works even with hashed/styled-component classes.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

_SEARCH_URL = "https://www.fairprice.com.sg/search?query={}"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# JS injected into the page to extract products without knowing class names
_EXTRACT_JS = """() => {
    const priceRe   = /^\\$?(\\d+\\.\\d{2})$/;
    const strikeSel = 'del,s,strike,[class*="was"],[class*="original"],[class*="before"],[class*="old-price"],[class*="compare-price"]';
    const promoSel  = '[class*="promo"],[class*="offer"],[class*="deal"],[class*="badge"],[class*="tag"],[class*="sticker"],[class*="label"]';
    const promoRe   = /\\d[+]\\d\\s*free|\\d-for-\\d|\\bbuy\\s+\\d+\\s+get\\s+\\d+|(?:any\\s+)?\\d+\\s+(?:for|@|at)\\s+\\$[\\d.]+/i;
    // Pre-scan for section-level promo islands
    const promoIslands = [];
    for (const el of document.querySelectorAll('div,section,li,article,span')) {
        if (el.children.length > 6) continue;
        const t = el.textContent.trim();
        if (t.length > 3 && t.length < 100 && promoRe.test(t)) {
            promoIslands.push({ el, text: t });
        }
    }

    const seen    = new Set();
    const results = [];

    const iter = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = iter.nextNode())) {
        const txt = node.textContent.trim();
        const m   = txt.match(priceRe);
        if (!m) continue;
        const price = parseFloat(m[1]);
        if (price < 0.10 || price > 999) continue;

        let el = node.parentElement, name = '', img = '', origPrice = null, promoText = null;
        let foundCard = false;

        for (let i = 0; i < 10; i++) {
            if (!el || el === document.body) break;

            // Phase 1: find product name + image
            if (!foundCard) {
                if (!name) {
                    const candidates = el.querySelectorAll('span,p,a,h1,h2,h3,h4,h5');
                    for (const c of candidates) {
                        if (c.children.length > 0) continue;
                        const t = c.textContent.trim();
                        if (t.length > 5 && t.length < 250 &&
                            !t.match(/^\\$?[\\d.,\\s]+$/) &&
                            !t.match(/^(add|view|buy|shop|more|sale|off|promo|per|kg|g\\b)/i) &&
                            !t.includes('$') &&
                            !/add\\s+to\\s+cart/i.test(t) &&
                            !/\\d+\\.\\d+\\s*\\(\\d+\\)/.test(t)) {
                            name = t;
                            break;
                        }
                    }
                }

                if (!img) {
                    const imgEl = el.querySelector('img');
                    if (imgEl) img = imgEl.src || imgEl.dataset.src || '';
                }

                if (name && img) foundCard = true;
            }

            // Phase 2: promo detection (keep climbing)
            if (i >= 1) {
                if (!promoText) {
                    for (const p of el.querySelectorAll(promoSel)) {
                        const t = p.textContent.trim();
                        if (promoRe.test(t) && t.length < 80) { promoText = t; break; }
                    }
                    if (!promoText) {
                        for (const c of el.querySelectorAll('span,div,p')) {
                            if (c.children.length > 0) continue;
                            const t = c.textContent.trim();
                            if (promoRe.test(t) && t.length < 80) { promoText = t; break; }
                        }
                    }
                    if (!promoText) {
                        for (const { el: pe, text } of promoIslands) {
                            if (el.contains(pe)) { promoText = text; break; }
                        }
                    }
                }

                if (!origPrice) {
                    for (const d of el.querySelectorAll(strikeSel)) {
                        const t = d.textContent.trim();
                        const pm = t.match(/^\\$?(\\d+\\.\\d{2})$/);
                        if (pm) {
                            const op = parseFloat(pm[1]);
                            if (op > price) { origPrice = op; break; }
                        }
                    }
                }
            }

            // Keep climbing to capture section-level promos
            if (foundCard && i >= 7) break;
            el = el.parentElement;
        }

        if (!name) continue;
        const key = name + '|' + price;
        if (seen.has(key)) continue;
        seen.add(key);
        results.push({ name, price, image: img, original_price: origPrice, promo: promoText });
    }
    return results;
}"""


async def search_ntuc(query: str, limit: int = 20) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()

        try:
            await page.goto(_SEARCH_URL.format(query), wait_until="load", timeout=30_000)
        except Exception as exc:
            print(f"[ntuc] page load error: {exc}")
            await browser.close()
            return []

        # Wait for at least one price to appear in the DOM
        try:
            await page.wait_for_function(
                "() => document.body.innerText.includes('$')",
                timeout=12_000,
            )
        except Exception:
            pass
        await asyncio.sleep(2)

        title = await page.title()
        print(f"[ntuc] loaded: {title}")

        raw = await page.evaluate(_EXTRACT_JS)
        await browser.close()

    print(f"[ntuc] DOM extracted {len(raw)} price nodes")

    import re
    def clean_name(n: str) -> str:
        if not n:
            return ''
        n = re.sub(r'\$\d+(?:\.\d+)?', '', n)
        n = re.sub(r'add\s+to\s+cart', '', n, flags=re.IGNORECASE)
        n = re.sub(r'\d+\.\d+\s*\(\d+\)', '', n)
        n = re.sub(r'^(?:any\s+\d+\s+(?:at|for|@)\s*|\d+\s+(?:for|@|at)\s*|buy\s+\d+\s+get\s+\d+\s*)', '', n, flags=re.IGNORECASE)
        n = re.sub(r'\s+', ' ', n).strip()
        return n

    products = []
    for item in raw[:limit]:
        name  = clean_name((item.get("name") or "").strip())
        price = item.get("price")
        if not name or not price:
            continue
        orig = item.get("original_price")
        products.append({
            "name":           name,
            "brand":          "",
            "price":          float(price),
            "original_price": float(orig) if orig else None,
            "promo":          item.get("promo") or None,
            "unit":           "",
            "image":          item.get("image", ""),
            "barcode":        None,
            "category":       "",
            "store":          "ntuc",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[ntuc] parsed {len(products)} products")
    return products
        price = item.get("price")
        if not name or not price:
            continue
        orig = item.get("original_price")
        products.append({
            "name":           name,
            "brand":          "",
            "price":          float(price),
            "original_price": float(orig) if orig else None,
            "promo":          item.get("promo") or None,
            "unit":           "",
            "image":          item.get("image", ""),
            "barcode":        None,
            "category":       "",
            "store":          "ntuc",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[ntuc] parsed {len(products)} products")
    return products
