"""Creating and editing clubs and events.

Validation lives here so every caller shares it. Problems are collected per
field in `Invalid.errors`, so a form can show each one next to its input.

Changing an event takes the same event-row lock as an RSVP and counts seats in
a second statement (see rsvps.py), so a capacity change can't race an RSVP
into overbooking. Capacity can never go below the number of people already
confirmed. Raising it doesn't promote anyone here: the worker sees the free
seats and fills them from the waitlist within a second, as it does after a
cancellation.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg import errors as pg_errors

from headcount.rsvps import EventNotFound, EventStarted, NotFound

MAX_CLUB_NAME = 80
MAX_CLUB_DESCRIPTION = 1000
MAX_TITLE = 120
MAX_LOCATION = 120
MAX_EVENT_DESCRIPTION = 4000
MAX_CAPACITY = 10_000

_CLUB_COLUMNS = "id, name, description"
_EVENT_COLUMNS = "id, club_id, title, description, location, starts_at, capacity"


class ClubNotFound(NotFound):
    pass


class Invalid(Exception):
    """The input can't be saved. `errors` maps field name -> message."""

    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{field}: {msg}" for field, msg in errors.items()))
        self.errors = errors


# ---------------------------------------------------------------- cleaning input


def _line(value, field: str, label: str, max_len: int, errors: dict) -> str:
    """A required single-line field: whitespace collapsed."""
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        errors[field] = f"{label} is required."
    elif len(cleaned) > max_len:
        errors[field] = f"{label} can be at most {max_len} characters."
    return cleaned


def _text(value, field: str, label: str, max_len: int, errors: dict) -> str:
    """An optional multi-line field: line breaks kept, ends trimmed."""
    cleaned = str(value or "").replace("\r\n", "\n").strip()
    if len(cleaned) > max_len:
        errors[field] = f"{label} can be at most {max_len} characters."
    return cleaned


def parse_local_time(value, tz: ZoneInfo) -> datetime | None:
    """'2026-12-01T18:30' (what <input type=datetime-local> sends), read in `tz`."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def clean_club(*, name, description="") -> dict:
    errors: dict[str, str] = {}
    club = {
        "name": _line(name, "name", "Name", MAX_CLUB_NAME, errors),
        "description": _text(description, "description", "Description", MAX_CLUB_DESCRIPTION, errors),
    }
    if errors:
        raise Invalid(errors)
    return club


def clean_event(*, title, location, starts_at, capacity, description="", tz: ZoneInfo,
                now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    errors: dict[str, str] = {}
    event = {
        "title": _line(title, "title", "Title", MAX_TITLE, errors),
        "description": _text(description, "description", "Description", MAX_EVENT_DESCRIPTION, errors),
        "location": _line(location, "location", "Location", MAX_LOCATION, errors),
    }

    when = None
    if not str(starts_at or "").strip():
        errors["starts_at"] = "Start time is required."
    else:
        when = parse_local_time(starts_at, tz)
        if when is None:
            errors["starts_at"] = "Use a date and time like 2026-12-01 18:30."
        elif when <= now:
            errors["starts_at"] = "The start time has to be in the future."
    event["starts_at"] = when

    seats = None
    try:
        seats = int(str(capacity).strip())
    except ValueError:
        errors["capacity"] = "Capacity has to be a whole number."
    else:
        if not 1 <= seats <= MAX_CAPACITY:
            errors["capacity"] = f"Capacity has to be between 1 and {MAX_CAPACITY:,}."
    event["capacity"] = seats

    if errors:
        raise Invalid(errors)
    return event


# ---------------------------------------------------------------- clubs


def _check_club_name_free(conn: psycopg.Connection, name: str, except_id: int | None = None) -> None:
    taken = conn.execute(
        "SELECT 1 FROM clubs WHERE lower(name) = lower(%s) AND id IS DISTINCT FROM %s",
        (name, except_id),
    ).fetchone()
    if taken:
        raise Invalid({"name": "There's already a club with this name."})


def create_club(conn: psycopg.Connection, *, name, description="", organiser_id: int | None = None) -> dict:
    """Create a club; `organiser_id` (the person creating it) becomes its first organiser."""
    club = clean_club(name=name, description=description)
    try:
        with conn.transaction():
            _check_club_name_free(conn, club["name"])
            row = conn.execute(
                f"INSERT INTO clubs (name, description) VALUES (%(name)s, %(description)s) "
                f"RETURNING {_CLUB_COLUMNS}",
                club,
            ).fetchone()
            if organiser_id is not None:
                conn.execute(
                    "INSERT INTO club_organisers (club_id, user_id, added_by) VALUES (%s, %s, %s)",
                    (row["id"], organiser_id, organiser_id),
                )
            return row
    except pg_errors.UniqueViolation:  # created by someone else at the same moment
        raise Invalid({"name": "There's already a club with this name."}) from None


def update_club(conn: psycopg.Connection, club_id: int, *, name, description="") -> dict:
    club = clean_club(name=name, description=description)
    try:
        with conn.transaction():
            _check_club_name_free(conn, club["name"], except_id=club_id)
            row = conn.execute(
                f"UPDATE clubs SET name = %(name)s, description = %(description)s "
                f"WHERE id = %(id)s RETURNING {_CLUB_COLUMNS}",
                {**club, "id": club_id},
            ).fetchone()
    except pg_errors.UniqueViolation:
        raise Invalid({"name": "There's already a club with this name."}) from None
    if row is None:
        raise ClubNotFound(club_id)
    return row


# ---------------------------------------------------------------- events


def create_event(conn: psycopg.Connection, club_id: int, *, tz: ZoneInfo, now=None, **fields) -> dict:
    event = clean_event(tz=tz, now=now, **fields)
    try:
        with conn.transaction():
            return conn.execute(
                f"""
                INSERT INTO events (club_id, title, description, location, starts_at, capacity)
                VALUES (%(club_id)s, %(title)s, %(description)s, %(location)s, %(starts_at)s, %(capacity)s)
                RETURNING {_EVENT_COLUMNS}
                """,
                {**event, "club_id": club_id},
            ).fetchone()
    except pg_errors.ForeignKeyViolation:
        raise ClubNotFound(club_id) from None


def update_event(conn: psycopg.Connection, event_id: int, *, tz: ZoneInfo, now=None, **fields) -> dict:
    """Change an upcoming event. Capacity can't drop below the confirmed head count."""
    event = clean_event(tz=tz, now=now, **fields)
    with conn.transaction():
        # Same lock as an RSVP, so nobody is confirmed between our count and our update.
        current = conn.execute(
            "SELECT id, starts_at <= now() AS started FROM events WHERE id = %s FOR UPDATE",
            (event_id,),
        ).fetchone()
        if current is None:
            raise EventNotFound(event_id)
        if current["started"]:
            raise EventStarted(event_id)
        confirmed = conn.execute(
            "SELECT count(*) AS n FROM rsvps WHERE event_id = %s AND status = 'confirmed'",
            (event_id,),
        ).fetchone()["n"]
        if event["capacity"] < confirmed:
            raise Invalid({"capacity": (
                f"{confirmed} people are already confirmed, so capacity can't be lower than "
                f"{confirmed}. They'd have to cancel first."
            )})
        return conn.execute(
            f"""
            UPDATE events
               SET title = %(title)s, description = %(description)s, location = %(location)s,
                   starts_at = %(starts_at)s, capacity = %(capacity)s
             WHERE id = %(id)s
            RETURNING {_EVENT_COLUMNS}
            """,
            {**event, "id": event_id},
        ).fetchone()
