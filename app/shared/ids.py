"""Transactional document-number generation (MR-2026-000145, PO-..., GRN-...)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.database import Base


class DocSequence(Base):
    __tablename__ = "doc_sequences"
    __table_args__ = (UniqueConstraint("prefix", "year", name="uq_prefix_year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    prefix: Mapped[str] = mapped_column(String(8), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


def next_number(db, prefix: str) -> str:
    """Allocate the next sequential number inside the caller's transaction."""
    year = datetime.now(timezone.utc).year
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Unsupported sequence backend: {dialect}")
    # The unique-key UPSERT covers both first allocation and increment in one
    # statement. SELECT followed by +=1 loses updates and races on first use.
    statement = (
        insert(DocSequence).values(prefix=prefix, year=year, value=1)
        .on_conflict_do_update(
            index_elements=["prefix", "year"],
            set_={"value": DocSequence.value + 1},
        ).returning(DocSequence.value)
    )
    value = db.execute(statement).scalar_one()
    return f"{prefix}-{year}-{value:06d}"
