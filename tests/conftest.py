"""Test fixtures. Tests run against the real Postgres at TEST_DATABASE_URL.

The test database is wiped and rebuilt from migrations at the start of the
session, and every test starts with empty tables.
"""

import os

import psycopg
import pytest

from headcount.config import load_dotenv
from headcount.migrate import migrate

APP_TABLES = ["notifications", "rsvps", "events", "clubs", "users"]


def _test_database_url() -> str:
    load_dotenv()
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.exit("TEST_DATABASE_URL is not set (see .env.example)", returncode=2)
    if url == os.environ.get("DATABASE_URL", "").strip():
        pytest.exit("TEST_DATABASE_URL must differ from DATABASE_URL: tests wipe it", returncode=2)
    return url


def reset_schema(url: str) -> None:
    """Drop everything in the public schema."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    reset_schema(url)
    migrate(url, log=lambda _msg: None)
    return url


@pytest.fixture
def db(database_url):
    """An autocommit connection to an empty, migrated test database."""
    with psycopg.connect(database_url, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE")
        yield conn


@pytest.fixture
def blank_database(database_url):
    """An empty database (no tables at all); fully migrated again afterwards."""
    reset_schema(database_url)
    yield database_url
    reset_schema(database_url)
    migrate(database_url, log=lambda _msg: None)
