"""Read-only queries for clubs, events and notifications (shared by API and pages)."""

import psycopg

# Event columns plus live seat counts. Expects the events table aliased as `e`.
EVENT_SELECT = """
SELECT e.id, e.club_id, e.title, e.description, e.location, e.starts_at, e.capacity,
       e.starts_at <= now() AS is_past,
       (SELECT count(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'confirmed')  AS confirmed,
       (SELECT count(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'waitlisted') AS waitlisted
  FROM events e
"""


def _with_seats(event: dict | None) -> dict | None:
    if event is not None:
        event["seats_left"] = max(event["capacity"] - event["confirmed"], 0)
    return event


def list_clubs(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        SELECT c.id, c.name, c.description,
               count(e.id) FILTER (WHERE e.starts_at > now()) AS upcoming_events
          FROM clubs c LEFT JOIN events e ON e.club_id = c.id
         GROUP BY c.id
         ORDER BY c.name
        """
    ).fetchall()


def get_club(conn: psycopg.Connection, club_id: int) -> dict | None:
    return conn.execute(
        "SELECT id, name, description FROM clubs WHERE id = %s", (club_id,)
    ).fetchone()


def club_events(conn: psycopg.Connection, club_id: int) -> list[dict]:
    """All of a club's events: upcoming soonest first, then past most recent first."""
    rows = conn.execute(
        EVENT_SELECT
        + " WHERE e.club_id = %s ORDER BY e.starts_at <= now(), "
        "CASE WHEN e.starts_at > now() THEN e.starts_at END ASC, e.starts_at DESC, e.id",
        (club_id,),
    ).fetchall()
    return [_with_seats(r) for r in rows]


def get_event(conn: psycopg.Connection, event_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT ev.*, c.name AS club_name
          FROM (""" + EVENT_SELECT + """ WHERE e.id = %s) ev
          JOIN clubs c ON c.id = ev.club_id
        """,
        (event_id,),
    ).fetchone()
    return _with_seats(row)


def notifications(conn: psycopg.Connection, user_id: int, limit: int = 100) -> list[dict]:
    return conn.execute(
        """
        SELECT n.id, n.event_id, e.title AS event_title, n.message, n.created_at, n.read_at
          FROM notifications n JOIN events e ON e.id = n.event_id
         WHERE n.user_id = %s
         ORDER BY n.created_at DESC, n.id DESC
         LIMIT %s
        """,
        (user_id, limit),
    ).fetchall()


def unread_count(conn: psycopg.Connection, user_id: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM notifications WHERE user_id = %s AND read_at IS NULL",
        (user_id,),
    ).fetchone()["n"]


def mark_notifications_read(conn: psycopg.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE notifications SET read_at = now() WHERE user_id = %s AND read_at IS NULL",
        (user_id,),
    )
