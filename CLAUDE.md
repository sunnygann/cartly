# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- **Never** run `git commit`, `git push`, or `surge` without explicit user permission.
- Only modify code files. Do not run deployment or publish commands autonomously.
- At the end of every prompt, if any changes were made to any codebase, make clear which files were edited.
- **Before updating this file**, read every source file in the project from scratch — `database.py`, `models.py`, `browser.py`, `main.py`, `backend/scrapers/_base.py`, `backend/scrapers/ntuc.py`, `backend/scrapers/coldstorage.py`, `backend/scrapers/shengsiong.py`, `backend/scrapers/redmart.py`, `backend/scrapers/donki.py`, `backend/scrapers/giant.py`, `backend/scrapers/__init__.py`, and `index.html` — so that all descriptions reflect the actual current code, not stale memory.

---

## Infrastructure

**This project has no local dev server. Everything runs on Railway.**

| Layer    | Host                                              | How it deploys                    |
|----------|---------------------------------------------------|-----------------------------------|
| Backend  | Railway (Singapore) — `https://cartly-production-9eb2.up.railway.app` | Auto-deploys on `git push main` via `backend/Dockerfile` |
| Frontend | Surge — `https://cartly.surge.sh`                | Manual: run `surge` from repo root, confirm domain `cartly.surge.sh` |
| Database | Railway PostgreSQL                                | `DATABASE_URL` injected by Railway as env var |

**When testing or debugging the API, always use the Railway URL, not localhost:**

```bash
# Health check
curl https://cartly-production-9eb2.up.railway.app/api/health

# Test a search (streams SSE)
curl -N https://cartly-production-9eb2.up.railway.app/api/search?q=milo

# Force fresh scrape (bypass cache)
curl -N "https://cartly-production-9eb2.up.railway.app/api/search?q=milo&fresh=1"
```

**Never run `docker compose` commands.** `docker-compose.yml` has been removed. The `backend/Dockerfile` exists solely for Railway's build process.

To clear the Railway DB cache (e.g. to force a re-scrape of a query):
```sql
-- Clear a specific query's cache so it re-scrapes next search
DELETE FROM scraped_queries WHERE query = 'your query';
DELETE FROM prices WHERE product_id IN (SELECT id FROM products WHERE LOWER(name) LIKE '%keyword%');
```

---

## Architecture

### Request flow

```
User types query
  → index.html calls GET /api/search?q=... (SSE stream)
    → backend immediately emits any cached DB results (< 6h old)
    → if query not recently scraped, launches all 6 scrapers in parallel via asyncio.create_task
      → each scraper manages its own Playwright browser (or Algolia API for Giant)
      → as each scraper finishes, results are upserted to DB and streamed back live
    → when all scrapers finish, ScrapedQuery record is written (marks query as cached)
  → frontend renders/re-renders results on each SSE message
  → SSE stream closes on "done" event
```

The `?fresh=1` flag bypasses the `ScrapedQuery` cache and forces a full re-scrape. `ScrapedQuery` is only written after at least one scraper returns results — a total failure will not poison the cache with an empty result set.

---

## File-by-file reference

### `backend/database.py`

Sets up the async SQLAlchemy engine. One critical job: Railway injects `DATABASE_URL` as `postgres://...` (psycopg2 syntax), but asyncpg requires `postgresql+asyncpg://...` — this file rewrites the prefix on startup. Exports:
- `engine` — the async engine
- `AsyncSessionLocal` — the session factory used everywhere
- `get_db` — a FastAPI dependency that yields a session per request
- `init_db` — runs `CREATE TABLE IF NOT EXISTS` for all models; called at startup

---

### `backend/models.py`

Four SQLAlchemy ORM classes:

| Table | Key columns | Notes |
|-------|-------------|-------|
| `stores` | `key`, `name`, `color` | Seeded at startup from `STORES_SEED` in `main.py`; never mutated at runtime |
| `products` | `name`, `brand`, `unit`, `image`, `barcode`, `category` | One row per unique product (by lowercase name). Has an index on `name` (not a functional `lower()` index — no unique constraint enforced at DB level). |
| `prices` | `product_id`, `store_id`, `price`, `original_price`, `promo`, `scraped_at` | Append-only price history. One row per scrape event. Composite index on `(product_id, store_id, scraped_at)`. |
| `scraped_queries` | `query`, `scraped_at` | Records when a query was last fully scraped. TTL = 6 hours. |

