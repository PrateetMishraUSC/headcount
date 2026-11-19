import psycopg
import pytest

from headcount.migrate import MIGRATIONS_DIR, MigrationError, discover, migrate


def table_names(url):
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
        ).fetchall()
    return [r[0] for r in rows]


def test_applies_all_migrations_then_nothing_on_rerun(blank_database):
    first = migrate(blank_database, log=lambda _m: None)
    assert first == [path.name for _, path in discover()]
    assert {"users", "clubs", "events", "rsvps", "notifications", "schema_migrations"} <= set(
        table_names(blank_database)
    )

    second = migrate(blank_database, log=lambda _m: None)
    assert second == []

    with psycopg.connect(blank_database) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert versions == [v for v, _ in discover()]


def test_failed_migration_rolls_back_and_is_retried(blank_database, tmp_path):
    (tmp_path / "0001_good.sql").write_text("CREATE TABLE good (id int);")
    (tmp_path / "0002_bad.sql").write_text(
        "CREATE TABLE half_done (id int);\nSELECT * FROM no_such_table;"
    )

    with pytest.raises(MigrationError, match="0002_bad.sql"):
        migrate(blank_database, tmp_path, log=lambda _m: None)

    tables = table_names(blank_database)
    assert "good" in tables
    assert "half_done" not in tables  # rolled back with its migration

    # Fix the file; only 0002 runs on the next attempt.
    (tmp_path / "0002_bad.sql").write_text("CREATE TABLE half_done (id int);")
    assert migrate(blank_database, tmp_path, log=lambda _m: None) == ["0002_bad.sql"]


def test_rejects_badly_named_or_duplicate_files(tmp_path):
    (tmp_path / "1_oops.sql").write_text("")
    with pytest.raises(MigrationError, match="must look like"):
        discover(tmp_path)

    (tmp_path / "1_oops.sql").unlink()
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0001_b.sql").write_text("")
    with pytest.raises(MigrationError, match="share number 0001"):
        discover(tmp_path)


def test_real_migration_files_are_well_formed():
    assert discover(MIGRATIONS_DIR)[0][0] == "0001"
