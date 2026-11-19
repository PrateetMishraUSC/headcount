"""Creating and editing clubs and events: the rules, then the pages."""

import html
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from headcount import manage, rsvps
from headcount.worker import Worker
from tests.factories import make_club, make_event, make_users
from tests.test_pages import sign_in, text

LA = ZoneInfo("America/Los_Angeles")


def local(dt: datetime) -> str:
    """What a datetime-local input would send for `dt`."""
    return dt.astimezone(LA).strftime("%Y-%m-%dT%H:%M")


def event_fields(**overrides):
    fields = dict(title="Blitz Night", description="Bring a clock.", location="Union 204",
                  starts_at=local(datetime.now(timezone.utc) + timedelta(days=7)), capacity="20")
    fields.update(overrides)
    return fields


def confirmed_count(db, event_id):
    return rsvps.seat_counts(db, event_id)["confirmed"]


# ------------------------------------------------------------ clubs


def test_create_club_tidies_input(db):
    club = manage.create_club(db, name="  Chess   Club ", description="  We play chess.\r\nOften.  ")
    assert club["name"] == "Chess Club"
    assert club["description"] == "We play chess.\nOften."


@pytest.mark.parametrize("name, message", [
    ("", "Name is required."),
    ("   ", "Name is required."),
    ("x" * 81, "Name can be at most 80 characters."),
])
def test_club_name_is_required_and_limited(db, name, message):
    with pytest.raises(manage.Invalid) as exc:
        manage.create_club(db, name=name)
    assert exc.value.errors == {"name": message}


def test_club_names_are_unique_ignoring_case(db):
    manage.create_club(db, name="Chess Club")
    with pytest.raises(manage.Invalid) as exc:
        manage.create_club(db, name="chess club")
    assert exc.value.errors == {"name": "There's already a club with this name."}


def test_update_club(db):
    chess = manage.create_club(db, name="Chess Club")
    manage.create_club(db, name="Go Club")

    renamed = manage.update_club(db, chess["id"], name="chess club", description="Now with go.")
    assert (renamed["name"], renamed["description"]) == ("chess club", "Now with go.")
    with pytest.raises(manage.Invalid):
        manage.update_club(db, chess["id"], name="GO CLUB")
    with pytest.raises(manage.ClubNotFound):
        manage.update_club(db, 999, name="Nobody")


# ------------------------------------------------------------ events


def test_create_event_reads_the_time_in_the_display_time_zone(db):
    club = make_club(db)
    event = manage.create_event(db, club["id"], tz=LA, **event_fields(starts_at="2030-01-15T18:30"))
    assert event["starts_at"] == datetime(2030, 1, 16, 2, 30, tzinfo=timezone.utc)
    assert (event["title"], event["capacity"], event["location"]) == ("Blitz Night", 20, "Union 204")


def test_event_validation_reports_every_problem_at_once(db):
    club = make_club(db)
    with pytest.raises(manage.Invalid) as exc:
        manage.create_event(db, club["id"], tz=LA, title=" ", location="", starts_at="", capacity="lots")
    assert exc.value.errors == {
        "title": "Title is required.",
        "location": "Location is required.",
        "starts_at": "Start time is required.",
        "capacity": "Capacity has to be a whole number.",
    }
    assert db.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


@pytest.mark.parametrize("field, value, message", [
    ("starts_at", "next tuesday", "Use a date and time like"),
    ("starts_at", local(datetime.now(timezone.utc) - timedelta(minutes=5)), "has to be in the future"),
    ("capacity", "0", "between 1 and 10,000"),
    ("capacity", "10001", "between 1 and 10,000"),
    ("capacity", "2.5", "whole number"),
    ("title", "x" * 121, "at most 120 characters"),
])
def test_event_field_rules(db, field, value, message):
    club = make_club(db)
    with pytest.raises(manage.Invalid) as exc:
        manage.create_event(db, club["id"], tz=LA, **event_fields(**{field: value}))
    assert list(exc.value.errors) == [field]
    assert message in exc.value.errors[field]


def test_create_event_for_a_missing_club(db):
    with pytest.raises(manage.ClubNotFound):
        manage.create_event(db, 999, tz=LA, **event_fields())


def test_update_event_changes_every_field(db):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=2)
    updated = manage.update_event(db, event["id"], tz=LA, **event_fields(
        title="Rapid Night", description="", location="Library", starts_at="2030-06-01T19:00", capacity="5"))
    assert updated["title"] == "Rapid Night" and updated["location"] == "Library"
    assert updated["capacity"] == 5 and updated["description"] == ""
    assert updated["starts_at"] == datetime(2030, 6, 2, 2, 0, tzinfo=timezone.utc)  # PDT


