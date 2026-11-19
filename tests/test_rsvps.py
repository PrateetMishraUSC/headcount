import threading
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from headcount import rsvps
from tests.factories import make_club, make_event, make_user, make_users


@pytest.fixture
def event(db):
    club = make_club(db)
    return make_event(db, club["id"], capacity=2)


def statuses(db, event_id):
    rows = db.execute(
        """SELECT u.display_name, r.status FROM rsvps r JOIN users u ON u.id = r.user_id
            WHERE r.event_id = %s ORDER BY r.id""",
        (event_id,),
    ).fetchall()
    return [(r["display_name"], r["status"]) for r in rows]


def waitlist_names(db, event_id):
    return [(w["position"], w["display_name"]) for w in rsvps.waitlist(db, event_id)]


# ------------------------------------------------------------ confirm vs waitlist


def test_rsvp_confirms_while_seats_are_free_then_waitlists(db, event):
    ann, bo, cy, di = make_users(db, "Ann", "Bo", "Cy", "Di")

    a = rsvps.rsvp(db, event["id"], ann["id"])
    b = rsvps.rsvp(db, event["id"], bo["id"])
    c = rsvps.rsvp(db, event["id"], cy["id"])
    d = rsvps.rsvp(db, event["id"], di["id"])

    assert (a.status, a.position, a.created) == ("confirmed", None, True)
    assert (b.status, b.position) == ("confirmed", None)
    assert (c.status, c.position) == ("waitlisted", 1)
    assert (d.status, d.position) == ("waitlisted", 2)
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 2, "waitlisted": 2}
    assert [p["display_name"] for p in rsvps.attendees(db, event["id"])] == ["Ann", "Bo"]
    assert waitlist_names(db, event["id"]) == [(1, "Cy"), (2, "Di")]


def test_rsvp_twice_returns_the_existing_rsvp(db, event):
    ann = make_user(db, "Ann")
    first = rsvps.rsvp(db, event["id"], ann["id"])
    again = rsvps.rsvp(db, event["id"], ann["id"])

    assert again.created is False
    assert again.rsvp["id"] == first.rsvp["id"]
    assert db.execute("SELECT count(*) AS n FROM rsvps").fetchone()["n"] == 1


