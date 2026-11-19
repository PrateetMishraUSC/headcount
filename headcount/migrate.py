"""Apply numbered SQL migrations from `migrations/` in order.

    python -m headcount.migrate            # uses DATABASE_URL
    python -m headcount.migrate --list     # show applied / pending

Each file runs in its own transaction together with its row in
`schema_migrations`, so a failing migration leaves no trace and the next run
retries it. A Postgres advisory lock makes concurrent runs (two deploys at
once) wait for each other instead of racing.
"""

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path

from headcount.config import ROOT, require
from headcount.db import connect

MIGRATIONS_DIR = ROOT / "migrations"
FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Arbitrary constant identifying "the headcount migration lock".
ADVISORY_LOCK_KEY = 4_826_101
# A migration that has to wait longer than this for a table lock gives up
# instead of queueing every web request behind it. Rerun it when it's quieter.
DEFAULT_LOCK_TIMEOUT = "5s"


class MigrationError(Exception):
    pass


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """Return [(version, path)] sorted by version; reject malformed names."""
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"{path.name}: migration files must look like 0001_some_name.sql"
            )
        version = match.group(1)
        if version in found:
            raise MigrationError(
                f"two migrations share number {version}: "
                f"{found[version].name} and {path.name}"
            )
        found[version] = path
    return sorted(found.items())


def migrate(
    url: str,
    directory: Path = MIGRATIONS_DIR,
    *,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Apply pending migrations. Returns the file names applied this run."""
    migrations = discover(directory)
    applied_now: list[str] = []
    with connect(url, autocommit=True, application_name="headcount-migrate") as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    text PRIMARY KEY,
                    name       text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            done = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations")
            }
            for version, path in migrations:
                if version in done:
                    continue
                log(f"applying {path.name}")
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('lock_timeout', %s, true)", (lock_timeout,))
                        conn.execute(path.read_text())
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                            (version, path.name),
                        )
                except Exception as exc:
                    raise MigrationError(f"{path.name} failed and was rolled back: {exc}") from exc
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    if not applied_now:
        log("database is up to date")
    return applied_now


def status(url: str, directory: Path = MIGRATIONS_DIR) -> list[tuple[str, bool]]:
    with connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ok").fetchone()["ok"]
        done = set()
        if exists:
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
    return [(path.name, version in done) for version, path in discover(directory)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", help="defaults to $DATABASE_URL")
    parser.add_argument("--list", action="store_true", help="show applied and pending migrations")
    parser.add_argument("--lock-timeout", default=DEFAULT_LOCK_TIMEOUT, help="per-migration lock_timeout (default 5s)")
    args = parser.parse_args(argv)
    url = args.database_url or require("DATABASE_URL")
    try:
        if args.list:
            for name, applied in status(url):
                print(f"{'applied' if applied else 'pending'}  {name}")
        else:
            migrate(url, lock_timeout=args.lock_timeout)
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
