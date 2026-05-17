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

        // Promo detection — three passes:
        // 1. data-testid="promo-label" inside card
        // 2. data-testid="promo-label" in parent (badge may sit above the card boundary)
        // 3. Element-level textContent scan (handles "2 for " + "$19.90" split across nodes)
        let promo = null;
        for (const el of card.querySelectorAll('[data-testid="promo-label"]')) {
            const t = el.textContent.trim();
            if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) { promo = t; break; }
        }
        if (!promo && card.parentElement) {
            for (const el of card.parentElement.querySelectorAll('[data-testid="promo-label"]')) {
                if (card.contains(el)) continue;
                // Only direct siblings of card, not promos buried in other nested cards
                if (el.parentElement === card.parentElement) {
                    const t = el.textContent.trim();
                    if (t.length > 3 && t.length < 80 && !promoJunk.test(t)) { promo = t; break; }
                }
            }
        }
        if (!promo) {
            for (const el of card.querySelectorAll('span, p, div')) {
                if (el.children.length > 5) continue;
                const t = el.textContent.trim();
                if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t)) { promo = t; break; }
            }
        }

        let salePrice = null, originalPrice = null;
        const nonStrikePrices = prices.filter(p => !p.insideStrike).map(p => p.price);
        const strikePrices = prices.filter(p => p.insideStrike).map(p => p.price);

        if (nonStrikePrices.length > 0) {
            salePrice = Math.min(...nonStrikePrices);
            // Only use explicitly struck prices as original_price.
            // Higher non-struck prices are multi-buy totals or U.P. labels —
            // they must not appear as strikethrough.
            if (strikePrices.length > 0) originalPrice = Math.max(...strikePrices);
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
            # Multi-pass adaptive scroll: each pass scrolls the full page slowly so
            # intersection observers fire and lazy-rendered products appear. Repeats
            # until scrollHeight stops growing for 2 consecutive passes (up to 20 passes).
            await page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                let stable = 0;
                for (let pass = 0; pass < 20 && stable < 2; pass++) {
                    const before = document.body.scrollHeight;
                    for (let y = 200; y <= document.body.scrollHeight + 200; y += 280) {
                        window.scrollTo(0, y);
                        await delay(100);
                    }
                    await delay(1500);
                    stable = document.body.scrollHeight === before ? stable + 1 : 0;
                }
                window.scrollTo(0, 0);
            }""")
            await asyncio.sleep(0.4)
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

    def _promo_consistent(price: float, promo) -> bool:
        """Return False if a 'N for $X' promo is mathematically impossible at this unit price."""
        if not promo:
            return True
        m = re.search(r'(\d+)\s+for\s+\$?([\d.]+)', promo, re.IGNORECASE)
        if m:
            n, total = int(m.group(1)), float(m.group(2))
            if n < 1 or total <= 0:
                return False
            implied = total / n
            return abs(implied - price) / max(implied, price) <= 0.15
        return True

    # Deduplicate by name. The headless browser sometimes assigns a wrong card
    # boundary, producing a duplicate entry with the same name but wrong data.
    # When both exist, prefer the entry whose promo is mathematically consistent
    # with its price (e.g. reject "$2.30 + 6 for $53.00" in favour of the real one).
    seen: dict[str, int] = {}
    deduped: list[dict] = []
    for p in products:
        key = p["name"].lower()
        if key not in seen:
            seen[key] = len(deduped)
            deduped.append(p)
        else:
            idx = seen[key]
            existing = deduped[idx]
            if not _promo_consistent(existing["price"], existing["promo"]) and \
               _promo_consistent(p["price"], p["promo"]):
                deduped[idx] = p  # swap to the coherent entry

    # Clear any surviving promo that is still inconsistent with its unit price
    for p in deduped:
        if not _promo_consistent(p["price"], p["promo"]):
            print(f"[ntuc] clearing inconsistent promo {p['promo']!r} for {p['name']!r} at ${p['price']}")
            p["promo"] = None

    products = deduped

    print(f"[ntuc] parsed {len(products)} products")
    return products