def test_database_rejects_two_live_rsvps_for_one_person(db, event):
    ann = make_user(db, "Ann")
    rsvps.rsvp(db, event["id"], ann["id"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO rsvps (event_id, user_id, status) VALUES (%s, %s, 'waitlisted')",
            (event["id"], ann["id"]),
        )


# ------------------------------------------------------------ cancel


def test_cancel_frees_the_seat_but_does_not_promote(db, event):
    ann, bo, cy = make_users(db, "Ann", "Bo", "Cy")
    for u in (ann, bo, cy):
        rsvps.rsvp(db, event["id"], u["id"])

    cancelled = rsvps.cancel(db, event["id"], ann["id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] is not None
    # Promotion is the worker's job: Cy is still waiting.
    assert statuses(db, event["id"]) == [("Ann", "cancelled"), ("Bo", "confirmed"), ("Cy", "waitlisted")]
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 1, "waitlisted": 1}


def test_leaving_the_waitlist_moves_everyone_behind_up(db, event):
    users = make_users(db, "Ann", "Bo", "Cy", "Di", "Ed")
    for u in users:
        rsvps.rsvp(db, event["id"], u["id"])
    assert waitlist_names(db, event["id"]) == [(1, "Cy"), (2, "Di"), (3, "Ed")]

    rsvps.cancel(db, event["id"], users[3]["id"])  # Di leaves

    assert waitlist_names(db, event["id"]) == [(1, "Cy"), (2, "Ed")]
    assert rsvps.current_rsvp(db, event["id"], users[4]["id"])["position"] == 2


def test_new_rsvp_cannot_take_a_freed_seat_ahead_of_the_waitlist(db, event):
    ann, bo, cy, di = make_users(db, "Ann", "Bo", "Cy", "Di")
    for u in (ann, bo, cy):
        rsvps.rsvp(db, event["id"], u["id"])
    rsvps.cancel(db, event["id"], ann["id"])  # one seat free, Cy waiting

    late = rsvps.rsvp(db, event["id"], di["id"])

    assert (late.status, late.position) == ("waitlisted", 2)
    assert waitlist_names(db, event["id"]) == [(1, "Cy"), (2, "Di")]


def test_rsvp_again_after_cancelling_goes_to_the_back(db, event):
    ann, bo, cy, di = make_users(db, "Ann", "Bo", "Cy", "Di")
    for u in (ann, bo, cy, di):
        rsvps.rsvp(db, event["id"], u["id"])
    rsvps.cancel(db, event["id"], cy["id"])

    back = rsvps.rsvp(db, event["id"], cy["id"])

    assert back.created is True
    assert waitlist_names(db, event["id"]) == [(1, "Di"), (2, "Cy")]


def test_cancel_without_a_live_rsvp(db, event):
    ann = make_user(db, "Ann")
    with pytest.raises(rsvps.NoActiveRsvp):
        rsvps.cancel(db, event["id"], ann["id"])
    rsvps.rsvp(db, event["id"], ann["id"])
    rsvps.cancel(db, event["id"], ann["id"])
    with pytest.raises(rsvps.NoActiveRsvp):
        rsvps.cancel(db, event["id"], ann["id"])


# ------------------------------------------------------------ errors


def test_unknown_event_or_user(db, event):
    ann = make_user(db, "Ann")
    with pytest.raises(rsvps.EventNotFound):
        rsvps.rsvp(db, 999, ann["id"])
    with pytest.raises(rsvps.UserNotFound):
        rsvps.rsvp(db, event["id"], 999)
    with pytest.raises(rsvps.EventNotFound):
        rsvps.cancel(db, 999, ann["id"])


def test_rsvps_close_when_the_event_starts(db):
    club = make_club(db)
    past = make_event(db, club["id"], starts_in=timedelta(hours=-1))
    ann = make_user(db, "Ann")
    with pytest.raises(rsvps.EventStarted):
        rsvps.rsvp(db, past["id"], ann["id"])
    with pytest.raises(rsvps.EventStarted):
        rsvps.cancel(db, past["id"], ann["id"])


# ------------------------------------------------------------ concurrency


def test_concurrent_rsvps_never_overbook(database_url, db):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=5)
    users = make_users(db, *[f"Racer {i}" for i in range(20)])
    start = threading.Barrier(len(users))
    errors = []

    def go(user_id):
        try:
            with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
                start.wait()
                rsvps.rsvp(conn, event["id"], user_id)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(u["id"],)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 5, "waitlisted": 15}
    assert [w["position"] for w in rsvps.waitlist(db, event["id"])] == list(range(1, 16))


# ------------------------------------------------------------ users and "your RSVPs"


def test_sign_in_by_name_is_case_insensitive(db):
    first = rsvps.find_or_create_user(db, "  Ada   Lovelace ")
    again = rsvps.find_or_create_user(db, "ada lovelace")
    assert first["display_name"] == "Ada Lovelace"
    assert again["id"] == first["id"]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 41])
def test_display_name_validation(db, bad):
    with pytest.raises(ValueError):
        rsvps.find_or_create_user(db, bad)


def test_user_rsvps_lists_status_and_position(db):
    club = make_club(db)
    soon = make_event(db, club["id"], capacity=1, title="Soon", starts_in=timedelta(days=1))
    later = make_event(db, club["id"], capacity=1, title="Later", starts_in=timedelta(days=5))
    ann, bo = make_users(db, "Ann", "Bo")
    rsvps.rsvp(db, soon["id"], ann["id"])
    rsvps.rsvp(db, soon["id"], bo["id"])  # Bo waitlisted #1 for Soon
    rsvps.rsvp(db, later["id"], bo["id"])
    rsvps.cancel(db, later["id"], bo["id"])

    mine = rsvps.user_rsvps(db, bo["id"])

    assert [(r["event_title"], r["status"], r["position"]) for r in mine] == [
        ("Soon", "waitlisted", 1),
        ("Later", "cancelled", None),
    ]
    assert mine[0]["club_name"] == "Chess Club"