**Important**: There is no `UNIQUE` constraint on `lower(products.name)`. Concurrent scrapers can create duplicate product rows. `_upsert_price` in `main.py` handles this with `scalars().first()` which tolerates duplicates.

---

### `backend/browser.py`

Manages a single globally shared Playwright Chromium instance via `get_browser()` and `stop_browser()`. Checks `_browser.is_connected()` before reusing; re-launches if crashed.

**Currently unused.** All scrapers manage their own browser lifecycle with `async with async_playwright() as p: browser = await p.chromium.launch(...)`. This file is a leftover from an earlier shared-browser architecture that was rolled back.

---

### `backend/main.py`

The FastAPI application. All API traffic enters here.

**Startup** (`@app.on_event("startup")`): Calls `init_db()` then seeds the 6 stores into the DB if they don't exist.

**`_upsert_price(db, store, raw)`**: Saves one scraped product.
1. Queries for an existing `Product` with the same lowercased name using `scalars().first()` (tolerates duplicate rows).
2. Creates a new `Product` if none found.
3. Always inserts a new `Price` row (history is append-only).
4. Updates `product.image`, `brand`, `unit` if the scraper provided them.

**`_fresh_prices(db, query, since)`**: The main DB read path. Returns prices for products matching all query words scraped within the TTL window. Uses a subquery to select only the **most recent** price per `(product_id, store_id)` pair — so multiple scrapes of the same item in one TTL window only show the latest price.

**`_query_was_scraped(db, query)`**: Checks if a `ScrapedQuery` row exists within the last 6 hours. If yes, skips re-scraping.

**`GET /api/search?q=...&fresh=...`** — Main SSE endpoint:
1. Emits `{type: "results", source: "cache", results: [...]}` immediately.
2. If recently scraped and `fresh=false`, emits `{type: "done"}` and exits.
3. Otherwise, launches all 6 scrapers in parallel via `asyncio.create_task`. As each finishes, upserts its results and emits `{type: "results", source: "live", results: [...]}` with the refreshed full result set.
4. Writes a `ScrapedQuery` row after all scrapers finish.
5. Emits `{type: "done"}`.

**`GET /api/history/{product_id}`**: Returns 30-day price history grouped by store. Used by the frontend chart.

**`GET /api/stores`**: Returns the 6 store rows.

**`GET /api/health`**: Simple uptime check.

---

### `backend/scrapers/__init__.py`

The scraper registry. Maps store keys to scraper functions:

```python
SCRAPERS = {
    "ntuc":  search_ntuc,
    "sheng": search_shengsiong,
    "giant": search_giant,
    "cold":  search_coldstorage,
    "red":   search_redmart,
    "donki": search_donki,
}
```

`main.py` iterates this dict to launch all scrapers.

---

### `backend/scrapers/_base.py`

Shared utilities used by multiple scrapers:

**`_BLOCKED_TYPES` + `block_resources(route)`**: A Playwright route handler. Registered with `page.route("**/*", block_resources)`, it aborts all requests for `image`, `font`, `media`, and `stylesheet` resources — cutting page load time significantly since scrapers only need DOM/JS data.

**`_UA`**: Shared desktop Chrome user-agent string used by all Playwright-based scrapers.

**`_EXTRACT_JS`**: Core DOM extraction script injected as JavaScript into pages. It:
1. Pre-scans the page for promo text islands (e.g. "Any 2 @ $22.00" banners above product grids).
2. Walks every text node in the DOM looking for `$X.XX` price patterns.
3. For each price found, climbs up to 10 ancestor elements to find: the nearest product name (leaf text in `span/p/a/h*`), product image (`img`), struck-through original price (`del`, `[class*="was"]`, etc.), and promo text (elements with classes like "promo", "badge", "label", or text matching multi-buy patterns).
4. Deduplicates by `name|price` key.
5. Returns `[{name, price, image, original_price, promo}]`.

Used directly by RedMart and Donki. NTUC has its own variant of this script. Cold Storage and ShengSiong have completely different extraction approaches.

**`scrape_store()`**: A generic helper that loads a URL, waits, runs `_EXTRACT_JS`, and cleans names. Currently unused by any live scraper — kept as infrastructure.

---

### `backend/scrapers/ntuc.py`

**Target**: `fairprice.com.sg/search?query=...`

