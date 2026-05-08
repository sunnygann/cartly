"""
NTUC FairPrice scraper.
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

_EXTRACT_JS = r"""() => {
    const priceRe = /^\$?(\d+\.\d{2})$/;
    const strikeSel = 'del,s,strike,[class*="was"],[class*="original"],[class*="before"],[class*="old-price"],[class*="compare-price"]';
    const promoJunk = /add\s+to\s+cart|\d+\.\d+\s*\(\d+\)/i;

    // STEP 1: collect ALL promo texts from the page
    const allPromos = [];
    for (const el of document.querySelectorAll('[data-testid="promo-label"]')) {
        const t = el.textContent.trim();
        if (t.length > 3 && t.length < 80 && !promoJunk.test(t) && !allPromos.includes(t)) {
            allPromos.push(t);
        }
    }
    const promoRe = /\d\+\d\s*free|\d-for-\d|\bbuy\s+\d+\s+get\s+\d+|(?:any\s+)?\d+\s+(?:for|@|at)\s+\$[\d.]+/i;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let tn;
    while (tn = walker.nextNode()) {
        const t = tn.textContent.trim();
        if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t) && !allPromos.includes(t)) {
            allPromos.push(t);
        }
    }

    // STEP 2: find product cards
    const cards = new Map();

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
        let promo = null;

        for (let i = 0; i < 14; i++) {
            if (!el || el === document.body) break;

            if (!promo) {
                const ancestorText = el.textContent;
                for (const p of allPromos) {
                    if (ancestorText.includes(p)) {
                        promo = p;
                        break;
                    }
                }
                if (promo) {
                    card = el;
                    break;
                }
            }

            if (!card && el.querySelector('img') && el.children.length >= 2) {
                card = el;
            }
            el = el.parentElement;
        }

        if (!card) continue;

        let insideStrike = false;
        let p = node.parentElement;
        while (p && p !== card && p !== document.body) {
            if (p.matches(strikeSel)) { insideStrike = true; break; }
            p = p.parentElement;
        }

        if (!cards.has(card)) cards.set(card, { prices: [], promo: promo });
        cards.get(card).prices.push({ node, price, insideStrike });
    }

    // STEP 3: build results
    const results = [];
    for (const [card, data] of cards.entries()) {
        const { prices, promo } = data;
        if (prices.length === 0) continue;

        let salePrice = null, originalPrice = null;
        const nonStrikePrices = prices.filter(p => !p.insideStrike).map(p => p.price);
        const strikePrices = prices.filter(p => p.insideStrike).map(p => p.price);

        if (nonStrikePrices.length > 0) {
            salePrice = Math.min(...nonStrikePrices);
            const allHigher = [...strikePrices, ...nonStrikePrices.filter(p => p > salePrice)];
            if (allHigher.length > 0) originalPrice = Math.max(...allHigher);
        } else {
            salePrice = Math.min(...strikePrices);
            if (strikePrices.length > 1) originalPrice = Math.max(...strikePrices);
        }

        let name = '';
        const link = card.querySelector('a[href]');
        if (link && !link.querySelector('img')) {
            const t = link.textContent.replace(/\s+/g, ' ').trim();
            if (t.length > 4 && t.length < 250 && !t.match(/\$/)) name = t;
        }
        if (!name) {
            const allTextEls = card.querySelectorAll('span, p, a, h1, h2, h3, h4, h5');
            let best = '';
            for (const el of allTextEls) {
                if (el.children.length > 0) continue;
                const t = el.textContent.trim();
                if (t.length > 4 && t.length < 250 && !t.match(/^\$?[\d.,]+$/) && !t.includes('$')) {
                    if (t.length > best.length) best = t;
                }
            }
            name = best;
        }
        if (!name) continue;

        // --- Extract image using product name (most reliable) ---
        let image = '';
        if (name) {
            const imgs = card.querySelectorAll('img');
            for (const img of imgs) {
                const alt = (img.alt || '').trim();
                if (alt === 'campaign label' || alt.includes('campaign') || alt.includes('badge')) continue;
                if (alt && name.toLowerCase().includes(alt.toLowerCase())) {
                    image = img.src || img.dataset.src || '';
                    break;
                }
            }
        }
        if (!image) {
            const allImgs = card.querySelectorAll('img');
            for (const img of allImgs) {
                const alt = (img.alt || '').toLowerCase();
                if (alt === 'campaign label' || alt.includes('campaign') || alt.includes('badge')) continue;
                if (img.width && img.width < 60) continue;
                if (img.height && img.height < 60) continue;
                image = img.src || img.dataset.src || '';
                if (image) break;
            }
        }
        if (!image) {
            const imgEl = card.querySelector('img');
            image = imgEl ? (imgEl.src || imgEl.dataset.src || '') : '';
        }

        results.push({
            name,
            price: salePrice,
            image,
            original_price: originalPrice,
            promo: promo
        });
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