"""JSON API for scripts.

There are no passwords anywhere in Headcount, so the API identifies people by
`user_id` in the request body, just as the pages identify them by a cookie.
"""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from headcount import queries, rsvps
from headcount.deps import get_db

router = APIRouter(prefix="/api", tags=["api"])

Db = Depends(get_db)


class UserRef(BaseModel):
    user_id: int = Field(gt=0)


def event_json(event: dict) -> dict:
    keys = ("id", "club_id", "title", "description", "location", "starts_at", "capacity",
            "confirmed", "waitlisted", "seats_left", "is_past")
    return {k: event[k] for k in keys}


def rsvp_json(rsvp: dict) -> dict:
    keys = ("id", "event_id", "user_id", "status", "position", "created_at", "confirmed_at", "cancelled_at")
    return {k: rsvp.get(k) for k in keys}


def user_json(user: dict) -> dict:
    return {"id": user["id"], "display_name": user["display_name"]}


def require_user(conn: psycopg.Connection, user_id: int) -> dict:
    user = rsvps.get_user(conn, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.get("/clubs/{club_id}")
def get_club(club_id: int, conn: psycopg.Connection = Db):
    club = queries.get_club(conn, club_id)
    if club is None:
        raise HTTPException(404, "Club not found")
    return {**club, "events": [event_json(e) for e in queries.club_events(conn, club_id)]}


@router.get("/events/{event_id}")
def get_event(event_id: int, conn: psycopg.Connection = Db):
    event = queries.get_event(conn, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    return {
        **event_json(event),
        "club": {"id": event["club_id"], "name": event["club_name"]},
        "attendees": [
            {"user_id": a["user_id"], "display_name": a["display_name"]}
            for a in rsvps.attendees(conn, event_id)
        ],
        "waitlist": [
            {"position": w["position"], "user_id": w["user_id"], "display_name": w["display_name"]}
            for w in rsvps.waitlist(conn, event_id)
        ],
    }


@router.post("/events/{event_id}/rsvp")
def rsvp(event_id: int, body: UserRef, response: Response, conn: psycopg.Connection = Db):
    """Confirmed if a seat is free and nobody is waiting, else waitlisted.

    201 for a new RSVP, 200 if the user already had one (returned unchanged).
    """
    try:
        result = rsvps.rsvp(conn, event_id, body.user_id)
    except rsvps.EventNotFound:
        raise HTTPException(404, "Event not found")
    except rsvps.UserNotFound:
        raise HTTPException(404, "User not found")
    except rsvps.EventStarted:
        raise HTTPException(409, "This event has already started")
    response.status_code = 201 if result.created else 200
    return rsvp_json(result.rsvp)


@router.post("/events/{event_id}/cancel")
def cancel(event_id: int, body: UserRef, conn: psycopg.Connection = Db):
    """Cancel a confirmed RSVP or leave the waitlist."""
    try:
        return rsvp_json(rsvps.cancel(conn, event_id, body.user_id))
    except rsvps.EventNotFound:
        raise HTTPException(404, "Event not found")
    except rsvps.NoActiveRsvp:
        raise HTTPException(404, "No active RSVP for this user and event")
    except rsvps.EventStarted:
        raise HTTPException(409, "This event has already started")


@router.get("/users/{user_id}/rsvps")
def user_rsvps(user_id: int, conn: psycopg.Connection = Db):
    user = require_user(conn, user_id)
    rows = rsvps.user_rsvps(conn, user_id)
    return {
        "user": user_json(user),
        "rsvps": [
            {**rsvp_json(r), "event": {"id": r["event_id"], "title": r["event_title"],
                                       "starts_at": r["starts_at"], "location": r["location"],
                                       "club_id": r["club_id"], "club_name": r["club_name"]}}
            for r in rows
        ],
    }


@router.get("/users/{user_id}/notifications")
def user_notifications(user_id: int, conn: psycopg.Connection = Db):
    user = require_user(conn, user_id)
    return {"user": user_json(user), "notifications": queries.notifications(conn, user_id)}
