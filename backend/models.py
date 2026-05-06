from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Index
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
        Index("ix_products_name_lower", "name"),
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
        Index("ix_prices_product_store_time", "product_id", "store_id", "scraped_at"),
    )
