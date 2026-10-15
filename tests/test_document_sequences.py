"""Transactional number allocation used by manufacturing material issues."""
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_first_and_existing_sequence_allocations(client):
    from app.platform import database
    from app.shared.ids import next_number

    def allocate(_):
        with database.SessionLocal() as db:
            number = next_number(db, "QA")
            db.commit()
            return number

    with ThreadPoolExecutor(max_workers=6) as pool:
        first = list(pool.map(allocate, range(12)))
        second = list(pool.map(allocate, range(12)))
    numbers = first + second
    assert len(set(numbers)) == 24
    assert sorted(int(n.rsplit("-", 1)[1]) for n in numbers) == list(range(1, 25))


def test_sequence_rollback_does_not_commit_other_changes(client):
    from app.platform import database
    from app.shared.ids import next_number
    from app.modules.projects.models import ProductionUnit

    with database.SessionLocal() as db:
        first = next_number(db, "ROLL")
        db.add(ProductionUnit(house_no="ROLLBACK-UNIT"))
        db.flush()
        db.rollback()
    with database.SessionLocal() as db:
        assert db.query(ProductionUnit).filter_by(house_no="ROLLBACK-UNIT").count() == 0
        assert next_number(db, "ROLL") == first
        db.commit()
