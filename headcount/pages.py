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

from headcount import manage, queries, rsvps
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
        tz=display_tz(request),
    )
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def display_tz(request: Request) -> ZoneInfo:
    return ZoneInfo(settings_for(request).display_timezone)


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


# ---------------------------------------------------------------- creating and editing
# (Declared before /clubs/{club_id} so that /clubs/new isn't read as a club id.)

CLUB_FIELDS = ("name", "description")
EVENT_FIELDS = ("title", "description", "location", "starts_at", "capacity")


def local_input(value: datetime, tz: ZoneInfo) -> str:
    """A timestamp as <input type=datetime-local> expects it, in the display time zone."""
    return value.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def get_club_or_404(conn, club_id: int) -> dict:
    club = queries.get_club(conn, club_id)
    if club is None:
        raise HTTPException(404, "There's no such club.")
    return club


def get_event_or_404(conn, event_id: int) -> dict:
    event = queries.get_event(conn, event_id)
    if event is None:
        raise HTTPException(404, "There's no such event.")
    return event


def editable_event_or_409(conn, event_id: int) -> dict:
    event = get_event_or_404(conn, event_id)
    if event["is_past"]:
        raise HTTPException(409, "This event has already started, so it can't be edited.")
    return event


def club_form(request, conn, viewer, *, club=None, form=None, errors=None, status_code=200):
    form = form or {f: (club or {}).get(f, "") for f in CLUB_FIELDS}
    return render(request, conn, viewer, "club_form.html", status_code=status_code,
                  club=club, form=form, errors=errors or {})


def event_form(request, conn, viewer, *, club, event=None, form=None, errors=None, status_code=200):
    tz = display_tz(request)
    if form is None and event is not None:
        form = {f: event[f] for f in EVENT_FIELDS}
        form["starts_at"] = local_input(event["starts_at"], tz)
    return render(request, conn, viewer, "event_form.html", status_code=status_code,
                  club=club, event=event, errors=errors or {},
                  form=form or {f: "" for f in EVENT_FIELDS},
                  min_start=local_input(datetime.now(tz), tz))


@router.get("/clubs/new")
def new_club_page(request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/clubs/new")
    return club_form(request, conn, viewer)


@router.post("/clubs/new")
def create_club(request: Request, name: str = Form(""), description: str = Form(""),
                conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/clubs/new")
    form = {"name": name, "description": description}
    try:
        club = manage.create_club(conn, **form)
    except manage.Invalid as exc:
        return club_form(request, conn, viewer, form=form, errors=exc.errors, status_code=400)
    return redirect(f"/clubs/{club['id']}")


@router.get("/clubs/{club_id}/edit")
def edit_club_page(club_id: int, request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/edit")
    return club_form(request, conn, viewer, club=get_club_or_404(conn, club_id))


@router.post("/clubs/{club_id}/edit")
def update_club(club_id: int, request: Request, name: str = Form(""), description: str = Form(""),
                conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/edit")
    club = get_club_or_404(conn, club_id)
    form = {"name": name, "description": description}
    try:
        manage.update_club(conn, club_id, **form)
    except manage.Invalid as exc:
        return club_form(request, conn, viewer, club=club, form=form, errors=exc.errors, status_code=400)
    except manage.ClubNotFound:
        raise HTTPException(404, "There's no such club.")
    return redirect(f"/clubs/{club_id}")


@router.get("/clubs/{club_id}/events/new")
def new_event_page(club_id: int, request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/events/new")
    return event_form(request, conn, viewer, club=get_club_or_404(conn, club_id))


@router.post("/clubs/{club_id}/events/new")
def create_event(club_id: int, request: Request, title: str = Form(""), description: str = Form(""),
                 location: str = Form(""), starts_at: str = Form(""), capacity: str = Form(""),
                 conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/events/new")
    club = get_club_or_404(conn, club_id)
    form = dict(title=title, description=description, location=location,
                starts_at=starts_at, capacity=capacity)
    try:
        event = manage.create_event(conn, club_id, tz=display_tz(request), **form)
    except manage.Invalid as exc:
        return event_form(request, conn, viewer, club=club, form=form, errors=exc.errors, status_code=400)
    except manage.ClubNotFound:
        raise HTTPException(404, "There's no such club.")
    return redirect(f"/events/{event['id']}")


@router.get("/events/{event_id}/edit")
def edit_event_page(event_id: int, request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/events/{event_id}/edit")
    event = editable_event_or_409(conn, event_id)
    club = get_club_or_404(conn, event["club_id"])
    return event_form(request, conn, viewer, club=club, event=event)


@router.post("/events/{event_id}/edit")
def update_event(event_id: int, request: Request, title: str = Form(""), description: str = Form(""),
                 location: str = Form(""), starts_at: str = Form(""), capacity: str = Form(""),
                 conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/events/{event_id}/edit")
    event = editable_event_or_409(conn, event_id)
    form = dict(title=title, description=description, location=location,
                starts_at=starts_at, capacity=capacity)
    try:
        manage.update_event(conn, event_id, tz=display_tz(request), **form)
    except manage.Invalid as exc:
        club = get_club_or_404(conn, event["club_id"])
        return event_form(request, conn, viewer, club=club, event=event, form=form,
                          errors=exc.errors, status_code=400)
    except rsvps.EventNotFound:
        raise HTTPException(404, "There's no such event.")
    except rsvps.EventStarted:
        raise HTTPException(409, "This event has already started, so it can't be edited.")
    return redirect(f"/events/{event_id}")


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
