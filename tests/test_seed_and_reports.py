import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from headcount import reports, rsvps
from headcount.seed import CLUBS, NUM_USERS, DatabaseNotEmpty, seed
from tests.factories import make_club, make_event, make_users

LA = ZoneInfo("America/Los_Angeles")


def db_now(db):
    return db.execute("SELECT now() AS t").fetchone()["t"]


def snapshot(db):
    """Everything about the seeded data except absolute times and timestamps."""
    return db.execute(
        """
        SELECT c.name, e.title, e.capacity, u.display_name, r.status
          FROM rsvps r JOIN events e ON e.id = r.event_id JOIN clubs c ON c.id = e.club_id
          JOIN users u ON u.id = r.user_id
         ORDER BY r.id
        """
    ).fetchall()


# ------------------------------------------------------------ seed


def test_seed_creates_a_realistic_consistent_dataset(db):
    now = db_now(db)
    counts = seed(db, now=now)

    assert counts["clubs"] == len(CLUBS)
    assert counts["users"] == NUM_USERS
    assert counts["events"] == sum(len(events) for _, _, events in CLUBS)
    assert counts["confirmed"] > 0 and counts["waitlisted"] > 0 and counts["cancelled"] > 0
    assert counts["notifications"] > 0  # some freed seats were filled from waitlists

    events = db.execute(
        """SELECT e.*,
                  (SELECT count(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'confirmed')  AS confirmed,
                  (SELECT count(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'waitlisted') AS waiting
             FROM events e"""
    ).fetchall()
    # Times are relative to now: all in the future, within four weeks, and each
    # club's first event within a day, at a sensible local hour.
    assert all(now < e["starts_at"] <= now + timedelta(days=29) for e in events)
    firsts = [min((e for e in events if e["club_id"] == c), key=lambda e: e["id"])
              for c in {e["club_id"] for e in events}]
    assert all(f["starts_at"] <= now + timedelta(hours=24) for f in firsts)
    assert all(9 <= f["starts_at"].astimezone(LA).hour < 22 for f in firsts)
    # Sizes vary, some events are full with a waitlist, some have room.
    assert len({e["capacity"] for e in events}) >= 8
    assert any(e["confirmed"] == e["capacity"] and e["waiting"] > 0 for e in events)
    assert any(e["confirmed"] < e["capacity"] for e in events)
    # Same rules as live traffic.
    for e in events:
        assert e["confirmed"] <= e["capacity"]
        assert e["waiting"] == 0 or e["confirmed"] == e["capacity"], "seat free while people wait"


def test_seed_is_deterministic(db):
    seed(db, now=db_now(db))
    first = snapshot(db)
    seed(db, now=db_now(db) + timedelta(days=3), reset=True)
    assert snapshot(db) == first


def test_seed_refuses_to_overwrite_existing_data(db):
    make_club(db)
    with pytest.raises(DatabaseNotEmpty):
        seed(db, now=db_now(db))
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0


@pytest.mark.parametrize("hour", [1, 8, 13, 20, 23])
def test_seed_at_any_hour_has_no_past_or_middle_of_the_night_events(db, hour):
    # Seeding tomorrow at various local hours (tomorrow, so every event is in
    # the future for the RSVP code, which checks against the real clock).
    at = (datetime.now(LA) + timedelta(days=1)).replace(hour=hour, minute=30, second=0, microsecond=0)
    seed(db, now=at, reset=True)
    times = [r["starts_at"] for r in db.execute("SELECT starts_at FROM events")]
    assert min(times) > at
    assert all(9 <= t.astimezone(LA).hour < 22 for t in times)


# ------------------------------------------------------------ reports


@pytest.fixture
def history(db):
    chess = make_club(db, name="Chess Club")
    make_club(db, name="Archery Club")  # no events
    last_week = make_event(db, chess["id"], capacity=2, title="Last Week", starts_in=timedelta(days=-7))
    yesterday = make_event(db, chess["id"], capacity=3, title="Yesterday", starts_in=timedelta(days=-1))
    upcoming = make_event(db, chess["id"], capacity=5, title="Next Week", starts_in=timedelta(days=7))
    # Build RSVPs while the events were still upcoming, then move them into the past.
    db.execute("UPDATE events SET starts_at = now() + interval '1 day' WHERE id IN (%s, %s)",
               (last_week["id"], yesterday["id"]))
    ann, bo, cy, di = make_users(db, "Ann", "Bo", "Cy", "Di")
    for u in (ann, bo, cy, di):
        rsvps.rsvp(db, last_week["id"], u["id"])  # 2 confirmed, 2 waitlisted
    rsvps.cancel(db, last_week["id"], di["id"])   # -> 1 waitlisted, 1 cancelled
    rsvps.rsvp(db, yesterday["id"], ann["id"])
    rsvps.rsvp(db, upcoming["id"], ann["id"])
    db.execute("UPDATE events SET starts_at = now() - interval '7 days' WHERE id = %s", (last_week["id"],))
    db.execute("UPDATE events SET starts_at = now() - interval '1 day' WHERE id = %s", (yesterday["id"],))
    return chess


def test_attendance_counts_every_event_so_far(db, history):
    rows = reports.attendance(db)
    got = [(r["club"], r["event"], r["capacity"], r["confirmed"], r["waitlisted"], r["cancelled"]) for r in rows]
    assert got == [
        ("Archery Club", None, None, 0, 0, 0),
        ("Chess Club", "Last Week", 2, 2, 1, 1),
        ("Chess Club", "Yesterday", 3, 1, 0, 0),
    ]
    with_upcoming = [r["event"] for r in reports.attendance(db, include_upcoming=True)]
    assert with_upcoming == [None, "Last Week", "Yesterday", "Next Week"]


def test_text_report(db, history):
    out = reports.format_text(reports.attendance(db), LA, datetime.now(timezone.utc), False)
    lines = out.splitlines()
    assert lines[0].startswith("Headcount attendance report")
    assert "Archery Club" in lines and "  (no events yet)" in lines
    last_week = next(line for line in lines if "Last Week" in line)
    assert last_week.split()[-4:] == ["2", "2", "1", "1"]
    total = next(line for line in lines if line.strip().startswith("Total"))
    assert total.split()[-4:] == ["5", "3", "1", "1"]


def test_csv_report(db, history):
    rows = list(csv.DictReader(io.StringIO(reports.format_csv(reports.attendance(db), LA))))
    assert [(r["club"], r["event"], r["confirmed"], r["waitlisted"], r["cancelled"]) for r in rows] == [
        ("Chess Club", "Last Week", "2", "1", "1"),
        ("Chess Club", "Yesterday", "1", "0", "0"),
    ]


def test_report_cli_runs(db, history, monkeypatch, capsys, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert reports.main(["--format", "csv"]) == 0
    assert capsys.readouterr().out.startswith(",".join(reports.COLUMNS))
