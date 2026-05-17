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
    // Only trust visually struck elements and unambiguous "was/old" class patterns.
    // Removed [class*="original"] and [class*="before"] — too broad, they match
    // NTUC deal-price elements and cause the sale price to be misclassified as struck.
    const strikeSel = 'del,s,strike,[class*="was"],[class*="old-price"],[class*="compare-price"],[class*="strikethrough"]';
    const promoJunk = /add\s+to\s+cart|\d+\.\d+\s*\(\d+\)/i;
    const promoRe = /\d\+\d\s*free|\d-for-\d|\bbuy\s+\d+\s+get\s+\d+|(?:any\s+)?\d+\s+(?:for|@|at)\s+\$[\d.]+/i;
    const skipImgRe = /campaign|label|banner|sticker|badge|promo|offer|deal|creative|FPGAds/i;

    // STEP 1: find product cards using structural signals only.
    const cards = new Map();

    const iter = document.createNodeIterator(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = iter.nextNode())) {
        const txt = node.textContent.trim();
        const m = txt.match(priceRe);
        if (!m) continue;
        const price = parseFloat(m[1]);
        if (price < 0.10 || price > 999) continue;

        // Walk up to find the smallest ancestor that looks like a product card.
        let el = node.parentElement;
        let card = null;
        for (let i = 0; i < 14; i++) {
            if (!el || el === document.body) break;
            if (el.querySelector('img') && el.children.length >= 2 && el.children.length <= 15) {
                card = el;
                break;
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

    // STEP 2: build results
    const results = [];
    for (const [card, data] of cards.entries()) {
        const { prices } = data;
        if (prices.length === 0) continue;

        // Determine sale price and the text node it came from.
        // saleNode is used below to pick the promo and image closest to THIS price.
        const nonStrike = prices.filter(p => !p.insideStrike);
        const struck    = prices.filter(p =>  p.insideStrike);

        let salePrice = null, saleNode = null, originalPrice = null;
        if (nonStrike.length > 0) {
            const min = nonStrike.reduce((a, b) => a.price < b.price ? a : b);
            salePrice = min.price; saleNode = min.node;
            if (struck.length > 0) originalPrice = Math.max(...struck.map(p => p.price));
        } else if (struck.length > 0) {
            const min = struck.reduce((a, b) => a.price < b.price ? a : b);
            salePrice = min.price; saleNode = min.node;
            if (struck.length > 1) originalPrice = Math.max(...struck.map(p => p.price));
        }
        if (!salePrice) continue;

        // PROMO DETECTION
        // When a card spans multiple products (e.g. an inline ad followed by a real product),
        // there may be multiple [data-testid="promo-label"] elements inside.
        // Use compareDocumentPosition to find the LAST promo-label that precedes saleNode in
        // DOM order — that is the promo belonging to this specific product, not an earlier one.
        let promo = null;
        const allPromos = Array.from(card.querySelectorAll('[data-testid="promo-label"]'));
        if (allPromos.length === 1) {
            const t = allPromos[0].textContent.trim();
            if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) promo = t;
        } else if (allPromos.length > 1) {
            // DOCUMENT_POSITION_FOLLOWING (4): saleNode follows the promo → promo is before price.
            // Iterate forward and keep overwriting so we end up with the last (closest) match.
            for (const el of allPromos) {
                if (el.compareDocumentPosition(saleNode) & 4) {
                    const t = el.textContent.trim();
                    if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) promo = t;
                }
            }
        }
        // Pass 2: closest preceding sibling promo label (badge sits above the card boundary)
        if (!promo && card.parentElement) {
            let sib = card.previousElementSibling;
            while (sib && !promo) {
                const candidates = sib.matches('[data-testid="promo-label"]')
                    ? [sib]
                    : Array.from(sib.querySelectorAll('[data-testid="promo-label"]'))
                          .filter(el => el.parentElement === sib);
                for (const el of candidates) {
                    const t = el.textContent.trim();
                    if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) { promo = t; break; }
                }
                sib = sib.previousElementSibling;
            }
        }
        // Pass 3: textContent scan for multi-buy patterns split across nodes
        if (!promo) {
            for (const el of card.querySelectorAll('span, p, div')) {
                if (el.children.length > 5) continue;
                const t = el.textContent.trim();
                if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t)) { promo = t; break; }
            }
        }

        // NAME EXTRACTION
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

        // UNIT EXTRACTION (size often in a separate span on NTUC)
        let unit = '';
        const unitRe = /^\d+(?:\.\d+)?\s*(?:ml|l|kg|g|oz|lb|pcs?|pieces?|pk|pack|tabs?|caps?|sachets?)\s*$/i;
        for (const el of card.querySelectorAll('span, p, div')) {
            if (el.children.length > 0) continue;
            const t = el.textContent.trim();
            if (unitRe.test(t)) { unit = t; break; }
        }

        // IMAGE EXTRACTION
        // When a card spans multiple products the first image belongs to an earlier product.
        // Iterate images in REVERSE DOM order and use compareDocumentPosition to find the
        // closest non-ad image that precedes saleNode — the image for THIS product.
        let image = '';
        const allImgs = Array.from(card.querySelectorAll('img'));
        for (let i = allImgs.length - 1; i >= 0; i--) {
            const img = allImgs[i];
            // DOCUMENT_POSITION_FOLLOWING (4): saleNode follows img → img is before the price
            if (!(img.compareDocumentPosition(saleNode) & 4)) continue;
            let src = '';
            if (img.srcset) src = img.srcset.split(',')[0].trim().split(/\s+/)[0];
            if (!src && img.dataset.src && !img.dataset.src.startsWith('data:')) src = img.dataset.src;
            if (!src && img.src && !img.src.startsWith('data:')) src = img.src;
            if (!src || skipImgRe.test(src)) continue;
            image = src;
            break;
        }
        // Fallback: any non-ad img in card (for cards with only one product)
        if (!image) {
            for (const img of allImgs) {
                let src = '';
                if (img.srcset) src = img.srcset.split(',')[0].trim().split(/\s+/)[0];
                if (!src && img.dataset.src && !img.dataset.src.startsWith('data:')) src = img.dataset.src;
                if (!src && img.src && !img.src.startsWith('data:')) src = img.src;
                if (!src || skipImgRe.test(src)) continue;
                image = src;
                break;
            }
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


async def search_ntuc(query: str) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()
        await page.route("**/*", block_resources)
        raw = []

        try:
            await page.goto(_SEARCH_URL.format(query), wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('$')",
                    timeout=12_000,
                )
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=1_000)
            except Exception:
                pass
            # Scroll through the page so intersection observers fire and lazy img.src
            # values get replaced with real URLs before we extract.
            await page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const h = document.body.scrollHeight;
                for (let y = 300; y < h; y += 400) { window.scrollTo(0, y); await delay(40); }
                window.scrollTo(0, 0);
            }""")
            await asyncio.sleep(0.3)
            title = await page.title()
            print(f"[ntuc] loaded: {title}")
            raw = await page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            print(f"[ntuc] error: {exc}")
        finally:
            await ctx.close()
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