**How**:
- Launches its own Playwright browser; registers `block_resources`.
- Waits for `$` to appear in body text (proxy for "products rendered"), then sleeps 2s.
- Runs a **multi-pass adaptive scroll** — scrolls to `scrollHeight`, checks if height grew, repeats up to 20 times with 1.5s waits. Stops after 2 consecutive passes with no height change. Captures all infinite-scroll products.
- Runs a **custom `_EXTRACT_JS` variant** using a promo-first card detection strategy:
  1. Collects all promo labels from `[data-testid="promo-label"]` and multi-buy text patterns across the whole page.
  2. For each price node, climbs the tree. If an ancestor contains a known promo string → that element is the card boundary, promo is captured.
  3. Falls back to "element with `img` + at least 2 children" as the card boundary.
  4. Classifies prices as sale vs. original by checking if the price node is inside a strikethrough selector.
- Cleans product names (strips price strings, "add to cart" noise, promo prefixes).
- No result limit — returns every product found.

---

### `backend/scrapers/coldstorage.py`

**Target**: `coldstorage.com.sg/search?q=...`

**How**: Cold Storage is a Next.js RSC app — product data is not in the rendered HTML as text nodes but embedded as JSON in script tags and streamed over the network.

- Registers a **response interceptor** (`pg.on("response", ...)`) capturing HTTP responses from the Cold Storage search URL with content-type `x-component`/`text/plain`/`application/json` — these are RSC payloads.
- On page load, evaluates JS to read `window.__next_f` (the RSC stream array baked into initial HTML), scanning for `"initialProducts":` and parsing the JSON array that follows. Gives ~30 initial in-stock products.
- Runs a **multi-pass incremental scroll** (up to 15 passes, 2s each). After each scroll, processes newly captured response bodies via `_parse_scroll_response()` which splits lines looking for `{"products": [...]}` JSON. Tracks collected count — stops when stable for 2 consecutive passes.
- Deduplicates by `productId`.
- Uses `inventoryStatus` field for sold-out detection; marks them `promo = "SOLD OUT"`.
- Uses `promoPrice` vs `price` fields for sale detection — no DOM scraping.
- Does not use `_EXTRACT_JS`.

---

### `backend/scrapers/shengsiong.py`

**Target**: `shengsiong.com.sg`

**How**: Three-path extraction with fallback:

1. **Homepage search interaction**: Navigates to homepage, finds search input, types query, presses Enter. If the resulting URL doesn't reflect the query (search didn't fire), retries with direct URL formats (`/search/{query+}`, `/search/{slug}`, `/search?q={query}`).

2. **JSON-LD path** (primary): Reads all `<script type="application/ld+json">` blocks. If any contain `"@type": "Product"` or `"Offer"`, extracts name/price/image from structured data and returns immediately.

3. **DOM sweep** (fallback): Queries all `[class*="price"]` elements. For each, climbs up to 4 levels to find a name element (by class or tag), then up to 3 more levels for an `img`. Collects `{name, price, image, original_price}`.

No result limit on any path.

---

### `backend/scrapers/redmart.py`

**Target**: RedMart catalog on `lazada.sg`

**How**:
- Tries three URL patterns in order: `lazada.sg/catalog/?q=...&seller_type=official`, `redmart.lazada.sg/catalog/?q=...`, `redmart.lazada.sg/search/#q=...`.
- Uses the shared `_EXTRACT_JS` to extract all price nodes from the DOM.
- Post-filters with a local `_is_relevant()` function (defined in this file, not `_base.py`): keeps only products whose names contain at least one meaningful query word (Lazada returns mixed-seller results).
- Stops trying URLs once relevant results are found.
- No result limit.

---

### `backend/scrapers/donki.py`

**Target**: Don Don Donki products on `lazada.sg`

**How**: Almost identical to RedMart. Uses two URL patterns:
- `lazada.sg/catalog/?q=don+don+donki+{query}` — prepends the brand to the search.
- `lazada.sg/catalog/?q={query}&seller_type=official&brand=don-don-donki` — brand filter.

Uses `_EXTRACT_JS` + a local `_is_relevant()` function (defined in this file, not `_base.py`). Stop-on-first-hit URL logic. No result limit.

---

### `backend/scrapers/giant.py`

**Target**: Giant's Algolia search index (no browser)

**How**: The only scraper that does not use Playwright at all. Giant's website uses Algolia InstantSearch with public credentials baked into their JS.

- Resolves the Algolia hostname via **DNS-over-HTTPS** (Cloudflare `1.1.1.1`) because `PFCHI1YM66-dsn.algolia.net` is geo-restricted to Singapore ISP DNS and fails on Railway's datacenter DNS resolver.
- Sends a POST to Algolia's REST API with `hitsPerPage: 1000`.
- Parses the clean JSON response — no DOM scraping.
- Returns empty list if DNS resolution fails (non-SG networks).

