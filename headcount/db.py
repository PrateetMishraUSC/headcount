"""Opening database connections. No ORM and no pool: psycopg 3 directly."""

import psycopg
from psycopg.rows import dict_row

DEFAULT_CONNECT_TIMEOUT_S = 5


def connect(
    url: str,
    *,
    statement_timeout_ms: int | None = None,
    connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
    autocommit: bool = False,
    application_name: str = "headcount",
) -> psycopg.Connection:
    """Open a connection whose rows come back as dicts.

    `statement_timeout_ms` is sent as a startup option, so it applies to every
    statement on the connection without an extra round trip. `connect_timeout_s`
    keeps a dead or unreachable database from hanging the caller.
    """
    kwargs: dict = {
        "autocommit": autocommit,
        "row_factory": dict_row,
        "connect_timeout": connect_timeout_s,
        "application_name": application_name,
    }
    if statement_timeout_ms:
        kwargs["options"] = f"-c statement_timeout={int(statement_timeout_ms)}"
    return psycopg.connect(url, **kwargs)
