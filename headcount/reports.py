"""Nightly attendance report, per club, for every event that has started so far.

    python -m headcount.reports                 # plain-text table on stdout
    python -m headcount.reports --format csv    # CSV, for spreadsheets
    python -m headcount.reports --all           # include upcoming events too

Meant to run from cron (see docs/RUNBOOK.md); it exits non-zero on failure
so cron's mail or your monitoring notices.
"""

import argparse
import csv
import io
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg

from headcount.config import get_settings
from headcount.db import connect

COLUMNS = ["club", "event_id", "event", "starts_at", "capacity", "confirmed", "waitlisted", "cancelled"]


def attendance(conn: psycopg.Connection, *, include_upcoming: bool = False) -> list[dict]:
    """One row per club and event: capacity and RSVP counts by status.

    Clubs without any (matching) events still appear, with event fields None.
    """
    return conn.execute(
        """
        SELECT c.id AS club_id, c.name AS club, e.id AS event_id, e.title AS event,
               e.starts_at, e.capacity,
               count(r.id) FILTER (WHERE r.status = 'confirmed')  AS confirmed,
               count(r.id) FILTER (WHERE r.status = 'waitlisted') AS waitlisted,
               count(r.id) FILTER (WHERE r.status = 'cancelled')  AS cancelled
          FROM clubs c
          LEFT JOIN events e ON e.club_id = c.id AND (%(all)s OR e.starts_at <= now())
          LEFT JOIN rsvps  r ON r.event_id = e.id
         GROUP BY c.id, e.id
         ORDER BY c.name, e.starts_at NULLS FIRST, e.id
        """,
        {"all": include_upcoming},
    ).fetchall()


def format_csv(rows: list[dict], tz: ZoneInfo) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for r in rows:
        if r["event_id"] is None:
            continue
        writer.writerow([r["club"], r["event_id"], r["event"], r["starts_at"].astimezone(tz).isoformat(),
                         r["capacity"], r["confirmed"], r["waitlisted"], r["cancelled"]])
    return out.getvalue()


def format_text(rows: list[dict], tz: ZoneInfo, generated_at: datetime, include_upcoming: bool) -> str:
    scope = "all events" if include_upcoming else "events that have started"
    lines = [f"Headcount attendance report · {generated_at.astimezone(tz):%Y-%m-%d %H:%M %Z} · {scope}", ""]
    header = f"  {'Date':<16} {'Event':<34} {'Cap':>5} {'Conf':>5} {'Wait':>5} {'Canc':>5}"
    clubs: dict[str, list[dict]] = {}
    for r in rows:
        clubs.setdefault(r["club"], []).append(r)
    for club, club_rows in clubs.items():
        lines.append(club)
        events = [r for r in club_rows if r["event_id"] is not None]
        if not events:
            lines += ["  (no events yet)", ""]
            continue
        lines.append(header)
        for r in events:
            title = r["event"] if len(r["event"]) <= 34 else r["event"][:33] + "…"
            lines.append(
                f"  {r['starts_at'].astimezone(tz):%Y-%m-%d %H:%M} {title:<34} "
                f"{r['capacity']:>5} {r['confirmed']:>5} {r['waitlisted']:>5} {r['cancelled']:>5}"
            )
        totals = {k: sum(r[k] for r in events) for k in ("capacity", "confirmed", "waitlisted", "cancelled")}
        lines.append(
            f"  {'Total':<16} {f'{len(events)} events':<34} {totals['capacity']:>5} "
            f"{totals['confirmed']:>5} {totals['waitlisted']:>5} {totals['cancelled']:>5}"
        )
        lines.append("")
    if not clubs:
        lines.append("No clubs yet.")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=["text", "csv"], default="text")
    parser.add_argument("--all", action="store_true", help="include events that haven't started yet")
    args = parser.parse_args(argv)
    settings = get_settings()
    tz = ZoneInfo(settings.display_timezone)
    # A generous timeout: a report may scan everything, but should never hang cron.
    with connect(settings.database_url, autocommit=True, statement_timeout_ms=120_000,
                 application_name="headcount-reports") as conn:
        rows = attendance(conn, include_upcoming=args.all)
    if args.format == "csv":
        sys.stdout.write(format_csv(rows, tz))
    else:
        sys.stdout.write(format_text(rows, tz, datetime.now(timezone.utc), args.all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
