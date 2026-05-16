from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Index, text
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class Store(Base):
    __tablename__ = "stores"
    id     = Column(Integer, primary_key=True)
    key    = Column(String, unique=True, nullable=False)
    name   = Column(String, nullable=False)
    color  = Column(String, default="#888888")
    prices = relationship("Price", back_populates="store")


class Product(Base):
    __tablename__ = "products"
    id       = Column(Integer, primary_key=True)
    name     = Column(String, nullable=False)
    brand    = Column(String)
    unit     = Column(String)
    image    = Column(String)
    barcode  = Column(String)
    category = Column(String)
    prices   = relationship("Price", back_populates="product")

    __table_args__ = (
        # Functional unique index prevents duplicate product rows from concurrent scrapers.
        # On existing DBs, run: DROP INDEX IF EXISTS ix_products_name_lower;
        #                        CREATE UNIQUE INDEX uix_products_name_lower ON products (lower(name));
        Index("uix_products_name_lower", text("lower(name)"), unique=True),
    )


class Price(Base):
    __tablename__ = "prices"
    id             = Column(Integer, primary_key=True)
    product_id     = Column(Integer, ForeignKey("products.id"), nullable=False)
    store_id       = Column(Integer, ForeignKey("stores.id"), nullable=False)
    price          = Column(Float, nullable=False)
    original_price = Column(Float)
    promo          = Column(String)
    scraped_at     = Column(DateTime, default=datetime.utcnow, nullable=False)

    product = relationship("Product", back_populates="prices")
    store   = relationship("Store",   back_populates="prices")

    __table_args__ = (
        # scraped_at leads so the TTL range filter hits the index before grouping by product/store.
        # On existing DBs: DROP INDEX ix_prices_product_store_time;
        #                  CREATE INDEX ix_prices_scraped_at_product_store ON prices (scraped_at, product_id, store_id);
        Index("ix_prices_scraped_at_product_store", "scraped_at", "product_id", "store_id"),
    )


class ScrapedQuery(Base):
    __tablename__ = "scraped_queries"
    id         = Column(Integer, primary_key=True)
    query      = Column(String, nullable=False, index=True)
    scraped_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EmailSignup(Base):
    __tablename__ = "email_signups"
    id          = Column(Integer, primary_key=True)
    email       = Column(String, nullable=False)
    signed_up_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("uix_email_signups_email", "email", unique=True),
    )
