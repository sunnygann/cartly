"""Shared DOM extraction logic reused across all store scrapers."""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Finds every $X.XX price node in the rendered DOM, then walks up the
# element tree to find the nearest product name and image.
_EXTRACT_JS = """() => {
    const priceRe   = /^\\$?(\\d+\\.\\d{2})$/;
    const strikeSel = 'del,s,strike,[class*="was"],[class*="original"],[class*="before"],[class*="old-price"],[class*="compare-price"]';
    const promoSel  = '[class*="promo"],[class*="offer"],[class*="deal"],[class*="badge"],[class*="tag"],[class*="sticker"],[class*="label"]';
    const promoRe   = /\\d[+]\\d\\s*free|\\d-for-\\d|\\bbuy\\s+\\d+\\s+get\\s+\\d+|(?:any\\s+)?\\d+\\s+(?:for|@|at)\\s+\\$[\\d.]+/i;

    // Pre-scan: collect section-level promo islands (e.g. "Any 2 @ $22.00" banners
    // that sit above a product grid rather than inside each card).
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
    const iter    = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
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

            // Phase 1: find product name + image (only until we have both)
            if (!foundCard) {
                if (!name) {
                    for (const c of el.querySelectorAll('span,p,a,h1,h2,h3,h4,h5')) {
                        if (c.children.length > 0) continue;
                        const t = c.textContent.trim();
                        if (t.length > 5 && t.length < 250 &&
                            !t.match(/^\\$?[\\d.,\\s]+$/) &&
            !t.match(/^(add|view|buy|shop|more|sale|off|promo|per|kg|g\\b)/i) &&
            // ⬇️ new anti‑garbage checks ⬇️
            !t.includes('$') &&
            !/add\s+to\s+cart/i.test(t) &&
            !/\d+\.\d+\s*\(\d+\)/.test(t)) {
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

            // Phase 2: promo detection (keep climbing even after card is found)
            if (i >= 1) {
                if (!promoText) {
                    // Per-card badge (class-name based)
                    for (const p of el.querySelectorAll(promoSel)) {
                        const t = p.textContent.trim();
                        if (promoRe.test(t) && t.length < 80) { promoText = t; break; }
                    }
                    // Per-card badge (leaf text)
                    if (!promoText) {
                        for (const c of el.querySelectorAll('span,div,p')) {
                            if (c.children.length > 0) continue;
                            const t = c.textContent.trim();
                            if (promoRe.test(t) && t.length < 80) { promoText = t; break; }
                        }
                    }
                    // Section-level island
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

            // Don't break early — keep climbing to capture section-level promos
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


async def scrape_store(
    store_key: str,
    search_urls: list[str],
    query: str,
    limit: int = 20,
    wait_selector: str = None,
    extra_sleep: float = 2.0,
) -> list[dict]:
    """Load each URL candidate until one gives products, then extract."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=_UA, viewport={"width": 1280, "height": 900}
        )
        page = await ctx.new_page()

        raw = []
        for url_tmpl in search_urls:
            url = url_tmpl.format(query)
            try:
                await page.goto(url, wait_until="load", timeout=28_000)
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=8_000)
                    except Exception:
                        pass
                await asyncio.sleep(extra_sleep)
            except Exception as exc:
                print(f"[{store_key}] {url} failed: {exc}")
                continue

            title = await page.title()
            print(f"[{store_key}] loaded: {title} | {page.url}")
            raw = await page.evaluate(_EXTRACT_JS)
            print(f"[{store_key}] DOM extracted {len(raw)} price nodes")
            if raw:
                break

        await browser.close()

    products = []
    for item in raw[:limit]:
        name  = (item.get("name") or "").strip()
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
            "store":          store_key,
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[{store_key}] parsed {len(products)} products")
    return products