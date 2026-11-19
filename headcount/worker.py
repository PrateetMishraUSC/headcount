"""Background worker: fills freed seats from the waitlist.

    python -m headcount.worker            # run until SIGTERM / Ctrl-C
    python -m headcount.worker --once     # one poll, then exit

Every poll it repeatedly claims one upcoming event that has a free seat and
people waiting, promotes the earliest waiters into the free seats and creates
a "You got a spot" notification for each, until no such event is left.

Safe to run several at once: an event is claimed with
`SELECT ... FOR UPDATE SKIP LOCKED`, so two workers never work on the same
event at the same time; the second simply moves on to another event. The web
request that confirms RSVPs takes the same event lock (see rsvps.py), so a
promotion and a new RSVP can't both take the last seat.

The worker keeps one connection open and replaces it every N polls
(WORKER_RECONNECT_EVERY, default 360), so after a database failover it ends up
on the new primary without a restart. A connection that breaks is replaced on
the next poll.
"""

import argparse
import os
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timezone

import psycopg

from headcount.config import get_settings
from headcount.db import connect

# Upper bound on events handled in one poll, so a poll always finishes.
MAX_EVENTS_PER_POLL = 100

CLAIM_EVENT_SQL = """
SELECT e.id, e.title, e.capacity
  FROM events e
 WHERE e.starts_at > now()
   AND e.id <> ALL(%(skip)s)
   AND EXISTS (SELECT 1 FROM rsvps w
                WHERE w.event_id = e.id AND w.status = 'waitlisted')
   AND (SELECT count(*) FROM rsvps c
         WHERE c.event_id = e.id AND c.status = 'confirmed') < e.capacity
 ORDER BY e.starts_at, e.id
 LIMIT 1
   FOR UPDATE OF e SKIP LOCKED
"""

# Run after the claim, as its own statement: its snapshot is taken after we
# hold the event lock, so the count includes every committed confirmation.
CONFIRMED_COUNT_SQL = """
SELECT count(*) AS confirmed FROM rsvps WHERE event_id = %s AND status = 'confirmed'
"""

PROMOTE_SQL = """
WITH next AS (
    SELECT id FROM rsvps
     WHERE event_id = %(event_id)s AND status = 'waitlisted'
     ORDER BY created_at, id
     LIMIT %(seats)s
       FOR UPDATE
)
UPDATE rsvps r
   SET status = 'confirmed', confirmed_at = clock_timestamp()
  FROM next, users u
 WHERE r.id = next.id AND u.id = r.user_id
RETURNING r.id AS rsvp_id, r.user_id, u.display_name, r.event_id, r.created_at
"""

NOTIFY_SQL = """
INSERT INTO notifications (user_id, event_id, rsvp_id, message)
VALUES (%(user_id)s, %(event_id)s, %(rsvp_id)s, %(message)s)
ON CONFLICT (rsvp_id) DO NOTHING
"""


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    print(f"{stamp} worker[{os.getpid()}] {message}", flush=True)


def promote_one_event(conn: psycopg.Connection, skip: list[int]) -> tuple[dict, list[dict]] | None:
    """Claim one event needing promotions and fill its free seats.

    Returns (event, promotions), or None when nothing is claimable. Events in
    `skip` are ignored (already handled this poll).
    """
    with conn.transaction():
        event = conn.execute(CLAIM_EVENT_SQL, {"skip": skip}).fetchone()
        if event is None:
            return None
        confirmed = conn.execute(CONFIRMED_COUNT_SQL, (event["id"],)).fetchone()["confirmed"]
        seats = event["capacity"] - confirmed
        promoted: list[dict] = []
        if seats > 0:
            promoted = conn.execute(PROMOTE_SQL, {"event_id": event["id"], "seats": seats}).fetchall()
            promoted.sort(key=lambda p: (p["created_at"], p["rsvp_id"]))
            for p in promoted:
                conn.execute(NOTIFY_SQL, {**p, "message": f"You got a spot at {event['title']}!"})
        return event, promoted


class Worker:
    def __init__(
        self,
        database_url: str,
        *,
        poll_interval_s: float = 1.0,
        reconnect_every: int = 360,
        statement_timeout_ms: int | None = 5000,
        log: Callable[[str], None] = log,
    ):
        if reconnect_every < 1:
            raise ValueError("reconnect_every must be at least 1")
        self.database_url = database_url
        self.poll_interval_s = poll_interval_s
        self.reconnect_every = reconnect_every
        self.statement_timeout_ms = statement_timeout_ms
        self.log = log
        self.stop_requested = threading.Event()
        self.conn: psycopg.Connection | None = None
        self.polls_on_connection = 0
        self.connections_opened = 0

    # -- connection handling

    def _close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def _connection(self) -> psycopg.Connection:
        if self.conn is not None and self.polls_on_connection >= self.reconnect_every:
            self.log(f"reconnecting to the database (every {self.reconnect_every} polls)")
            self._close()
        if self.conn is None or self.conn.closed:
            self.conn = connect(
                self.database_url,
                autocommit=True,
                statement_timeout_ms=self.statement_timeout_ms,
                application_name="headcount-worker",
            )
            self.polls_on_connection = 0
            self.connections_opened += 1
        return self.conn

    # -- work

    def poll_once(self) -> list[dict]:
        """One poll: handle every claimable event. Returns the promotions made."""
        promotions: list[dict] = []
        try:
            conn = self._connection()
            self.polls_on_connection += 1
            handled: list[int] = []
            while len(handled) < MAX_EVENTS_PER_POLL:
                result = promote_one_event(conn, handled)
                if result is None:
                    break
                event, promoted = result
                handled.append(event["id"])
                for p in promoted:
                    self.log(
                        f"promoted user {p['user_id']} ({p['display_name']}) to confirmed "
                        f"for event {event['id']} ({event['title']})"
                    )
                promotions.extend(promoted)
        except psycopg.Error as exc:
            # Includes a dropped connection during failover. Work done so far
            # is committed; anything in flight was rolled back and will be
            # retried on a fresh connection next poll.
            self.log(f"database error, will reconnect: {type(exc).__name__}: {exc}".strip())
            self._close()
        return promotions

    def run(self, max_polls: int | None = None) -> None:
        self.log(
            f"started (poll every {self.poll_interval_s}s, "
            f"reconnect every {self.reconnect_every} polls)"
        )
        polls = 0
        try:
            while not self.stop_requested.is_set():
                self.poll_once()
                polls += 1
                if max_polls is not None and polls >= max_polls:
                    break
                self.stop_requested.wait(self.poll_interval_s)
        finally:
            self._close()
            self.log("stopped")

    def stop(self, *_args) -> None:
        self.stop_requested.set()


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single poll and exit")
    parser.add_argument("--poll-interval", type=float, default=settings.worker_poll_interval_s)
    parser.add_argument("--reconnect-every", type=int, default=settings.worker_reconnect_every)
    args = parser.parse_args(argv)

    worker = Worker(
        settings.database_url,
        poll_interval_s=args.poll_interval,
        reconnect_every=args.reconnect_every,
        statement_timeout_ms=settings.statement_timeout_ms,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run(max_polls=1 if args.once else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
