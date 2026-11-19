import threading
from datetime import timedelta

import psycopg
import pytest
from psycopg.rows import dict_row

from headcount import rsvps
from headcount.worker import Worker
from tests.factories import make_club, make_event, make_user, make_users


@pytest.fixture
def make_worker(database_url):
    workers = []

    def factory(**kwargs):
        lines = []
        kwargs.setdefault("poll_interval_s", 0)
        w = Worker(database_url, log=lines.append, **kwargs)
        w.lines = lines
        workers.append(w)
        return w

    yield factory
    for w in workers:
        w._close()


def fill(db, event, names):
    users = make_users(db, *names)
    for u in users:
        rsvps.rsvp(db, event["id"], u["id"])
    return users


def names(rows):
    return [r["display_name"] for r in rows]


def notifications(db):
    return db.execute(
        """SELECT u.display_name, n.message, n.event_id FROM notifications n
             JOIN users u ON u.id = n.user_id ORDER BY n.id"""
    ).fetchall()


# ------------------------------------------------------------ promotion


def test_promotes_the_earliest_waiter_and_notifies_them(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=2, title="Blitz Night")
    ann, bo, cy, di = fill(db, event, ["Ann", "Bo", "Cy", "Di"])
    rsvps.cancel(db, event["id"], ann["id"])

    promoted = make_worker().poll_once()

    assert names(promoted) == ["Cy"]
    assert names(rsvps.attendees(db, event["id"])) == ["Bo", "Cy"]
    assert [(w["position"], w["display_name"]) for w in rsvps.waitlist(db, event["id"])] == [(1, "Di")]
    assert [(n["display_name"], n["message"]) for n in notifications(db)] == [
        ("Cy", "You got a spot at Blitz Night!")
    ]


def test_fills_every_free_seat_in_queue_order(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=3)
    users = fill(db, event, ["Ann", "Bo", "Cy", "Di", "Ed", "Flo", "Gus"])
    for u in users[:3]:
        rsvps.cancel(db, event["id"], u["id"])

    worker = make_worker()
    promoted = worker.poll_once()

    assert names(promoted) == ["Di", "Ed", "Flo"]
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 3, "waitlisted": 1}
    assert [line for line in worker.lines if "promoted" in line] == [
        f"promoted user {u['id']} ({u['display_name']}) to confirmed for event {event['id']} (Blitz Night)"
        for u in users[3:6]
    ]


def test_handles_several_events_in_one_poll(db, make_worker):
    club = make_club(db)
    events = [make_event(db, club["id"], capacity=1, title=f"E{i}") for i in range(3)]
    for i, event in enumerate(events):
        first, _second = fill(db, event, [f"First{i}", f"Second{i}"])
        rsvps.cancel(db, event["id"], first["id"])

    promoted = make_worker().poll_once()

    assert sorted(names(promoted)) == ["Second0", "Second1", "Second2"]


def test_nothing_to_do(db, make_worker):
    club = make_club(db)
    full = make_event(db, club["id"], capacity=1, title="Full")
    fill(db, full, ["Ann", "Bo"])  # full, one waiting, no free seat
    roomy = make_event(db, club["id"], capacity=5, title="Roomy")
    fill(db, roomy, ["Cy"])  # free seats, nobody waiting

    assert make_worker().poll_once() == []
    assert notifications(db) == []


