import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db, init_db
from models import Store, Product, Price, ScrapedQuery
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

PRICE_TTL_HOURS = 6


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
        await db.flush()
    else:
        if raw.get("image"):
            product.image = raw["image"]
        if raw.get("brand"):
            product.brand = raw["brand"]
        if raw.get("unit"):
            product.unit = raw["unit"]

    db.add(Price(
        product_id=product.id,
        store_id=store.id,
        price=raw["price"],
        original_price=raw.get("original_price"),
        promo=raw.get("promo"),
        scraped_at=raw.get("scraped_at", datetime.utcnow()),
    ))


async def _fresh_prices(db: AsyncSession, query: str, since: datetime | None = None) -> list[dict]:
    cutoff = since if since is not None else datetime.utcnow() - timedelta(hours=PRICE_TTL_HOURS)
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
    import math
    rows = (await db.execute(stmt)).all()
    seen: set[tuple] = set()
    results = []
    for price, product, store in rows:
        key = (price.product_id, price.store_id)
        if key in seen:
            continue
        seen.add(key)
        if price.price is not None and math.isfinite(float(price.price)):
            results.append(_fmt_row(price, product, store))
    return results


async def _query_was_scraped(db: AsyncSession, query: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=PRICE_TTL_HOURS)
    r = await db.execute(
        select(ScrapedQuery)
        .where(ScrapedQuery.query == query.lower().strip())
        .where(ScrapedQuery.scraped_at >= cutoff)
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


def _safe_float(v) -> float | None:
    """Return None for NaN/Inf so json.dumps never raises ValueError."""
    import math
    if v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _fmt_row(price: Price, product: Product, store: Store) -> dict:
    return {
        "product_id":     product.id,
        "product_name":   product.name,
        "brand":          product.brand,
        "unit":           product.unit,
        "image":          product.image,
        "store_key":      store.key,
        "store_name":     store.name,
        "store_color":    store.color,
        "price":          _safe_float(price.price),
        "original_price": _safe_float(price.original_price),
        "promo":          price.promo,
        "scraped_at":     price.scraped_at.isoformat(),
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), fresh: bool = False):
    async def event_stream():
        # 1. Emit cached results immediately
        async with _session() as db:
            cached = await _fresh_prices(db, q)
            already_scraped = (not fresh) and await _query_was_scraped(db, q)

        yield _sse({"type": "results", "source": "cache", "results": cached})

        if already_scraped:
            yield _sse({"type": "done"})
            return

        # 2. Run each scraper independently; emit after each one saves
        scrape_start = datetime.utcnow()

        async def run_one(store_key, fn):
            try:
                return store_key, await fn(q)
            except Exception as exc:
                print(f"[{store_key}] error: {exc}")
                return store_key, []

        tasks = [asyncio.create_task(run_one(k, fn)) for k, fn in SCRAPERS.items()]

        for fut in asyncio.as_completed(tasks):
            store_key, results = await fut
            if not results:
                continue
            async with _session() as db:
                store = await _get_store(db, store_key)
                if not store:
                    continue
                for raw in results:
                    try:
                        await _upsert_price(db, store, raw)
                    except Exception as exc:
                        print(f"[{store_key}] upsert error: {exc}")
                await db.commit()
                updated = await _fresh_prices(db, q, since=scrape_start if fresh else None)
            yield _sse({"type": "results", "source": "live", "results": updated})

        # 3. Record this query so subsequent searches hit cache
        async with _session() as db:
            db.add(ScrapedQuery(query=q.lower().strip()))
            await db.commit()

        yield _sse({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history/{product_id}")
async def price_history(product_id: int, days: int = 30, db: AsyncSession = Depends(get_db)):
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


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
