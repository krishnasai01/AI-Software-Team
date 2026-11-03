import os

import psycopg
import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def db(monkeypatch):
    """A freshly migrated database (wiped first), with one product registered."""
    from core import db as core_db
    from core.settings import get_settings
    from db.migrate import migrate

    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    monkeypatch.setenv("TRUSTED_LABELERS", '["owner"]')
    monkeypatch.setenv("HUMAN_APPROVERS", '["owner"]')
    monkeypatch.setenv("GITHUB_APP_IDS", '{"orchestrator": 1, "engineer": 2, "reviewer": 3}')
    get_settings.cache_clear()
    await core_db.close_pool()
    assert migrate(TEST_DB) == ["0001_init.sql", "0002_v2.sql"]
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO products (name, repo, sandbox_image) "
            "VALUES ('shop', 'acme/shop', 'agent-company/sandbox-rehearsal:test')"
        )
    yield
    await core_db.close_pool()
    get_settings.cache_clear()
