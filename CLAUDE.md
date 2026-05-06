# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local development

```bash
# Start backend + PostgreSQL
docker compose up

# Backend auto-reloads on file changes (uvicorn --reload is set in docker-compose.yml)
# API is at http://localhost:8000, docs at http://localhost:8000/docs

# Clear price cache to force re-scrape
docker compose exec db psql -U cartly -c "DELETE FROM prices;"

# Test a scraper result
curl "http://localhost:8000/api/search?q=milo"
```

Frontend is a single `index.html` — open directly in browser, no build step.

## Deployment

- **Backend**: Railway (Singapore region), auto-deploys on `git push` to `main`
- **Frontend**: Surge (`cartly.surge.sh`), deployed manually with `surge` from repo root
- **Database**: Railway PostgreSQL, `DATABASE_URL` injected as env var

To redeploy frontend: `surge` from `/Users/sunny/Cartly/` — confirm domain as `cartly.surge.sh`.

## Architecture

**Request flow**: `index.html` → `GET /api/search?q=...` → backend checks DB for fresh results (< 6h old) → if stale, runs all 5 scrapers in parallel → saves to DB → returns results.

**Backend** (`backend/`): FastAPI + SQLAlchemy async + asyncpg on PostgreSQL.

- `main.py` — API routes, `_fresh_prices()` query, `_run_scrapers()`, `_upsert_price()` upsert logic
- `database.py` — async engine, `DATABASE_URL` handling (converts `postgres://` to `postgresql+asyncpg://`)
- `models.py` — three tables: `Store`, `Product`, `Price` (Store and Product are referenced by Price via FK)
- `scrapers/` — one file per store, all return `list[dict]` with keys: `name, brand, price, original_price, promo, unit, image, barcode, category, store, scraped_at`

**DB query**: `_fresh_prices` uses a subquery to get the latest price per `(product_id, store_id)` pair within the TTL window, then filters products by `AND` of individual query words (word-split matching, not full-string substring).

**Scraper architecture**: All scrapers use Playwright headless Chromium. `_base.py` exports `_EXTRACT_JS` (a DOM text-walker that finds `$X.XX` price nodes and walks up to find name/image) and `scrape_store()` (generic URL-loop helper). Individual scrapers override this where needed.

**Giant scraper** (`giant.py`): Does NOT use DOM extraction. Giant uses Algolia InstantSearch (app `PFCHI1YM66`, index `giant_product_live`) with DNS geo-restricted to Singapore ISP networks. The scraper calls Algolia directly via in-page `fetch()`. Returns empty on non-SG networks (local dev) — works correctly on Railway Singapore.

**Grouping** (frontend only, `index.html`): `groupByProduct()` uses IDF-weighted keyword overlap (threshold 0.40) to cluster results from different stores into one card. One result per store per group. `extractMeta()` parses quantity/size tokens from product names for display.

## Key constants

- `PRICE_TTL_HOURS = 6` in `main.py` — cache lifetime before re-scrape
- Algolia credentials in `giant.py` — app ID `PFCHI1YM66`, index `giant_product_live`
- Store keys: `ntuc`, `giant`, `cold`, `sheng`, `red`
- Brand colors: NTUC `#e8231a`, Giant `#f5a623`, Cold Storage `#0066cc`, Sheng Siong `#2ecc71`, RedMart `#e84393`
- App accent color: `#12b76a` (green)
