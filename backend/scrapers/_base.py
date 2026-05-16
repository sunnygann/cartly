"""Shared DOM extraction logic reused across all store scrapers."""
from playwright.async_api import async_playwright

_BLOCKED_TYPES = {"image", "font", "media", "stylesheet"}

async def block_resources(route):
    if route.request.resource_type in _BLOCKED_TYPES:
        await route.abort()
    else:
        await route.continue_()

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
    const promoJunk = /add\\s+to\\s+cart|\\d+\\.\\d+\\s*\\(\\d+\\)/i;

    // Pre-scan: collect section-level promo islands (e.g. "Any 2 @ $22.00" banners
    // that sit above a product grid rather than inside each card).
    const promoIslands = [];
    for (const el of document.querySelectorAll('div,section,li,article,span')) {
        if (el.children.length > 6) continue;
        const t = el.textContent.trim();
        if (t.length > 3 && t.length < 60 && promoRe.test(t) && !promoJunk.test(t)) {
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
                            !t.match(/^\\$?[\\d.,\\s%]+$/) &&
                            !t.match(/^(add|view|buy|shop|more|sale|off|promo|per|\\d+g\\b)/i) &&
                            !t.includes('$') &&
                            !/add\\s+to\\s+cart/i.test(t) &&
                            !/\\d+\\.\\d+\\s*\\(\\d+\\)/.test(t)) {
                            name = t; break;
                        }
                    }
                }
                if (!img) {
                    const imgEl = el.querySelector('img');
                    if (imgEl) {
                        let src = '';
                        if (imgEl.srcset) src = imgEl.srcset.split(',')[0].trim().split(/\s+/)[0];
                        if (!src && imgEl.dataset.src && !imgEl.dataset.src.startsWith('data:')) src = imgEl.dataset.src;
                        if (!src && imgEl.src && !imgEl.src.startsWith('data:')) src = imgEl.src;
                        img = src;
                    }
                }
                if (name && img) foundCard = true;
            }

            // Phase 2: promo detection (keep climbing even after card is found)
            if (i >= 1) {
                if (!promoText) {
                    // Per-card badge (class-name based)
                    for (const p of el.querySelectorAll(promoSel)) {
                        const t = p.textContent.trim();
                        if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t)) { promoText = t; break; }
                    }
                    // Per-card badge (leaf text)
                    if (!promoText) {
                        for (const c of el.querySelectorAll('span,div,p')) {
                            if (c.children.length > 0) continue;
                            const t = c.textContent.trim();
                            if (promoRe.test(t) && t.length < 60 && !promoJunk.test(t)) { promoText = t; break; }
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


_SKIP_WORDS = {"the","and","for","with","per","from","each","in","of","a","an","to","at","is","it"}

def _is_relevant(name: str, query: str) -> bool:
    name_l  = name.lower()
    q_words = [w for w in query.lower().split() if len(w) > 2 and w not in _SKIP_WORDS]
    if not q_words:
        return True
    return any(w in name_l for w in q_words)
