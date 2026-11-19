"""Request-scoped dependencies shared by the API and the pages."""

from collections.abc import Iterator

import psycopg
from fastapi import Request

from headcount.config import Settings, get_settings
from headcount.db import connect


def settings_for(request: Request) -> Settings:
    app = request.app
    if app.state.settings is None:  # resolved on first request, not at import
        app.state.settings = get_settings()
    return app.state.settings


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    """A fresh autocommit connection for this request, with a statement_timeout."""
    settings = settings_for(request)
    conn = connect(
        settings.database_url,
        autocommit=True,
        statement_timeout_ms=settings.statement_timeout_ms,
        connect_timeout_s=settings.connect_timeout_s,
        application_name="headcount-web",
    )
    try:
        yield conn
    finally:
        conn.close()
