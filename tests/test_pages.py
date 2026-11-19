import re
from datetime import timedelta

import psycopg
import pytest

from headcount import rsvps
from headcount.pages import COOKIE
from headcount.worker import Worker
from tests.factories import make_club, make_event, make_user, make_users


def sign_in(client, name):
    resp = client.post("/signin", data={"display_name": name, "next": "/"}, follow_redirects=False)
    assert resp.status_code == 303
    return resp


def text(resp):
    """Visible text with tags and extra whitespace removed, for simple assertions."""
    return " ".join(re.sub(r"<[^>]+>", " ", resp.text).split())


@pytest.fixture
def event(db):
    club = make_club(db, description="We play chess.")
    return make_event(db, club["id"], capacity=2, title="Blitz Night")


# ------------------------------------------------------------ browsing


def test_club_list_and_club_page(client, db, event):
    make_event(db, event["club_id"], title="Old Game", starts_in=timedelta(days=-3))

    home = client.get("/")
    assert home.status_code == 200
    assert "Chess Club" in home.text
    assert "1 upcoming event" in text(home)

    club = client.get(f"/clubs/{event['club_id']}")
    assert club.status_code == 200
    body = text(club)
    assert "Upcoming events" in body and "Blitz Night" in body
    assert "Past events" in body and "Old Game" in body
    assert "0 of 2 seats taken · 2 left" in body


def test_event_page_shows_capacity_attendees_and_waitlist(client, db, event):
    ann, bo, cy = make_users(db, "Ann", "Bo", "Cy")
    for u in (ann, bo, cy):
        rsvps.rsvp(db, event["id"], u["id"])

    page = client.get(f"/events/{event['id']}")

    body = text(page)
    assert "2 of 2 seats taken · full · 1 waiting" in body
    assert "Going (2) Ann Bo" in body
    assert "Waitlist (1) #1 Cy" in body
    assert "Bring a clock." in body and "Student Union 204" in body
    assert "Sign in to RSVP" in body
    assert 'style="width: 100%"' in page.text


def test_user_content_is_escaped(client, db):
    club = make_club(db, name="<script>alert(1)</script>")
    page = client.get(f"/clubs/{club['id']}")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


# ------------------------------------------------------------ sign in


def test_sign_in_sets_a_cookie_and_names_are_case_insensitive(client, db):
    resp = sign_in(client, "  Ada  Lovelace ")
    cookie = resp.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Ada Lovelace" in client.get("/").text

    client.post("/signout")
    sign_in(client, "ada lovelace")
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 1


def test_sign_in_rejects_blank_names(client, db):
    resp = client.post("/signin", data={"display_name": "   "})
    assert resp.status_code == 400
    assert "Pick a display name" in resp.text


def test_sign_in_only_redirects_within_the_site(client, db):
    for target, expected in [("/events/3", "/events/3"), ("//evil.example", "/"),
                             ("https://evil.example", "/"), ("/\\evil.example", "/")]:
        resp = client.post("/signin", data={"display_name": "Ann", "next": target}, follow_redirects=False)
        assert resp.headers["location"] == expected


def test_stale_cookie_counts_as_signed_out(client, db):
    client.cookies.set(COOKIE, "999")
    assert "Sign in" in client.get("/").text


# ------------------------------------------------------------ RSVP flow


def test_rsvp_cancel_and_waitlist_through_the_pages(make_client, db, event):
    ann = make_client()
    bo = make_client()
    cy = make_client()
    sign_in(ann, "Ann")
    sign_in(bo, "Bo")
    sign_in(cy, "Cy")

    resp = ann.post(f"/events/{event['id']}/rsvp", follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, f"/events/{event['id']}")
    assert "You're going." in ann.get(f"/events/{event['id']}").text

    bo.post(f"/events/{event['id']}/rsvp")
    page = cy.post(f"/events/{event['id']}/rsvp")  # follows the redirect
    assert "You're #1 on the waitlist" in text(page)

    ann.post(f"/events/{event['id']}/cancel")
    page = text(cy.get(f"/events/{event['id']}"))
    assert "#1 on the waitlist" in page  # cancelling doesn't promote by itself
    assert "1 left · 1 waiting" in page

    dan = make_client()
    sign_in(dan, "Dan")
    page = text(dan.get(f"/events/{event['id']}"))
    assert "Join the waitlist" in page and "going to the next person on the waitlist" in page

    cy.post(f"/events/{event['id']}/cancel")
    assert "RSVP" in text(cy.get(f"/events/{event['id']}"))


def test_rsvp_when_signed_out_goes_to_sign_in(client, event):
    resp = client.post(f"/events/{event['id']}/rsvp", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/signin?next=/events/{event['id']}"


def test_past_events_are_closed(client, db, event):
    past = make_event(db, event["club_id"], title="Done", starts_in=timedelta(hours=-2))
    sign_in(client, "Ann")
    assert "RSVPs are closed" in client.get(f"/events/{past['id']}").text
    resp = client.post(f"/events/{past['id']}/rsvp")
    assert resp.status_code == 409
    assert "already started" in resp.text


# ------------------------------------------------------------ your RSVPs and notifications


def test_your_rsvps_page(client, db, event):
    sign_in(client, "Ann")
    ann = db.execute("SELECT id FROM users WHERE display_name = 'Ann'").fetchone()
    other = make_event(db, event["club_id"], capacity=1, title="Simul")
    rsvps.rsvp(db, other["id"], make_user(db, "Zed")["id"])
    rsvps.rsvp(db, event["id"], ann["id"])
    rsvps.rsvp(db, other["id"], ann["id"])

    body = text(client.get("/me/rsvps"))
    assert "Blitz Night Chess Club" in body and "Going" in body
    assert "Simul Chess Club" in body and "Waitlist #1" in body


def test_notifications_inbox_marks_items_read(client, db, database_url):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1, title="Blitz Night")
    zed = make_user(db, "Zed")
    rsvps.rsvp(db, event["id"], zed["id"])
    sign_in(client, "Ann")
    ann = db.execute("SELECT id FROM users WHERE display_name = 'Ann'").fetchone()
    rsvps.rsvp(db, event["id"], ann["id"])
    rsvps.cancel(db, event["id"], zed["id"])
    Worker(database_url, log=lambda _l: None).poll_once()

    assert '<span class="badge">1</span>' in client.get("/").text
    inbox = text(client.get("/notifications"))
    assert "New You got a spot at Blitz Night!" in inbox
    assert 'class="badge"' not in client.get("/").text
    assert "New" not in text(client.get("/notifications"))


def test_pages_need_sign_in(client, db):
    for path in ("/me/rsvps", "/notifications"):
        resp = client.get(path, follow_redirects=False)
        assert resp.headers["location"] == f"/signin?next={path}"


# ------------------------------------------------------------ errors


def test_unknown_pages_get_an_html_404(client, db):
    resp = client.get("/events/999")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "no such event" in resp.text


def test_slow_page_returns_an_html_504(db, database_url, make_client, event):
    client = make_client(statement_timeout_ms=200)
    with psycopg.connect(database_url) as blocker:
        blocker.execute("LOCK TABLE events IN ACCESS EXCLUSIVE MODE")
        resp = client.get(f"/events/{event['id']}")
        blocker.rollback()
    assert resp.status_code == 504
    assert resp.headers["content-type"].startswith("text/html")
    assert "took too long" in resp.text


def test_stylesheet_is_served(client):
    resp = client.get("/static/style.css")
    assert resp.status_code == 200
    assert "--accent" in resp.text
