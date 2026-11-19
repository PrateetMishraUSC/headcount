"""Server-rendered HTML pages.

Sign-in is a display name stored in a cookie: no passwords. Typing someone
else's name signs you in as them, which is the deal for a club tool like this;
the cookie holds the user id and is HttpOnly + SameSite=Lax, so other sites
can't submit RSVP forms on your behalf.
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import psycopg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from headcount import queries, rsvps
from headcount.deps import get_db, settings_for

COOKIE = "headcount_user"
COOKIE_MAX_AGE = 365 * 24 * 3600

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@pass_context
def _when(context, value: datetime, fmt: str = "%a %-d %b %Y · %-I:%M %p") -> str:
    """Format a timestamp in the configured display time zone."""
    return value.astimezone(context["tz"]).strftime(fmt)


templates.env.filters["when"] = _when


# ---------------------------------------------------------------- helpers


def get_viewer(request: Request, conn: psycopg.Connection = Depends(get_db)) -> dict | None:
    """The signed-in user, or None. Unknown or malformed cookies count as signed out."""
    raw = request.cookies.get(COOKIE, "")
    if not raw.isdigit():
        return None
    return rsvps.get_user(conn, int(raw))


Db = Depends(get_db)
Viewer = Depends(get_viewer)


def render(request: Request, conn, viewer, template: str, status_code: int = 200, **context):
    context.update(
        viewer=viewer,
        unread=queries.unread_count(conn, viewer["id"]) if viewer else 0,
        tz=ZoneInfo(settings_for(request).display_timezone),
    )
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def safe_next(target: str | None) -> str:
    """Only redirect back to paths on this site (no `//evil.example`)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def signin_redirect(next_path: str) -> RedirectResponse:
    return redirect(f"/signin?next={quote(next_path)}")


# ---------------------------------------------------------------- sign in / out


@router.get("/signin")
def signin_form(request: Request, next: str = "/", conn=Db, viewer=Viewer):
    return render(request, conn, viewer, "signin.html", next=safe_next(next), error=None, name="")


@router.post("/signin")
def signin(request: Request, display_name: str = Form(""), next: str = Form("/"), conn=Db):
    try:
        user = rsvps.find_or_create_user(conn, display_name)
    except ValueError as exc:
        return render(request, conn, None, "signin.html", status_code=400,
                      next=safe_next(next), error=str(exc), name=display_name)
    response = redirect(safe_next(next))
    response.set_cookie(COOKIE, str(user["id"]), max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax")
    return response


@router.post("/signout")
def signout():
    response = redirect("/")
    response.delete_cookie(COOKIE)
    return response


# ---------------------------------------------------------------- browsing


@router.get("/")
def club_list(request: Request, conn=Db, viewer=Viewer):
    return render(request, conn, viewer, "clubs.html", clubs=queries.list_clubs(conn))


@router.get("/clubs/{club_id}")
def club_page(club_id: int, request: Request, conn=Db, viewer=Viewer):
    club = queries.get_club(conn, club_id)
    if club is None:
        raise HTTPException(404, "There's no such club.")
    events = queries.club_events(conn, club_id)
    return render(
        request, conn, viewer, "club.html", club=club,
        upcoming=[e for e in events if not e["is_past"]],
        past=[e for e in events if e["is_past"]],
    )


@router.get("/events/{event_id}")
def event_page(event_id: int, request: Request, conn=Db, viewer=Viewer):
    event = queries.get_event(conn, event_id)
    if event is None:
        raise HTTPException(404, "There's no such event.")
    mine = rsvps.current_rsvp(conn, event_id, viewer["id"]) if viewer else None
    return render(
        request, conn, viewer, "event.html", event=event, mine=mine,
        attendees=rsvps.attendees(conn, event_id),
        waitlist=rsvps.waitlist(conn, event_id),
    )


# ---------------------------------------------------------------- RSVP / cancel


@router.post("/events/{event_id}/rsvp")
def rsvp(event_id: int, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/events/{event_id}")
    try:
        rsvps.rsvp(conn, event_id, viewer["id"])
    except rsvps.EventNotFound:
        raise HTTPException(404, "There's no such event.")
    except rsvps.EventStarted:
        raise HTTPException(409, "This event has already started, so RSVPs are closed.")
    return redirect(f"/events/{event_id}")


@router.post("/events/{event_id}/cancel")
def cancel(event_id: int, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/events/{event_id}")
    try:
        rsvps.cancel(conn, event_id, viewer["id"])
    except rsvps.EventNotFound:
        raise HTTPException(404, "There's no such event.")
    except rsvps.NoActiveRsvp:
        pass  # already cancelled (double click, two tabs): nothing to do
    except rsvps.EventStarted:
        raise HTTPException(409, "This event has already started, so it can't be cancelled.")
    return redirect(f"/events/{event_id}")


# ---------------------------------------------------------------- your stuff


@router.get("/me/rsvps")
def my_rsvps(request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/me/rsvps")
    rows = rsvps.user_rsvps(conn, viewer["id"])
    upcoming = [r for r in rows if not r["is_past"] and r["status"] != "cancelled"]
    other = [r for r in rows if r not in upcoming]
    other.sort(key=lambda r: r["starts_at"], reverse=True)
    return render(request, conn, viewer, "my_rsvps.html", upcoming=upcoming, other=other)


@router.get("/notifications")
def notifications(request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/notifications")
    items = queries.notifications(conn, viewer["id"])
    # Opening the inbox marks everything read; this render still shows which were new.
    queries.mark_notifications_read(conn, viewer["id"])
    return render(request, conn, viewer, "notifications.html", items=items)
