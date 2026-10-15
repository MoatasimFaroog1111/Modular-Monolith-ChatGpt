"""Database engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.platform.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    engine = create_engine(
        _settings.database_url,
        connect_args={"check_same_thread": False},
    )
else:
    # PostgreSQL reference deployment (pooled).
    engine = create_engine(_settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
