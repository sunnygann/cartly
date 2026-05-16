# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- **Never** run `git commit`, `git push`, or `surge` without explicit user permission.
- Only modify code files. Do not run deployment or publish commands autonomously.
- At the end of every prompt, if any changes were made to any codebase, make clear which files were edited.

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
    → if query not recently scraped, launches all scrapers in parallel via asyncio.create_task
      → each scraper runs in the shared Chromium browser (or via Algolia for Giant)
      → as each scraper finishes, results are upserted to DB and streamed back live
    → when all scrapers finish, ScrapedQuery record is written (marks query as cached)
  → frontend renders/re-renders results on each SSE message
  → SSE stream closes on "done" event
```

The `?fresh=1` flag (visible only at `?debug` in URL) bypasses the `ScrapedQuery` cache and forces a full re-scrape. `ScrapedQuery` is only written if at least one scraper returned results — a total failure (network outage, geo-block) will not cache an empty result set.

### Backend (`backend/`)

**`main.py`** — Core of the application.
- `lifespan` context manager handles startup (DB init, browser pre-warm, store seeding) and shutdown (browser teardown). Replaces the deprecated `@app.on_event` pattern.
- `_upsert_price()` — Atomically inserts a product using `INSERT ... ON CONFLICT DO NOTHING` against the unique `lower(name)` index, then SELECTs the canonical row and updates optional fields (image, brand, unit). This prevents duplicate product rows from concurrent scrapers.
- `_fresh_prices()` — Returns all prices scraped within the TTL window matching the query words. Uses a subquery to select only the most recent price per `(product_id, store_id)` pair.
- `_query_was_scraped()` — Checks `ScrapedQuery` table; if the same query was scraped within 6 hours, skips re-scraping.
- `ScrapedQuery` is only written after a successful scrape (at least one store returned results), preventing a network failure from poisoning the cache.
- SSE stream emits `{type: "results", source: "cache"|"live", results: [...]}` as each data source completes, then `{type: "done"}`.

**`database.py`** — Async SQLAlchemy engine setup. Converts Railway's `postgres://` URL to `postgresql+asyncpg://` (required by asyncpg). Exports `AsyncSessionLocal` and `get_db` dependency.

**`browser.py`** — Manages a single shared Playwright Chromium instance across all scrapers.
- Protected by `asyncio.Lock` to prevent two coroutines from launching a new browser simultaneously (e.g. after a crash).
- Browser is pre-warmed at startup so the first search request doesn't pay the launch cost.
- All scrapers receive the shared `browser` object and open their own `BrowserContext` within it. They never close the browser itself, only their context.

**`models.py`** — Four SQLAlchemy ORM models:

| Table | Key columns | Notes |
|-------|-------------|-------|
| `stores` | `key`, `name`, `color` | Seeded at startup; never changes at runtime |
| `products` | `name`, `brand`, `unit`, `image`, `barcode`, `category` | One row per unique product (case-insensitive name). Unique index on `lower(name)`. |
| `prices` | `product_id`, `store_id`, `price`, `original_price`, `promo`, `scraped_at` | One row per scrape event. Grows unboundedly; indexed on `(scraped_at, product_id, store_id)`. |
| `scraped_queries` | `query`, `scraped_at` | Records when a query was last fully scraped. TTL = 6 hours. |

### Scrapers (`backend/scrapers/`)

All scrapers follow the same contract: `async def search_X(query: str, browser=None) -> list[dict]`

Each dict has keys: `name, brand, price, original_price, promo, unit, image, barcode, category, store, scraped_at`

There are **no result limits** — scrapers return every product they find.

**Shared base (`_base.py`)**:
- `_EXTRACT_JS` — JavaScript injected into pages to extract prices via DOM text-node iteration. Walks up the DOM tree from each price node to find product name, image, original price, and promo text. Used by NTUC, RedMart, Donki, and Sheng Siong (fallback path).
- `block_resources()` — Playwright route handler that aborts image/font/media/stylesheet requests to speed up page loads.
- `_is_relevant()` — Filters extracted products to keep only those whose names contain at least one meaningful query word. Used by RedMart and Donki (Lazada pages return mixed results).
- `_UA` — Shared desktop Chrome user-agent string.

**NTUC (`ntuc.py`)**: Loads `fairprice.com.sg/search?query=...`. Uses `_EXTRACT_JS` for DOM extraction. Extracts unit info from a dedicated DOM element (the only scraper that reliably gets unit data from the site itself).

