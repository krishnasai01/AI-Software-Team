"""Apply db/migrations/*.sql in order, once each. Usage: python -m db.migrate"""

from pathlib import Path

import psycopg

from core.settings import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate(dsn: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    for name in migrate(get_settings().database_url):
        print(f"applied {name}")
