"""
NTUC FairPrice scraper.
"""
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import block_resources

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
    const promoRe = /\d\+\d\s*free|\d-for-\d|\bbuy\s+\d+\s+get\s+\d+|(?:any\s+)?\d+\s+(?:for|@|at)\s+\$[\d.]+/i;

    // STEP 1: find product cards using structural signals only.
    // Do NOT use a global promo list to find cards — that causes promos from
    // one product to attract ancestors that span multiple products.
    const cards = new Map();

    const iter = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = iter.nextNode())) {
        const txt = node.textContent.trim();
        const m = txt.match(priceRe);
        if (!m) continue;
        const price = parseFloat(m[1]);
        if (price < 0.10 || price > 999) continue;

        // Walk up to find the smallest ancestor that looks like a product card
        let el = node.parentElement;
        let card = null;
        for (let i = 0; i < 14; i++) {
            if (!el || el === document.body) break;
            if (el.querySelector('img') && el.children.length >= 2 && el.children.length <= 15) {
                card = el;
                break; // smallest matching ancestor = tightest card boundary
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

        if (!cards.has(card)) cards.set(card, { prices: [] });
        cards.get(card).prices.push({ node, price, insideStrike });
    }

    // STEP 2: build results — find promo WITHIN each card's own subtree
    const results = [];
    for (const [card, data] of cards.entries()) {
        const { prices } = data;
        if (prices.length === 0) continue;

        // Promo: check data-testid label first, then text pattern — scoped to card
        let promo = null;
        for (const el of card.querySelectorAll('[data-testid="promo-label"]')) {
            const t = el.textContent.trim();
            if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) { promo = t; break; }
        }
        if (!promo) {
            const tw = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
            let tn;
            while ((tn = tw.nextNode())) {
                const t = tn.textContent.trim();
                if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t)) { promo = t; break; }
            }
        }
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

        // --- UNIT EXTRACTION (size often in a separate span on NTUC) ---
        let unit = '';
        const unitRe = /^\d+(?:\.\d+)?\s*(?:ml|l|kg|g|oz|lb|pcs?|pieces?|pk|pack|tabs?|caps?|sachets?)\s*$/i;
        for (const el of card.querySelectorAll('span, p, div')) {
            if (el.children.length > 0) continue;
            const t = el.textContent.trim();
            if (unitRe.test(t)) { unit = t; break; }
        }

        // --- IMAGE EXTRACTION (card-scoped, skip campaign/label images) ---
        let image = '';
        const skipImgRe = /campaign|label|banner|sticker|badge|promo|offer|deal/i;
        for (const img of card.querySelectorAll('img')) {
            // Next.js lazy images: srcset always has real URLs even when src is a placeholder
            let src = '';
            if (img.srcset) src = img.srcset.split(',')[0].trim().split(/\s+/)[0];
            if (!src && img.dataset.src && !img.dataset.src.startsWith('data:')) src = img.dataset.src;
            if (!src && img.src && !img.src.startsWith('data:')) src = img.src;
            if (!src || skipImgRe.test(src)) continue;
            image = src;
            break;
        }

        results.push({
            name,
            price: salePrice,
            image,
            unit,
            original_price: originalPrice,
            promo: promo
        });
    }
    return results;
}"""


async def search_ntuc(query: str, browser=None) -> list[dict]:
    own_browser = browser is None
    _pw = None
    if own_browser:
        _pw = await async_playwright().start()
        browser = await _pw.chromium.launch(headless=True)

    ctx = await browser.new_context(user_agent=_UA)
    page = await ctx.new_page()
    await page.route("**/*", block_resources)
    raw = []

    try:
        await page.goto(_SEARCH_URL.format(query), wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_function(
                "() => document.body.innerText.includes('$')",
                timeout=3_000,
            )
        except Exception:
            pass
        # Adaptive scroll: 500px steps at 40ms, exits early if page height
        # stabilises for 2 consecutive passes (infinite-scroll already settled).
        await page.evaluate("""async () => {
            const delay = ms => new Promise(r => setTimeout(r, ms));
            let lastH = 0, noGrowth = 0;
            for (let pass = 0; pass < 8; pass++) {
                const h = document.body.scrollHeight;
                if (h === lastH) { if (++noGrowth >= 2) break; }
                else { noGrowth = 0; }
                lastH = h;
                for (let y = 500; y <= h; y += 500) {
                    window.scrollTo(0, y);
                    await delay(40);
                }
                await delay(300);
            }
            window.scrollTo(0, 0);
        }""")
        title = await page.title()
        print(f"[ntuc] loaded: {title}")
        raw = await page.evaluate(_EXTRACT_JS)
    except Exception as exc:
        print(f"[ntuc] error: {exc}")
    finally:
        await ctx.close()
        if own_browser and _pw:
            await browser.close()
            await _pw.stop()

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
    for item in raw:
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
            "unit":           (item.get("unit") or "").strip(),
            "image":          item.get("image", ""),
            "barcode":        None,
            "category":       "",
            "store":          "ntuc",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[ntuc] parsed {len(products)} products")
    return products