def test_does_not_promote_into_events_that_already_started(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo = fill(db, event, ["Ann", "Bo"])
    rsvps.cancel(db, event["id"], ann["id"])
    db.execute("UPDATE events SET starts_at = now() - interval '1 minute' WHERE id = %s", (event["id"],))

    assert make_worker().poll_once() == []
    assert rsvps.current_rsvp(db, event["id"], bo["id"])["status"] == "waitlisted"


def test_promoted_user_can_cancel_and_the_seat_goes_to_the_next(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo, cy = fill(db, event, ["Ann", "Bo", "Cy"])
    worker = make_worker()

    rsvps.cancel(db, event["id"], ann["id"])
    assert names(worker.poll_once()) == ["Bo"]
    rsvps.cancel(db, event["id"], bo["id"])
    assert names(worker.poll_once()) == ["Cy"]
    assert names(notifications(db)) == ["Bo", "Cy"]


def test_one_promotion_can_only_ever_have_one_notification(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo = fill(db, event, ["Ann", "Bo"])
    rsvps.cancel(db, event["id"], ann["id"])
    make_worker().poll_once()
    note = db.execute("SELECT * FROM notifications").fetchone()

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO notifications (user_id, event_id, rsvp_id, message) VALUES (%s, %s, %s, 'dup')",
            (note["user_id"], note["event_id"], note["rsvp_id"]),
        )


# ------------------------------------------------------------ several workers


def test_a_locked_event_is_skipped_not_waited_for(db, database_url, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo = fill(db, event, ["Ann", "Bo"])
    rsvps.cancel(db, event["id"], ann["id"])

    # Another worker (or an RSVP) is holding this event's lock.
    with psycopg.connect(database_url) as other:
        other.execute("SELECT 1 FROM events WHERE id = %s FOR UPDATE", (event["id"],))
        assert make_worker(statement_timeout_ms=1000).poll_once() == []  # returns at once
        other.rollback()

    assert names(make_worker().poll_once()) == ["Bo"]


def test_two_workers_never_promote_the_same_person_twice(db, database_url, make_worker):
    club = make_club(db)
    events = []
    for i in range(10):
        event = make_event(db, club["id"], capacity=3, title=f"Event {i}")
        users = fill(db, event, [f"P{i}-{j}" for j in range(8)])
        for u in users[:3]:  # free all three seats; five people waiting
            rsvps.cancel(db, event["id"], u["id"])
        events.append(event)

    workers = [make_worker(), make_worker()]
    start = threading.Barrier(len(workers))
    results = {}

    def run(i, w):
        start.wait()
        results[i] = [p for _ in range(3) for p in w.poll_once()]

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    all_promoted = [p["rsvp_id"] for r in results.values() for p in r]
    assert len(all_promoted) == 30
    assert len(set(all_promoted)) == 30  # nobody promoted twice
    assert db.execute("SELECT count(*) AS n FROM notifications").fetchone()["n"] == 30
    for event in events:
        assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 3, "waitlisted": 2}
        # The three promoted were the first three in the queue.
        assert names(rsvps.attendees(db, event["id"])) == [
            f"P{event['title'].split()[1]}-{j}" for j in (3, 4, 5)
        ]


def test_worker_and_concurrent_rsvps_never_overbook(db, database_url, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=5)
    first = fill(db, event, [f"Early {i}" for i in range(8)])  # 5 confirmed, 3 waiting
    for u in first[:4]:
        rsvps.cancel(db, event["id"], u["id"])  # 4 seats free
    late = make_users(db, *[f"Late {i}" for i in range(10)])
    worker = make_worker()
    start = threading.Barrier(len(late) + 1)

    def rsvp(user_id):
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
            start.wait()
            rsvps.rsvp(conn, event["id"], user_id)

    def work():
        start.wait()
        for _ in range(5):
            worker.poll_once()

    threads = [threading.Thread(target=rsvp, args=(u["id"],)) for u in late]
    threads.append(threading.Thread(target=work))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    worker.poll_once()

    # 14 live RSVPs (1 kept seat + 3 early waiters + 10 late) for 5 seats.
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 5, "waitlisted": 9}
    # The early waiters were ahead of every late RSVP.
    assert {"Early 5", "Early 6", "Early 7"} <= set(names(rsvps.attendees(db, event["id"])))


# ------------------------------------------------------------ connection handling


def test_reconnects_every_n_polls(db, make_worker):
    worker = make_worker(reconnect_every=3)
    pids = []
    for _ in range(7):
        worker.poll_once()
        pids.append(worker.conn.info.backend_pid)

    assert worker.connections_opened == 3  # polls 1-3, 4-6, 7
    assert len(set(pids[0:3])) == 1 and len(set(pids[3:6])) == 1
    assert pids[0] != pids[3] != pids[6]
    assert sum("reconnecting" in line for line in worker.lines) == 2


def test_survives_its_connection_being_killed(db, make_worker):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo = fill(db, event, ["Ann", "Bo"])
    worker = make_worker()
    worker.poll_once()
    old_pid = worker.conn.info.backend_pid

    # Simulate a failover: the server drops the worker's connection.
    db.execute("SELECT pg_terminate_backend(%s)", (old_pid,))
    rsvps.cancel(db, event["id"], ann["id"])

    assert worker.poll_once() == []  # this poll hits the dead connection
    assert any("database error, will reconnect" in line for line in worker.lines)
    assert names(worker.poll_once()) == ["Bo"]  # next poll reconnects and works
    assert worker.conn.info.backend_pid != old_pid


def test_run_logs_start_and_stop(db, make_worker):
    worker = make_worker()
    worker.run(max_polls=2)
    assert worker.lines[0].startswith("started (poll every 0s, reconnect every 360 polls)")
    assert worker.lines[-1] == "stopped"
    assert worker.conn is None
