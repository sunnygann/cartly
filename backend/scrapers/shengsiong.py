"""
Sheng Siong scraper.
Tries, in order:
  1. JSON-LD structured data (schema.org Product markup)
  2. Batched DOM sweep via a single page.evaluate() call
All URL candidates are tried in parallel; first to land on a results page wins.
"""
import re
import json
import asyncio
from datetime import datetime
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
from ._base import block_resources

_PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Single JS evaluation replaces the Python IPC loop (was ~30 round-trips per product).
_DOM_SWEEP_JS = r"""() => {
    const PRICE_RE = /\$?\s*(\d+\.\d{2})/;
    const ORIG_SEL = 'del,[class*="was"],[class*="original"],[class*="before"],[class*="old-price"]';
    const seen = new Set();
    const results = [];

    const priceEls = document.querySelectorAll('[class*="price" i],[class*="Price"],[id*="price" i]');
    for (const priceEl of priceEls) {
        const priceText = (priceEl.innerText || '').trim();
        const m = PRICE_RE.exec(priceText);
        if (!m) continue;
        const price = parseFloat(m[1]);
        if (price <= 0 || price > 999) continue;

        // Walk up 4 levels for name
        let name = '';
        let el = priceEl;
        for (let i = 0; i < 4; i++) {
            el = el.parentElement;
            if (!el) break;
            const nameEl = el.querySelector('[class*="name" i],[class*="title" i],h1,h2,h3,h4,h5,a');
            if (nameEl) {
                const t = (nameEl.innerText || '').trim();
                if (t.length > 3 && t.length < 200 && !t.includes('$')) { name = t; break; }
            }
        }
        if (!name) continue;
        const key = name + '|' + price;
        if (seen.has(key)) continue;
        seen.add(key);

        // Walk up 7 levels from price element for image (independent of name walk)
        let img = '';
        let elImg = priceEl;
        for (let i = 0; i < 7; i++) {
            elImg = elImg.parentElement;
            if (!elImg) break;
            const imgEl = elImg.querySelector('img');
            if (imgEl) { img = imgEl.src || ''; break; }
        }

        // Look for original price within same ancestor used for name
        let origPrice = null;
        let elOrig = priceEl;
        for (let i = 0; i < 4; i++) {
            elOrig = elOrig.parentElement;
            if (!elOrig) break;
            const origEl = elOrig.querySelector(ORIG_SEL);
            if (origEl) {
                const om = PRICE_RE.exec((origEl.innerText || '').trim());
                if (om && parseFloat(om[1]) > price) origPrice = parseFloat(om[1]);
                break;
            }
        }

        results.push({ name, price, image: img, orig_price: origPrice });
    }
    return results;
}"""


