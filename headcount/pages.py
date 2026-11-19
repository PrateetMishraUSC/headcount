"""Server-rendered HTML pages.

Sign-in is by display name. A name without a password signs in with just the
name (anyone typing it gets in, which is the deal for members); a name with a
password needs it. Club organisers must have a password before they can manage
anything (see auth.py and organisers.py).

The cookie is signed (auth.cookie_value), HttpOnly and SameSite=Lax, so it
can't be forged and other sites can't submit forms on your behalf.
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

from headcount import auth, manage, organisers, queries, rsvps
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
    """The signed-in user (with has_password), or None. Bad or stale cookies count as signed out."""
    return auth.user_from_cookie(conn, settings_for(request).secret_key, request.cookies.get(COOKIE))


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


def password_redirect(next_path: str) -> RedirectResponse:
    """Organiser actions need a password: set one, then come back."""
    return redirect(f"/me/account?next={quote(next_path)}")


def signed_in(request: Request, user: dict, target: str) -> RedirectResponse:
    """Redirect with a fresh sign-in cookie for `user` (a row that includes password_hash)."""
    response = redirect(target)
    response.set_cookie(
        COOKIE, auth.cookie_value(settings_for(request).secret_key, user), max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="lax", secure=request.url.scheme == "https",
    )
    return response


def can_manage(conn, viewer: dict | None, club_id: int) -> bool:
    return bool(viewer and viewer["has_password"] and organisers.is_organiser(conn, club_id, viewer["id"]))


def manager_check(conn, viewer: dict | None, club_id: int, path: str) -> RedirectResponse | None:
    """None if the viewer may manage this club; otherwise the response to send instead.

    `path` is a page (GET) to come back to after signing in or setting a password.
    """
    if viewer is None:
        return signin_redirect(path)
    if not organisers.is_organiser(conn, club_id, viewer["id"]):
        raise HTTPException(403, "Only this club's organisers can do that.")
    if not viewer["has_password"]:
        return password_redirect(path)
    return None


# ---------------------------------------------------------------- sign in / out


@router.get("/signin")
def signin_form(request: Request, next: str = "/", conn=Db, viewer=Viewer):
    return render(request, conn, viewer, "signin.html", next=safe_next(next), error=None, name="",
                  needs_password=False)


@router.post("/signin")
def signin(request: Request, display_name: str = Form(""), password: str = Form(""),
           next: str = Form("/"), conn=Db):
    def again(error, needs_password=False, status_code=400):
        return render(request, conn, None, "signin.html", status_code=status_code, next=safe_next(next),
                      error=error, name=display_name, needs_password=needs_password)

    try:
        user = auth.authenticate(conn, display_name, password)
    except ValueError as exc:
        return again(str(exc))
    except auth.PasswordRequired:
        return again(None, needs_password=True, status_code=200)
    except auth.WrongPassword:
        return again("That password isn't right.", needs_password=True)
    return signed_in(request, user, safe_next(next))


@router.post("/signout")
def signout():
    response = redirect("/")
    response.delete_cookie(COOKIE)
    return response


def account_page(request, conn, viewer, *, next="", saved=False, error=None, status_code=200):
    return render(request, conn, viewer, "account.html", status_code=status_code,
                  next=safe_next(next) if next else "", saved=saved, error=error,
                  clubs=organisers.clubs_organised_by(conn, viewer["id"]))


@router.get("/me/account")
def account(request: Request, next: str = "", saved: bool = False, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/me/account")
    return account_page(request, conn, viewer, next=next, saved=saved)


@router.post("/me/account")
def set_password(request: Request, current_password: str = Form(""), new_password: str = Form(""),
                 confirm_password: str = Form(""), next: str = Form(""), conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect("/me/account")
    try:
        user = auth.set_password(conn, viewer["id"], new_password,
                                 current_password=current_password, confirm=confirm_password)
    except auth.WrongPassword:
        return account_page(request, conn, viewer, next=next, status_code=400,
                            error="Your current password isn't right.")
    except ValueError as exc:
        return account_page(request, conn, viewer, next=next, status_code=400, error=str(exc))
    # The cookie is signed over the password, so issue a new one (other browsers are signed out).
    return signed_in(request, user, safe_next(next) if next else "/me/account?saved=true")


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


def club_creator_check(viewer) -> RedirectResponse | None:
    """Starting a club makes you its organiser, so it needs a password too."""
    if viewer is None:
        return signin_redirect("/clubs/new")
    if not viewer["has_password"]:
        return password_redirect("/clubs/new")
    return None


@router.get("/clubs/new")
def new_club_page(request: Request, conn=Db, viewer=Viewer):
    if denied := club_creator_check(viewer):
        return denied
    return club_form(request, conn, viewer)


@router.post("/clubs/new")
def create_club(request: Request, name: str = Form(""), description: str = Form(""),
                conn=Db, viewer=Viewer):
    if denied := club_creator_check(viewer):
        return denied
    form = {"name": name, "description": description}
    try:
        club = manage.create_club(conn, **form, organiser_id=viewer["id"])
    except manage.Invalid as exc:
        return club_form(request, conn, viewer, form=form, errors=exc.errors, status_code=400)
    return redirect(f"/clubs/{club['id']}")


@router.get("/clubs/{club_id}/edit")
def edit_club_page(club_id: int, request: Request, conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/edit")
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, f"/clubs/{club_id}/edit"):
        return denied
    return club_form(request, conn, viewer, club=club)


@router.post("/clubs/{club_id}/edit")
def update_club(club_id: int, request: Request, name: str = Form(""), description: str = Form(""),
                conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/edit")
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, f"/clubs/{club_id}/edit"):
        return denied
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
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, f"/clubs/{club_id}/events/new"):
        return denied
    return event_form(request, conn, viewer, club=club)


@router.post("/clubs/{club_id}/events/new")
def create_event(club_id: int, request: Request, title: str = Form(""), description: str = Form(""),
                 location: str = Form(""), starts_at: str = Form(""), capacity: str = Form(""),
                 conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/clubs/{club_id}/events/new")
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, f"/clubs/{club_id}/events/new"):
        return denied
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
    if denied := manager_check(conn, viewer, event["club_id"], f"/events/{event_id}/edit"):
        return denied
    club = get_club_or_404(conn, event["club_id"])
    return event_form(request, conn, viewer, club=club, event=event)


@router.post("/events/{event_id}/edit")
def update_event(event_id: int, request: Request, title: str = Form(""), description: str = Form(""),
                 location: str = Form(""), starts_at: str = Form(""), capacity: str = Form(""),
                 conn=Db, viewer=Viewer):
    if viewer is None:
        return signin_redirect(f"/events/{event_id}/edit")
    event = editable_event_or_409(conn, event_id)
    if denied := manager_check(conn, viewer, event["club_id"], f"/events/{event_id}/edit"):
        return denied
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


def organisers_page(request, conn, viewer, club, *, error=None, name="", status_code=200):
    return render(request, conn, viewer, "organisers.html", status_code=status_code, club=club,
                  people=organisers.club_organisers(conn, club["id"]), error=error, name=name)


@router.get("/clubs/{club_id}/organisers")
def club_organisers(club_id: int, request: Request, conn=Db, viewer=Viewer):
    path = f"/clubs/{club_id}/organisers"
    if viewer is None:
        return signin_redirect(path)
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, path):
        return denied
    return organisers_page(request, conn, viewer, club)


@router.post("/clubs/{club_id}/organisers")
def add_organiser(club_id: int, request: Request, display_name: str = Form(""), conn=Db, viewer=Viewer):
    path = f"/clubs/{club_id}/organisers"
    if viewer is None:
        return signin_redirect(path)
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, path):
        return denied
    try:
        organisers.add_organiser(conn, club_id, display_name, added_by=viewer["id"])
    except rsvps.UserNotFound:
        return organisers_page(request, conn, viewer, club, name=display_name, status_code=400, error=(
            f"Nobody called {' '.join(display_name.split())} has signed in yet. "
            "Ask them to sign in once, then add them."))
    except ValueError as exc:
        return organisers_page(request, conn, viewer, club, name=display_name, status_code=400, error=str(exc))
    return redirect(path)


@router.post("/clubs/{club_id}/organisers/{user_id}/remove")
def remove_organiser(club_id: int, user_id: int, request: Request, conn=Db, viewer=Viewer):
    path = f"/clubs/{club_id}/organisers"
    if viewer is None:
        return signin_redirect(path)
    club = get_club_or_404(conn, club_id)
    if denied := manager_check(conn, viewer, club_id, path):
        return denied
    try:
        organisers.remove_organiser(conn, club_id, user_id)
    except organisers.NotAnOrganiser:
        pass  # already removed (double click, two tabs)
    except organisers.LastOrganiser:
        return organisers_page(request, conn, viewer, club, status_code=409, error=(
            "A club needs at least one organiser. Add someone else before removing yourself."))
    return redirect(f"/clubs/{club_id}" if user_id == viewer["id"] else path)


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
    people = organisers.club_organisers(conn, club_id)
    is_organiser = bool(viewer) and any(p["user_id"] == viewer["id"] for p in people)
    return render(
        request, conn, viewer, "club.html", club=club, organisers=people, is_organiser=is_organiser,
        can_manage=is_organiser and viewer["has_password"],
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
        can_manage=can_manage(conn, viewer, event["club_id"]),
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
