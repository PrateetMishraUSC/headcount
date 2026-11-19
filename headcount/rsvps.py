"""Users, RSVPs and the waitlist.

Every function takes an open psycopg connection (rows as dicts) and runs its
writes inside `conn.transaction()`. Use autocommit connections so that block
is a real transaction rather than a savepoint inside one.

How seats stay correct
----------------------
Anything that can *confirm* someone (an RSVP here, a promotion in the worker)
first locks the event row, then counts seats in a separate statement. Under
READ COMMITTED each statement gets a fresh snapshot, so a count taken after
the lock sees everything committed by whoever held the lock before us. Two
people racing for the last seat are therefore served one at a time and the
event can never be overbooked.

Cancelling only lowers the head count, so it doesn't need the event lock. It
never promotes anyone: filling the freed seat is the worker's job.
"""

from dataclasses import dataclass

import psycopg

MAX_DISPLAY_NAME = 40


class NotFound(Exception):
    pass


class EventNotFound(NotFound):
    pass


class UserNotFound(NotFound):
    pass


class NoActiveRsvp(NotFound):
    pass


class EventStarted(Exception):
    """RSVPs and cancellations close once the event has started."""


@dataclass(frozen=True)
class RsvpResult:
    rsvp: dict
    created: bool  # False if the user already had a live RSVP

    @property
    def status(self) -> str:
        return self.rsvp["status"]

    @property
    def position(self) -> int | None:
        return self.rsvp.get("position")


# ---------------------------------------------------------------- users


def clean_display_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise ValueError("Pick a display name.")
    if len(cleaned) > MAX_DISPLAY_NAME:
        raise ValueError(f"Display names can be at most {MAX_DISPLAY_NAME} characters.")
    return cleaned


def find_or_create_user(conn: psycopg.Connection, display_name: str) -> dict:
    """Sign-in by name: return the user with this name (any case), creating it if new."""
    name = clean_display_name(display_name)
    with conn.transaction():
        user = conn.execute(
            """
            INSERT INTO users (display_name) VALUES (%s)
            ON CONFLICT ((lower(display_name))) DO NOTHING
            RETURNING id, display_name, created_at
            """,
            (name,),
        ).fetchone()
        if user is None:
            user = conn.execute(
                "SELECT id, display_name, created_at FROM users WHERE lower(display_name) = lower(%s)",
                (name,),
            ).fetchone()
    return user


def get_user(conn: psycopg.Connection, user_id: int) -> dict | None:
    return conn.execute(
        "SELECT id, display_name, created_at FROM users WHERE id = %s", (user_id,)
    ).fetchone()


# ---------------------------------------------------------------- RSVP / cancel

_RSVP_COLUMNS = "id, event_id, user_id, status, created_at, confirmed_at, cancelled_at"


def _with_position(conn: psycopg.Connection, rsvp: dict) -> dict:
    rsvp = dict(rsvp)
    rsvp["position"] = None
    if rsvp["status"] == "waitlisted":
        rsvp["position"] = conn.execute(
            """
            SELECT count(*) + 1 AS position
              FROM rsvps w
             WHERE w.event_id = %(event_id)s
               AND w.status = 'waitlisted'
               AND (w.created_at, w.id) < (%(created_at)s, %(id)s)
            """,
            rsvp,
        ).fetchone()["position"]
    return rsvp


