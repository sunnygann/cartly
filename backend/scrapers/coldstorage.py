"""
Cold Storage scraper.
Uses two sources:
  1. window.__next_f RSC stream — scans all entries for any known product-list key
     (initialProducts, products, searchProducts, productList, items).
  2. RSC fetch responses intercepted during scroll — additional items loaded lazily.
Products deduplicated by productId. inventoryStatus field used for sold-out detection.
SSL certificate is expired — we pass ignore_https_errors=True.
"""
import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright
from ._base import _UA, block_resources

_URL = "https://www.coldstorage.com.sg/search?q={}"
_MAX_SCROLLS = 30

_EXTRACT_JS = r"""() => {
    const MARKERS = ['"initialProducts":', '"products":', '"searchProducts":', '"productList":', '"items":'];
    const seen = new Set();
    const results = [];

    function extractArray(content, markerEnd) {
        const arrStart = content.indexOf('[', markerEnd);
        if (arrStart === -1 || arrStart - markerEnd > 30) return null;
        let depth = 0;
        for (let i = arrStart; i < content.length; i++) {
            if (content[i] === '[') depth++;
            else if (content[i] === ']') {
                depth--;
                if (depth === 0) {
                    try { return JSON.parse(content.slice(arrStart, i + 1)); }
                    catch (e) { return null; }
                }
            }
        }
        return null;
    }

    for (const entry of (window.__next_f || [])) {
        if (!Array.isArray(entry) || typeof entry[1] !== 'string') continue;
        const content = entry[1];
        for (const marker of MARKERS) {
            let searchFrom = 0;
            let idx;
            while ((idx = content.indexOf(marker, searchFrom)) !== -1) {
                searchFrom = idx + 1;
                const arr = extractArray(content, idx + marker.length);
                if (!Array.isArray(arr) || arr.length === 0) continue;
                // Must contain productId to be a product list
                if (!arr[0] || typeof arr[0] !== 'object' || !arr[0].productId) continue;
                for (const p of arr) {
                    if (p.productId && !seen.has(p.productId)) {
                        seen.add(p.productId);
                        results.push(p);
                    }
                }
            }
        }
    }
    return results;
}"""


_PRODUCT_KEYS = ("products", "searchProducts", "productList", "items", "initialProducts", "data")

def _find_products(obj) -> list | None:
    """Recursively search for a known product-list key containing product dicts."""
    if isinstance(obj, dict):
        for key in _PRODUCT_KEYS:
            v = obj.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict) and "productId" in v[0]:
                return v
        for val in obj.values():
            found = _find_products(val)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_products(item)
            if found is not None:
                return found
    return None


_T_PREFIX = re.compile(r'^T[0-9a-fA-F]+,')
# Any of these strings appearing on a line suggests it may contain product data
_SCROLL_RSC_HINTS = ('"products"', '"searchProducts"', '"productList"', '"initialProducts"', '"productId"')


def _parse_scroll_response(body: bytes) -> list[dict]:
    """Extract products from an RSC fetch response body."""
    results = []
    try:
        text = body.decode("utf-8", "replace")
        for line in text.split("\n"):
            line = line.strip()
            if not line or not any(h in line for h in _SCROLL_RSC_HINTS):
                continue
            colon_idx = line.find(":")
            if colon_idx == -1:
                continue
            payload = line[colon_idx + 1:]
            # Handle Next.js RSC text-blob format "Tlen,"
            if _T_PREFIX.match(payload):
                payload = payload[payload.index(",") + 1:]
            if not payload:
                continue
            if payload[0] not in ('{', '['):
                for ch in ('{', '['):
                    idx = payload.find(ch)
                    if idx != -1:
                        payload = payload[idx:]
                        break
                else:
                    continue
            try:
                data = json.loads(payload)
            except Exception:
                continue
            products = _find_products(data)
            if products:
                results.extend(products)
    except Exception:
        pass
    return results