**Cold Storage (`coldstorage.py`)**: Two-phase RSC (React Server Components) extraction.
1. Reads `window.__next_f` array on page load → finds `initialProducts` JSON array (initial page render).
2. Intercepts network responses during scroll → captures lazy-loaded product batches.
Deduplicates by `productId`. Uses `inventoryStatus` field for sold-out detection; marks sold-out items with `promo = "SOLD OUT"`. Does not use `_EXTRACT_JS`.

**Giant (`giant.py`)**: Does NOT use DOM or browser. Calls Algolia InstantSearch directly (app `PFCHI1YM66`, index `giant_product_live`). Algolia DNS is geo-restricted to Singapore ISP networks — returns empty results on non-SG networks. Works correctly on Railway Singapore. **Giant results are stored in the DB but the frontend currently hides Giant behind a "coming soon" chip.**

**Sheng Siong (`shengsiong.py`)**: Three-path extraction with fallback chain.
1. JSON-LD structured data (`<script type="application/ld+json">`) — fastest and most accurate; used when available.
2. DOM sweep: finds price elements by CSS class, walks up the DOM tree to find name and image. Image walk restarts from the original price element (independent of name walk depth) to avoid landing on page-level ancestors.
3. (Implicit) Returns empty if both paths fail.

**RedMart (`redmart.py`)**: Scrapes `lazada.sg` catalog with `seller_type=official` filter. Uses `_EXTRACT_JS` + `_is_relevant()` post-filter. Falls back to `redmart.lazada.sg` URLs.

**Don Don Donki (`donki.py`)**: Scrapes Lazada brand pages for Don Don Donki. Uses `_EXTRACT_JS` + `_is_relevant()`. Extracts `promo` and `original_price` from `_EXTRACT_JS` output (Lazada shows strikethrough prices and promo badges in DOM).

### Frontend (`index.html`)

Single-file frontend. No build step. Deployed to Surge manually.

**Search flow**:
1. `doSearch()` opens an `EventSource` SSE connection and immediately shows the "Searching stores…" animated badge.
2. On each `results` message, calls `renderResults()` which re-groups and re-renders the full result set.
3. On `done`, closes the stream and hides the badge.

**`groupByProduct(results, storeVariantCount)`**: Clusters results from different stores into product cards using a multi-gate matching algorithm:
- Gate 0: identical image URL → definite match
- Gate 1: brand must match if both known
- Gate 2: parsed quantity (g/ml/ct) must match within 5%; uses price ratio as proxy when quantity is unparseable
- Gate 3: IDF-weighted keyword Jaccard similarity ≥ 0.32 on quantity-stripped names
- Gate 4: high-IDF discriminating words that appear in one name but not the other veto the match

`kws()` results are memoized within each `groupByProduct()` call (Map keyed by product name) to avoid redundant string operations in the O(n²) pairwise comparison loop.

**Basket**: In-memory `basket` array. Add/qty/remove buttons use `data-*` attributes + event delegation (no `onclick` attribute injection) to avoid XSS. `_addBtnData` Map stores item metadata keyed by `product_id-store_key`. `maxPriceCache` is reset on each `renderResults()` call so savings calculations don't use prices from a prior search.

**Dev mode**: The "rescrape (dev)" toggle is hidden by default. Add `?debug` to the URL to reveal it (e.g. `https://cartly.surge.sh/?debug`).

---

## Key constants

- `PRICE_TTL_HOURS = 6` in `main.py` — cache lifetime before re-scrape
- `_SCRAPER_EXPECTED_S = 25` in `index.html` — expected worst-case scrape time shown in progress bar
- Algolia credentials in `giant.py` — app ID `PFCHI1YM66`, index `giant_product_live`
- Store keys: `ntuc`, `giant`, `cold`, `sheng`, `red`, `donki`
- Brand colors: NTUC `#e8231a`, Giant `#f5a623`, Cold Storage `#0066cc`, Sheng Siong `#2ecc71`, RedMart `#e84393`, Don Don Donki `#e60012`
- App accent color: `#12b76a` (green)
- Grouping similarity threshold: `0.32` (`THRESHOLD` in `groupByProduct`)
- Discriminating word IDF veto threshold: `0.8` (`DISC` in `isSameProduct`)