def test_capacity_cannot_drop_below_the_confirmed_count(db):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=5)
    for u in make_users(db, "Ann", "Bo", "Cy"):
        rsvps.rsvp(db, event["id"], u["id"])

    with pytest.raises(manage.Invalid) as exc:
        manage.update_event(db, event["id"], tz=LA, **event_fields(capacity="2"))
    assert "3 people are already confirmed" in exc.value.errors["capacity"]
    assert db.execute("SELECT capacity FROM events WHERE id = %s", (event["id"],)).fetchone()["capacity"] == 5

    manage.update_event(db, event["id"], tz=LA, **event_fields(capacity="3"))  # exactly full is fine
    assert db.execute("SELECT capacity FROM events WHERE id = %s", (event["id"],)).fetchone()["capacity"] == 3


def test_more_seats_go_to_the_waitlist_via_the_worker(db, database_url):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1)
    ann, bo, cy = make_users(db, "Ann", "Bo", "Cy")
    for u in (ann, bo, cy):
        rsvps.rsvp(db, event["id"], u["id"])

    manage.update_event(db, event["id"], tz=LA, **event_fields(capacity="2"))
    # Editing promotes nobody by itself; the worker does, in queue order.
    assert rsvps.seat_counts(db, event["id"]) == {"confirmed": 1, "waitlisted": 2}
    Worker(database_url, log=lambda _l: None).poll_once()
    assert [a["display_name"] for a in rsvps.attendees(db, event["id"])] == ["Ann", "Bo"]
    assert [w["display_name"] for w in rsvps.waitlist(db, event["id"])] == ["Cy"]


def test_started_or_missing_events_cannot_be_edited(db):
    club = make_club(db)
    past = make_event(db, club["id"], starts_in=timedelta(hours=-1))
    with pytest.raises(rsvps.EventStarted):
        manage.update_event(db, past["id"], tz=LA, **event_fields())
    with pytest.raises(rsvps.EventNotFound):
        manage.update_event(db, 999, tz=LA, **event_fields())


def test_capacity_change_waits_for_an_rsvp_in_progress(db, database_url):
    """An RSVP holding the event lock can't be missed by a capacity change.

    The RSVP confirms a 3rd person but hasn't committed. Lowering capacity to 2
    must wait for it, then see 3 confirmed and refuse. (Without the lock it
    would count 2, succeed, and leave the event overbooked.)
    """
    club = make_club(db)
    event = make_event(db, club["id"], capacity=5)
    users = make_users(db, "Ann", "Bo", "Cy")
    for u in users[:2]:
        rsvps.rsvp(db, event["id"], u["id"])

    outcome = {}

    def lower_capacity():
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
            try:
                manage.update_event(conn, event["id"], tz=LA, **event_fields(capacity="2"))
                outcome["result"] = "updated"
            except manage.Invalid:
                outcome["result"] = "refused"

    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as rsvp_conn:
        with rsvp_conn.transaction():
            assert rsvps.rsvp(rsvp_conn, event["id"], users[2]["id"]).status == "confirmed"
            editor = threading.Thread(target=lower_capacity)
            editor.start()
            time.sleep(0.5)
            assert editor.is_alive(), "the edit should be waiting for the RSVP's lock"
        editor.join(timeout=10)

    assert outcome == {"result": "refused"}
    assert confirmed_count(db, event["id"]) == 3
    assert db.execute("SELECT capacity FROM events WHERE id = %s", (event["id"],)).fetchone()["capacity"] == 5


# ------------------------------------------------------------ pages


def test_management_pages_need_sign_in(client, db):
    club = make_club(db)
    event = make_event(db, club["id"])
    for path in ("/clubs/new", f"/clubs/{club['id']}/edit", f"/clubs/{club['id']}/events/new",
                 f"/events/{event['id']}/edit"):
        for method in ("GET", "POST"):
            resp = client.request(method, path, follow_redirects=False)
            assert (resp.status_code, resp.headers["location"]) == (303, f"/signin?next={path}"), (method, path)
    assert db.execute("SELECT count(*) AS n FROM clubs").fetchone()["n"] == 1