async def search_coldstorage(query: str, browser=None) -> list[dict]:
    collected: list[dict] = []
    seen_ids: set = set()
    pending_responses: list = []

    own_browser = browser is None
    _pw = None
    if own_browser:
        _pw = await async_playwright().start()
        browser = await _pw.chromium.launch(headless=True)

    ctx = await browser.new_context(
        user_agent=_UA,
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 900},
    )
    pg = await ctx.new_page()
    await pg.route("**/*", block_resources)

    rsc_event = asyncio.Event()

    async def handle_response(resp):
        if "coldstorage.com.sg" not in resp.url or resp.status != 200:
            return
        ct = resp.headers.get("content-type", "")
        # Only read RSC/API responses — never JS bundles or HTML pages
        if not any(x in ct for x in ("x-component", "text/plain", "application/json", "octet-stream")):
            return
        try:
            body = await resp.body()
            pending_responses.append(body)
            rsc_event.set()
        except Exception:
            pass

    pg.on("response", handle_response)

    try:
        url = _URL.format(query)
        await pg.goto(url, wait_until="domcontentloaded", timeout=28_000)

        try:
            await pg.wait_for_function(
                """() => (window.__next_f || []).some(
                    e => Array.isArray(e) && typeof e[1] === 'string'
                      && e[1].includes('"initialProducts"')
                )""",
                timeout=10_000,
            )
        except Exception:
            pass

        title = await pg.title()
        print(f"[cold] loaded: {title} | {pg.url}")

        # Source 1: initial RSC stream
        initial_raw = await pg.evaluate(_EXTRACT_JS)
        for item in initial_raw:
            pid = item.get("productId")
            if pid not in seen_ids:
                seen_ids.add(pid)
                collected.append(item)
        print(f"[cold] initial RSC: {len(initial_raw)} items")

        # Source 2: scroll to trigger lazy-loading.
        # Keep timeouts short (1.5s RSC wait, 4 consecutive misses) so total
        # scroll time stays well within the 45s asyncio.wait_for budget.
        current_y = 0
        scroll_height = await pg.evaluate("() => document.body.scrollHeight")
        consecutive_no_rsc = 0
        for scroll_n in range(_MAX_SCROLLS):
            rsc_event.clear()
            prev_count = len(pending_responses)
            current_y = min(current_y + 800, scroll_height)
            await pg.evaluate(f"window.scrollTo(0, {current_y})")
            try:
                await asyncio.wait_for(rsc_event.wait(), timeout=3.0)
                scroll_height = await pg.evaluate("() => document.body.scrollHeight")
                consecutive_no_rsc = 0
            except asyncio.TimeoutError:
                consecutive_no_rsc += 1
                if consecutive_no_rsc >= 4:
                    print(f"[cold] no RSC for 4 consecutive scrolls, stopping")
                    break
            if len(pending_responses) == prev_count and current_y >= scroll_height:
                print(f"[cold] reached bottom on scroll {scroll_n + 1}, stopping")
                break

        # Re-run _EXTRACT_JS after scrolling — window.__next_f may have new RSC entries
        post_scroll_raw = await pg.evaluate(_EXTRACT_JS)
        new_from_post = 0
        for item in post_scroll_raw:
            pid = item.get("productId")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                collected.append(item)
                new_from_post += 1
        print(f"[cold] post-scroll __next_f: {len(post_scroll_raw)} total, {new_from_post} new unique")
        print(f"[cold] pending_responses: {len(pending_responses)} RSC bodies ({sum(len(b) for b in pending_responses)} bytes)")

    except Exception as exc:
        print(f"[cold] error: {exc}")
    finally:
        asyncio.ensure_future(ctx.close())
        if own_browser and _pw:
            await browser.close()
            await _pw.stop()

    # Merge scroll-triggered RSC responses
    for i, body in enumerate(pending_responses):
        text = body.decode("utf-8", "replace")
        has_pid = '"productId"' in text
        items = _parse_scroll_response(body)
        new_from_body = 0
        for item in items:
            pid = item.get("productId")
            if pid not in seen_ids:
                seen_ids.add(pid)
                collected.append(item)
                new_from_body += 1
        if items or len(body) > 500:
            print(f"[cold] scroll RSC body {i}: {len(body)}b hasPid:{has_pid} → {len(items)} products ({new_from_body} new) | {repr(text[:120])}")

    print(f"[cold] total collected: {len(collected)} products")

    products = []
    for item in collected:
        name    = (item.get("name") or "").strip()
        regular = item.get("price")
        promo   = item.get("promoPrice")

        if not name or regular is None:
            continue

        inventory = (item.get("inventoryStatus") or "").lower()
        sold_out  = bool(inventory) and inventory not in ("in stock", "available")

        if sold_out:
            current_price  = float(regular)
            original_price = None
        else:
            current_price  = float(promo) if promo else float(regular)
            original_price = float(regular) if promo else None
        promo_text = "SOLD OUT" if sold_out else (item.get("discountLabel") or None)

        products.append({
            "name":           name,
            "brand":          "",
            "price":          current_price,
            "original_price": original_price,
            "promo":          promo_text,
            "unit":           "",
            "image":          item.get("image") or "",
            "barcode":        None,
            "category":       "",
            "store":          "cold",
            "scraped_at":     datetime.utcnow(),
        })

    print(f"[cold] parsed {len(products)} products")
    return products
