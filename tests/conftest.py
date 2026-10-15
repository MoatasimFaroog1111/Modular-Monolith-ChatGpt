"""Shared test fixtures: isolated SQLite DB per test, seeded users."""
from __future__ import annotations

import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("FDE_MIN_QUOTATIONS", "2")

import pytest
from fastapi.testclient import TestClient

import app.platform.database as database
from app.main import app
from app.platform.database import Base


@pytest.fixture()
def client(tmp_path):
    # fresh engine per test for true isolation
    db_file = tmp_path / "test.db"
    database.engine.dispose()
    database.engine = __import__("sqlalchemy").create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    database.SessionLocal = __import__("sqlalchemy").orm.sessionmaker(
        bind=database.engine, autoflush=False, expire_on_commit=False
    )

    Base.metadata.create_all(bind=database.engine)
    from app.main import seed_dev_users

    seed_dev_users()

    # override get_db to use the test engine session
    from app.platform.database import get_db

    def _get_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    database.engine.dispose()