def rsvp(conn: psycopg.Connection, event_id: int, user_id: int) -> RsvpResult:
    """Confirm the user if a seat is free and nobody is waiting; else waitlist them.

    Idempotent: if the user already has a live RSVP it is returned unchanged.
    """
    with conn.transaction():
        # 1. Lock the event: serializes seat allocation for this event.
        event = conn.execute(
            "SELECT id, capacity, starts_at <= now() AS started FROM events WHERE id = %s FOR UPDATE",
            (event_id,),
        ).fetchone()
        if event is None:
            raise EventNotFound(event_id)
        if event["started"]:
            raise EventStarted(event_id)
        if get_user(conn, user_id) is None:
            raise UserNotFound(user_id)

        existing = conn.execute(
            f"SELECT {_RSVP_COLUMNS} FROM rsvps "
            "WHERE event_id = %s AND user_id = %s AND status <> 'cancelled'",
            (event_id, user_id),
        ).fetchone()
        if existing is not None:
            return RsvpResult(_with_position(conn, existing), created=False)

        # 2. Count in a new statement, i.e. a snapshot taken after the lock.
        counts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'confirmed')  AS confirmed,
                   count(*) FILTER (WHERE status = 'waitlisted') AS waitlisted
              FROM rsvps
             WHERE event_id = %s AND status <> 'cancelled'
            """,
            (event_id,),
        ).fetchone()
        # A seat freed by a cancellation belongs to the head of the waitlist,
        # even in the moment before the worker gets to it.
        seat_free = counts["confirmed"] < event["capacity"] and counts["waitlisted"] == 0
        status = "confirmed" if seat_free else "waitlisted"

        # clock_timestamp(), not now(): queue order is the order we got the
        # lock, not the order transactions happened to start.
        row = conn.execute(
            f"""
            INSERT INTO rsvps (event_id, user_id, status, created_at, confirmed_at)
            VALUES (%(event_id)s, %(user_id)s, %(status)s, clock_timestamp(),
                    CASE WHEN %(status)s = 'confirmed' THEN clock_timestamp() END)
            RETURNING {_RSVP_COLUMNS}
            """,
            {"event_id": event_id, "user_id": user_id, "status": status},
        ).fetchone()
        return RsvpResult(_with_position(conn, row), created=True)


def cancel(conn: psycopg.Connection, event_id: int, user_id: int) -> dict:
    """Cancel a confirmed RSVP or leave the waitlist. Never promotes anyone."""
    with conn.transaction():
        event = conn.execute(
            "SELECT id, starts_at <= now() AS started FROM events WHERE id = %s", (event_id,)
        ).fetchone()
        if event is None:
            raise EventNotFound(event_id)
        if event["started"]:
            raise EventStarted(event_id)
        row = conn.execute(
            f"""
            UPDATE rsvps SET status = 'cancelled', cancelled_at = now()
             WHERE event_id = %s AND user_id = %s AND status <> 'cancelled'
            RETURNING {_RSVP_COLUMNS}
            """,
            (event_id, user_id),
        ).fetchone()
        if row is None:
            raise NoActiveRsvp(event_id, user_id)
    row = dict(row)
    row["position"] = None
    return row


# ---------------------------------------------------------------- reads


def current_rsvp(conn: psycopg.Connection, event_id: int, user_id: int) -> dict | None:
    """The user's live RSVP for the event (with waitlist position), if any."""
    row = conn.execute(
        f"SELECT {_RSVP_COLUMNS} FROM rsvps "
        "WHERE event_id = %s AND user_id = %s AND status <> 'cancelled'",
        (event_id, user_id),
    ).fetchone()
    return _with_position(conn, row) if row else None


def seat_counts(conn: psycopg.Connection, event_id: int) -> dict:
    return conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'confirmed')  AS confirmed,
               count(*) FILTER (WHERE status = 'waitlisted') AS waitlisted
          FROM rsvps
         WHERE event_id = %s AND status <> 'cancelled'
        """,
        (event_id,),
    ).fetchone()


def attendees(conn: psycopg.Connection, event_id: int) -> list[dict]:
    """Confirmed people, in the order they got their seat."""
    return conn.execute(
        """
        SELECT r.id AS rsvp_id, u.id AS user_id, u.display_name, r.confirmed_at
          FROM rsvps r JOIN users u ON u.id = r.user_id
         WHERE r.event_id = %s AND r.status = 'confirmed'
         ORDER BY r.confirmed_at, r.id
        """,
        (event_id,),
    ).fetchall()


def waitlist(conn: psycopg.Connection, event_id: int) -> list[dict]:
    """Waiting people in queue order, with 1-based positions."""
    return conn.execute(
        """
        SELECT r.id AS rsvp_id, u.id AS user_id, u.display_name, r.created_at,
               row_number() OVER (ORDER BY r.created_at, r.id) AS position
          FROM rsvps r JOIN users u ON u.id = r.user_id
         WHERE r.event_id = %s AND r.status = 'waitlisted'
         ORDER BY r.created_at, r.id
        """,
        (event_id,),
    ).fetchall()


def user_rsvps(conn: psycopg.Connection, user_id: int) -> list[dict]:
    """All of a user's RSVPs (including cancelled ones), soonest event first."""
    return conn.execute(
        """
        WITH queue AS (
            SELECT id, row_number() OVER (PARTITION BY event_id ORDER BY created_at, id) AS position
              FROM rsvps
             WHERE status = 'waitlisted'
               AND event_id IN (SELECT event_id FROM rsvps
                                 WHERE user_id = %(user_id)s AND status = 'waitlisted')
        )
        SELECT r.id, r.event_id, r.user_id, r.status, r.created_at, r.confirmed_at,
               r.cancelled_at, q.position,
               e.title AS event_title, e.starts_at, e.location, e.starts_at <= now() AS is_past,
               c.id AS club_id, c.name AS club_name
          FROM rsvps r
          JOIN events e ON e.id = r.event_id
          JOIN clubs  c ON c.id = e.club_id
          LEFT JOIN queue q ON q.id = r.id
         WHERE r.user_id = %(user_id)s
         ORDER BY e.starts_at, r.created_at, r.id
        """,
        {"user_id": user_id},
    ).fetchall()
