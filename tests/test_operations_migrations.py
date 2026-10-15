"""Upgrade a populated prior release without application create_all()."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_operations_upgrade_keeps_existing_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                       check=True, capture_output=True, text=True)

    alembic("upgrade", "8d7f8177e403")
    with sqlite3.connect(db_path) as db:
        db.execute("INSERT INTO work_centers (id, name, hourly_rate) VALUES (1, 'legacy center', 35)")
        db.execute("""INSERT INTO users (id, username, full_name, role, phone, created_at)
                      VALUES (1, 'legacy', 'legacy', 'engineer', '', '2026-10-01 00:00:00')""")
        db.commit()
    alembic("upgrade", "head")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT name, hourly_rate, is_active, version FROM work_centers").fetchone() == (
            "legacy center", 35, 1, 1)
        assert db.execute("SELECT username FROM users").fetchone()[0] == "legacy"
        db.execute("SELECT * FROM landed_costs")
    alembic("check")
    # Rolling back only the new revisions preserves pre-existing center data.
    alembic("downgrade", "8d7f8177e403")
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT name, hourly_rate FROM work_centers").fetchone() == ("legacy center", 35)
    alembic("upgrade", "head")
