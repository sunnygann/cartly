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
```
# Via Railway's PostgreSQL dashboard or CLI
DELETE FROM scraped_queries WHERE query = 'your query';
DELETE FROM prices WHERE product_id IN (SELECT id FROM products WHERE LOWER(name) LIKE '%keyword%');
```

## Architecture

**Request flow**: `index.html` → `GET /api/search?q=...` (SSE stream) → backend checks DB for fresh results (< 6h old) → if stale, runs all scrapers in parallel → saves to DB → streams results back as they arrive.

**Backend** (`backend/`): FastAPI + SQLAlchemy async + asyncpg on PostgreSQL.

- `main.py` — API routes, `_fresh_prices()` query, scraper orchestration, `_upsert_price()` upsert logic. Accepts `?fresh=1` to bypass the scraped-query cache.
- `database.py` — async engine, `DATABASE_URL` handling (converts `postgres://` → `postgresql+asyncpg://`)
- `models.py` — three tables: `Store`, `Product`, `Price`. `ScrapedQuery` tracks which queries have been scraped recently.
- `scrapers/` — one file per store, all return `list[dict]` with keys: `name, brand, price, original_price, promo, unit, image, barcode, category, store, scraped_at`

**Cold Storage scraper** (`coldstorage.py`): Two-phase extraction.
1. `window.__next_f` RSC stream → `initialProducts` (30 in-stock items pre-rendered on page load).
2. Intercepts RSC fetch responses triggered by scrolling to bottom → additional items including sold-out ones.
Deduplicates by `productId`. Uses `inventoryStatus` field for sold-out detection; marks sold-out items with `promo = "SOLD OUT"`.

**Giant scraper** (`giant.py`): Does NOT use DOM extraction. Giant uses Algolia InstantSearch (app `PFCHI1YM66`, index `giant_product_live`) with DNS geo-restricted to Singapore ISP networks. Returns empty on non-SG networks — works correctly on Railway Singapore.

**Sold-out handling**: Items with `promo = "SOLD OUT"` are stored in the DB and returned in search results. The frontend renders them dimmed at the bottom of each card with a grey "Sold Out" badge and no "+ Add" button. The card is not greyed out if other stores have the item available.

**Grouping** (frontend only, `index.html`): `groupByProduct()` uses IDF-weighted keyword overlap (threshold 0.40) to cluster results from different stores into one card. One result per store per group. `extractMeta()` parses quantity/size tokens from product names for display.

## Key constants

- `PRICE_TTL_HOURS = 6` in `main.py` — cache lifetime before re-scrape
- `_SCRAPER_EXPECTED_S = 25` in `index.html` — expected worst-case scrape time shown in progress bar
- Algolia credentials in `giant.py` — app ID `PFCHI1YM66`, index `giant_product_live`
- Store keys: `ntuc`, `giant`, `cold`, `sheng`, `red`, `donki`
- Brand colors: NTUC `#e8231a`, Giant `#f5a623`, Cold Storage `#0066cc`, Sheng Siong `#2ecc71`, RedMart `#e84393`, Don Don Donki `#e60012`
- App accent color: `#12b76a` (green)