**Giant results are stored in the DB but the frontend hides Giant behind a "coming soon" chip** — the `data-store="giant"` chip has `pointer-events:none` and no `onclick`.

---

### `index.html`

Single-file frontend. No build step. Deployed to Surge manually.

#### Layout (top to bottom)
- Sticky nav bar with desktop links + mobile hamburger drawer
- Hero section: animated scrambling headline, search box, "rescrape (dev)" checkbox, popular tag pills
- Stores strip: clickable chips to filter which stores appear in results (`ntuc`, `cold`, `sheng`, `red`, `donki` active; `giant` disabled with "soon" chip)
- Results section (hidden until first search): price-grid + 30-day Chart.js history chart
- Basket builder: item table + order summary sidebar with store-split bar chart
- "How it works" explainer
- Price alert email signup (UI only, no backend wired)
- Footer

#### Search flow

1. `doSearch()` opens an `EventSource` to `/api/search?q=...`. Shows a floating "Searching stores…" badge with animated dots, countdown timer, and progress bar.
2. On each `results` SSE message, calls `renderResults()` which re-renders the full grid from scratch. As `live` messages arrive, the badge shows which stores have responded.
3. On `done`, closes the SSE connection and fades out the badge.

The `fresh-toggle` checkbox (always visible) passes `&fresh=1` to force a re-scrape bypassing the `ScrapedQuery` cache.

#### `groupByProduct(results)`

Takes the flat list of results and clusters them into product cards. Runs an O(n²) pairwise comparison with four gates:

- **Gate 0**: Identical image URL (query-param-stripped) → definite match, bypass name checks.
- **Gate 1**: Both have known brands that differ → not the same product.
- **Gate 2**: Parse quantity from `unit` or `product_name` (handles `g`, `ml`, `kg`, `l`, `pcs`, `pk`, multi-packs like `6x100ml`). If both parseable and differ by >12% → not the same.
- **Gate 3**: IDF-weighted Jaccard similarity on quantity-stripped, lowercased names ≥ `THRESHOLD` (0.32). IDF weights are computed across all results in the current search, so rare brand-specific words dominate over common generic words.

Groups sort: most stores first (descending), then cheapest price. Items within a group: available items by price ascending, sold-out items at the bottom.

#### `renderResults()`

Builds the card grid from groups. Each card:
- Header: product image, name (cleaned via `cleanProductName()` + `extractMeta()`), size/unit subtitle.
- One row per store: color dot, store name, price (with strikethrough original price if on sale), badge ("Best" / "+X%" / "−X%" / promo text / "Sold Out"), "+ Add" button.
- `normalizePromo()` canonicalizes raw promo strings into clean formats ("3 for $5.00", "Buy 2 Get 1 Free", "15% off").

After rendering, calls `loadHistory()` on the first product to draw the Chart.js price history.

#### Basket

In-memory `basket` array (not persisted across page reloads). `addToBasket()`, `removeFromBasket()`, `changeQty()` mutate the array and call `renderBasket()`. Order summary shows total, savings vs. `maxPriceCache` (the highest price seen for each product across stores in the current search), and a store-split bar chart.

#### Store filter

`selectedStores` is a `Set` persisted in `localStorage` under key `cartly_stores`. Toggling a chip re-calls `renderResults(_lastQuery, _lastResults)` without any network request.

---

## Key constants

| Constant | Location | Value | Purpose |
|----------|----------|-------|---------|
| `PRICE_TTL_HOURS` | `main.py` | `6` | Cache lifetime before re-scrape |
| `_SCRAPER_EXPECTED_S` | `index.html` | `25` | Progress bar duration (seconds) |
| `THRESHOLD` | `index.html groupByProduct` | `0.32` | Min IDF-weighted Jaccard for product match |
| Algolia App ID | `giant.py` | `PFCHI1YM66` | Giant Algolia credentials |
| Algolia Index | `giant.py` | `giant_product_live` | Giant Algolia index name |

## Store reference

| Store key | Display name | Color |
|-----------|-------------|-------|
| `ntuc` | NTUC FairPrice | `#e8231a` |
| `giant` | Giant | `#f5a623` |
| `cold` | Cold Storage | `#0066cc` |
| `sheng` | Sheng Siong | `#2ecc71` |
| `red` | RedMart | `#e84393` |
| `donki` | Don Don Donki | `#e60012` |

App accent color: `#12b76a` (green)
