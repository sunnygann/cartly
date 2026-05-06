import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func, text
from sqlalchemy.orm import selectinload

from database import get_db, init_db
from models import Store, Product, Price
from scrapers import SCRAPERS

app = FastAPI(title="Cartly API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORES_SEED = [
    {"key": "ntuc",  "name": "NTUC FairPrice", "color": "#e8231a"},
    {"key": "giant", "name": "Giant",           "color": "#f5a623"},
    {"key": "cold",  "name": "Cold Storage",    "color": "#0066cc"},
    {"key": "sheng", "name": "Sheng Siong",     "color": "#2ecc71"},
    {"key": "red",   "name": "RedMart",         "color": "#e84393"},
    {"key": "donki", "name": "Don Don Donki",   "color": "#e60012"},
]

PRICE_TTL_HOURS = 6  # scrape again after this many hours


@app.on_event("startup")
async def startup():
    await init_db()
    async with _session() as db:
        for s in STORES_SEED:
            exists = await db.execute(select(Store).where(Store.key == s["key"]))
            if not exists.scalar_one_or_none():
                db.add(Store(**s))
        await db.commit()


def _session():
    from database import AsyncSessionLocal
    return AsyncSessionLocal()


# ── helpers ──────────────────────────────────────────────────────────────────

async def _get_store(db: AsyncSession, key: str) -> Optional[Store]:
    r = await db.execute(select(Store).where(Store.key == key))
    return r.scalar_one_or_none()


async def _upsert_price(db: AsyncSession, store: Store, raw: dict):
    """Insert or update the latest price row for a store+product combo."""
    # Find or create product by exact name (normalised lower)
    name_lower = raw["name"].strip()
    r = await db.execute(
        select(Product).where(func.lower(Product.name) == name_lower.lower())
    )
    product = r.scalar_one_or_none()
    if not product:
        product = Product(
            name=name_lower,
            brand=raw.get("brand"),
            unit=raw.get("unit"),
            image=raw.get("image"),
            barcode=raw.get("barcode"),
            category=raw.get("category"),
        )
        db.add(product)
        await db.flush()  # get id

    db.add(Price(
        product_id=product.id,
        store_id=store.id,
        price=raw["price"],
        original_price=raw.get("original_price"),
        promo=raw.get("promo"),
        scraped_at=raw.get("scraped_at", datetime.utcnow()),
    ))


async def _fresh_prices(db: AsyncSession, query: str) -> list[dict]:
    """Return latest price rows for products matching query, if data is fresh."""
    cutoff = datetime.utcnow() - timedelta(hours=PRICE_TTL_HOURS)
    # latest price per product+store
    subq = (
        select(
            Price.product_id,
            Price.store_id,
            func.max(Price.scraped_at).label("latest_at"),
        )
        .where(Price.scraped_at >= cutoff)
        .group_by(Price.product_id, Price.store_id)
        .subquery()
    )
    stmt = (
        select(Price, Product, Store)
        .join(Product, Price.product_id == Product.id)
        .join(Store,   Price.store_id   == Store.id)
        .join(
            subq,
            (subq.c.product_id == Price.product_id)
            & (subq.c.store_id  == Price.store_id)
            & (subq.c.latest_at == Price.scraped_at),
        )
        .where(*[func.lower(Product.name).contains(w) for w in query.lower().split()])
        .order_by(Price.price)
    )
    rows = (await db.execute(stmt)).all()
    return [_fmt_row(price, product, store) for price, product, store in rows]


def _fmt_row(price: Price, product: Product, store: Store) -> dict:
    return {
        "product_id":    product.id,
        "product_name":  product.name,
        "brand":         product.brand,
        "unit":          product.unit,
        "image":         product.image,
        "store_key":     store.key,
        "store_name":    store.name,
        "store_color":   store.color,
        "price":         price.price,
        "original_price": price.original_price,
        "promo":         price.promo,
        "scraped_at":    price.scraped_at.isoformat(),
    }


async def _run_scrapers(query: str):
    """Fire all scrapers for a query and persist results."""
    async with _session() as db:
        tasks = [scraper(query) for scraper in SCRAPERS.values()]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        for store_key, result in zip(SCRAPERS.keys(), all_results):
            if isinstance(result, Exception):
                print(f"[{store_key}] scraper error: {result}")
                continue
            store = await _get_store(db, store_key)
            if not store:
                continue
            for raw in result:
                try:
                    await _upsert_price(db, store, raw)
                except Exception as exc:
                    print(f"[{store_key}] upsert error: {exc}")

        await db.commit()


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns live prices for a query.
    If DB has fresh data (< 6h old), returns immediately.
    Otherwise scrapes live, stores results, then returns.
    """
    fresh = await _fresh_prices(db, q)
    if fresh:
        return {"query": q, "source": "cache", "results": fresh}

    # No fresh data — scrape now and wait
    await _run_scrapers(q)
    results = await _fresh_prices(db, q)
    return {"query": q, "source": "live", "results": results}


@app.get("/api/history/{product_id}")
async def price_history(product_id: int, days: int = 30, db: AsyncSession = Depends(get_db)):
    """Returns per-store price history for a product over the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(Price, Store)
        .join(Store, Price.store_id == Store.id)
        .where(Price.product_id == product_id, Price.scraped_at >= cutoff)
        .order_by(Price.store_id, Price.scraped_at)
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        raise HTTPException(404, "No history found")

    by_store: dict[str, dict] = {}
    for price, store in rows:
        key = store.key
        if key not in by_store:
            by_store[key] = {"store_name": store.name, "color": store.color, "points": []}
        by_store[key]["points"].append({
            "date":  price.scraped_at.strftime("%d %b"),
            "price": price.price,
        })

    return {"product_id": product_id, "days": days, "stores": list(by_store.values())}


@app.get("/api/stores")
async def list_stores(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Store))).scalars().all()
    return [{"key": s.key, "name": s.name, "color": s.color} for s in rows]


@app.post("/api/scrape")
async def trigger_scrape(
    q: str = Query(..., min_length=1),
    background_tasks: BackgroundTasks = None,
):
    """Manually trigger a background scrape for a query."""
    background_tasks.add_task(_run_scrapers, q)
    return {"status": "scrape queued", "query": q}


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