def test_create_club_and_event_through_the_pages(client, db):
    sign_in(client, "Ann")
    assert "New club" in client.get("/").text

    form = client.get("/clubs/new")
    assert form.status_code == 200 and "Create club" in form.text
    resp = client.post("/clubs/new", data={"name": "Film Society", "description": "Weekly screenings."},
                       follow_redirects=False)
    club = db.execute("SELECT * FROM clubs").fetchone()
    assert (resp.status_code, resp.headers["location"]) == (303, f"/clubs/{club['id']}")
    page = text(client.get(f"/clubs/{club['id']}"))
    assert "Film Society" in page and "Weekly screenings." in page and "New event" in page

    assert "Create event" in client.get(f"/clubs/{club['id']}/events/new").text
    resp = client.post(f"/clubs/{club['id']}/events/new", follow_redirects=False, data=event_fields(
        title="Movie Night", location="Room 101", starts_at="2030-01-15T18:30", capacity="40"))
    event = db.execute("SELECT * FROM events").fetchone()
    assert (resp.status_code, resp.headers["location"]) == (303, f"/events/{event['id']}")
    page = text(client.get(f"/events/{event['id']}"))
    assert "Movie Night" in page and "Room 101" in page
    assert "Tue 15 Jan 2030 · 6:30 PM" in page  # shown back in the display time zone
    assert "0 of 40 seats taken" in page and "Edit event" in page


def test_invalid_forms_are_shown_again_with_the_problems(client, db):
    sign_in(client, "Ann")
    make_club(db, name="Chess Club")
    resp = client.post("/clubs/new", data={"name": "chess club", "description": "Mine."})
    assert resp.status_code == 400
    assert "There's already a club with this name." in html.unescape(resp.text)
    assert 'value="chess club"' in resp.text and "Mine." in resp.text  # what they typed is kept

    club = db.execute("SELECT id FROM clubs").fetchone()
    resp = client.post(f"/clubs/{club['id']}/events/new", data=event_fields(title="Quiz", capacity="0"))
    assert resp.status_code == 400
    assert "Capacity has to be between 1 and 10,000." in resp.text
    assert 'value="Quiz"' in resp.text and 'aria-invalid="true"' in resp.text
    assert db.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 0


def test_edit_club_through_the_pages(client, db):
    club = make_club(db, name="Chess Club", description="We play chess.")
    sign_in(client, "Ann")
    form = client.get(f"/clubs/{club['id']}/edit")
    assert 'value="Chess Club"' in form.text and "We play chess." in form.text
    resp = client.post(f"/clubs/{club['id']}/edit", data={"name": "Chess & Go", "description": ""},
                       follow_redirects=False)
    assert resp.headers["location"] == f"/clubs/{club['id']}"
    assert "Chess &amp; Go" in client.get("/").text


def test_edit_event_through_the_pages(client, db):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=3)
    ann, bo = make_users(db, "Ann", "Bo")
    rsvps.rsvp(db, event["id"], ann["id"])
    rsvps.rsvp(db, event["id"], bo["id"])
    sign_in(client, "Cy")

    form = client.get(f"/events/{event['id']}/edit")
    assert form.status_code == 200
    assert f'value="{local(event["starts_at"])}"' in form.text  # prefilled in local time
    assert "2 confirmed. Seats can't go below 2." in text(form)

    resp = client.post(f"/events/{event['id']}/edit", data=event_fields(capacity="1"))
    assert resp.status_code == 400 and "2 people are already confirmed" in resp.text

    resp = client.post(f"/events/{event['id']}/edit", follow_redirects=False,
                       data=event_fields(title="Blitz Night (moved)", capacity="10"))
    assert resp.headers["location"] == f"/events/{event['id']}"
    page = text(client.get(f"/events/{event['id']}"))
    assert "Blitz Night (moved)" in page and "2 of 10 seats taken" in page


def test_started_events_have_no_edit_page(client, db):
    club = make_club(db)
    past = make_event(db, club["id"], starts_in=timedelta(hours=-1))
    sign_in(client, "Ann")
    assert "Edit event" not in client.get(f"/events/{past['id']}").text
    for resp in (client.get(f"/events/{past['id']}/edit"),
                 client.post(f"/events/{past['id']}/edit", data=event_fields())):
        assert resp.status_code == 409 and "can't be edited" in html.unescape(resp.text)


def test_management_pages_404_for_unknown_clubs_and_events(client, db):
    sign_in(client, "Ann")
    for path in ("/clubs/999/edit", "/clubs/999/events/new", "/events/999/edit"):
        assert client.get(path).status_code == 404, path
    assert client.post("/clubs/999/events/new", data=event_fields()).status_code == 404