async def search_shengsiong(query: str, browser=None) -> list[dict]:
    own_browser = browser is None
    _pw = None
    if own_browser:
        _pw = await async_playwright().start()
        browser = await _pw.chromium.launch(headless=True)

    ctx = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 900},
    )
    products = []

    async def try_url(url):
        pg = await ctx.new_page()
        await pg.route("**/*", block_resources)
        try:
            await pg.goto(url, wait_until="domcontentloaded", timeout=12_000)
            try:
                await pg.wait_for_load_state("networkidle", timeout=1_500)
            except Exception:
                pass
            title = await pg.title()
            print(f"[sheng] trying: {title} | {pg.url}")
            is_home = "online grocery" in title.lower() or title.lower().strip() in ("sheng siong", "home")
            has_query = any(w in title.lower() for w in query.lower().split() if len(w) > 2)
            if has_query or not is_home:
                return pg
            await pg.close()
        except asyncio.CancelledError:
            try:
                await pg.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            print(f"[sheng] {url} failed: {exc}")
            try:
                await pg.close()
            except Exception:
                pass
        return None

    try:
        direct_candidates = [
            f"https://shengsiong.com.sg/search?q={quote_plus(query)}",
            f"https://shengsiong.com.sg/search/{quote_plus(query)}",
            f"https://shengsiong.com.sg/search/{query.replace(' ', '-')}",
        ]

        # Try all URL patterns in parallel; first to land on a results page wins.
        tasks = [asyncio.create_task(try_url(url)) for url in direct_candidates]
        page = None
        remaining = set(tasks)
        while remaining and page is None:
            done, remaining = await asyncio.wait(
                remaining, return_when=asyncio.FIRST_COMPLETED, timeout=14.0
            )
            if not done:
                break
            for t in done:
                try:
                    result = t.result()
                    if result is not None:
                        page = result
                        break
                except Exception:
                    pass
        for t in remaining:
            t.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

        if not page:
            # Fallback: homepage + search box
            print("[sheng] direct URLs failed, falling back to homepage search")
            page = await ctx.new_page()
            await page.route("**/*", block_resources)
            await page.goto("https://shengsiong.com.sg/", wait_until="domcontentloaded", timeout=25_000)
            search_sel = (
                "input[type='search'], input[name='q'], input[name='s'], "
                "input[name='keyword'], input[placeholder*='search' i], "
                "#search, .search-input, [class*='search' i] input"
            )
            search_input = await page.query_selector(search_sel)
            if not search_input:
                print("[sheng] search input not found on homepage")
                return []
            await search_input.click()
            await search_input.fill(query)
            await search_input.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

        title = await page.title()
        print(f"[sheng] landed: {title} | {page.url}")

        # ── 1. JSON-LD structured data ────────────────────────────────────
        json_ld_texts = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                 .map(s => s.textContent)
        """)
        print(f"[sheng] found {len(json_ld_texts)} JSON-LD blocks")

        for raw in json_ld_texts:
            try:
                data = json.loads(raw)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    parsed = _from_json_ld(item)
                    if parsed:
                        products.append(parsed)
            except Exception:
                continue

        if products:
            print(f"[sheng] JSON-LD gave {len(products)} products")
            return products

        # ── 2. Batched DOM sweep (single JS evaluation) ───────────────────
        raw_results = await page.evaluate(_DOM_SWEEP_JS)
        print(f"[sheng] DOM batch sweep found {len(raw_results)} price nodes")

        for item in raw_results:
            name = (item.get("name") or "").strip()
            price = item.get("price")
            if not name or not price or float(price) <= 0:
                continue
            orig_price = item.get("orig_price")
            products.append({
                "name":           name[:120],
                "brand":          "",
                "price":          float(price),
                "original_price": float(orig_price) if orig_price else None,
                "promo":          None,
                "unit":           "",
                "image":          item.get("image") or "",
                "barcode":        None,
                "category":       "",
                "store":          "sheng",
                "scraped_at":     datetime.utcnow(),
            })

    except Exception as exc:
        print(f"[sheng] error: {exc}")
    finally:
        asyncio.ensure_future(ctx.close())
        if own_browser and _pw:
            await browser.close()
            await _pw.stop()

    if products:
        print(f"[sheng] gave {len(products)} products")
    else:
        print("[sheng] no products found — site structure needs manual inspection")

    return products


def _from_json_ld(item: dict) -> dict | None:
    t = item.get("@type", "")
    if t not in ("Product", "Offer"):
        return None
    try:
        name  = item.get("name", "").strip()
        offer = item.get("offers") or item
        price = offer.get("price") or offer.get("lowPrice")
        if not name or not price:
            return None
        high = offer.get("highPrice")
        orig = float(high) if high and float(high) > float(price) else None
        image_raw = item.get("image", "")
        image = image_raw if isinstance(image_raw, str) else (image_raw[0] if image_raw else "")
        return {
            "name":           name,
            "brand":          (item.get("brand") or {}).get("name", ""),
            "price":          float(price),
            "original_price": orig,
            "promo":          None,
            "unit":           "",
            "image":          image,
            "barcode":        item.get("gtin13") or item.get("sku"),
            "category":       "",
            "store":          "sheng",
            "scraped_at":     datetime.utcnow(),
        }
    except Exception:
        return None
