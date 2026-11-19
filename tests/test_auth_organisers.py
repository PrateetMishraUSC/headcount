"""Passwords, the signed sign-in cookie, and club organisers."""

import html
import threading

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from headcount import auth, organisers, rsvps
from headcount.config import Settings
from headcount.manage import ClubNotFound
from headcount.pages import COOKIE
from headcount.seed import SEED_ORGANISER_PASSWORD, seed
from headcount.web import create_app
from tests.conftest import TEST_SECRET_KEY
from tests.factories import ORGANISER_PASSWORD, make_club, make_event, make_organiser, make_user
from tests.test_pages import sign_in, text

PW = ORGANISER_PASSWORD


def body(resp):
    return html.unescape(resp.text)


@pytest.fixture
def club(db):
    return make_club(db, name="Chess Club")


# ------------------------------------------------------------ passwords


def test_hash_and_verify():
    stored = auth.hash_password("hunter2hunter2")
    assert stored.startswith("scrypt$") and "hunter2" not in stored
    assert auth.verify_password("hunter2hunter2", stored)
    assert not auth.verify_password("hunter2hunter3", stored)
    assert not auth.verify_password("", stored)
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same password") != auth.hash_password("same password")  # salted


@pytest.mark.parametrize("password, confirm, message", [
    ("short", None, "at least 8"),
    ("x" * 201, None, "at most 200"),
    ("long enough", "long enougH", "don't match"),
])
def test_new_password_rules(password, confirm, message):
    with pytest.raises(ValueError, match=message):
        auth.check_new_password(password, confirm)


def test_authenticate(db):
    new = auth.authenticate(db, "  New  Person ")
    assert new["display_name"] == "New Person" and new["password_hash"] is None
    assert auth.authenticate(db, "new person", "ignored when there's no password")["id"] == new["id"]

    auth.set_password(db, new["id"], PW)
    with pytest.raises(auth.PasswordRequired):
        auth.authenticate(db, "New Person")
    with pytest.raises(auth.WrongPassword):
        auth.authenticate(db, "NEW PERSON", "wrong password")
    assert auth.authenticate(db, "new person", PW)["id"] == new["id"]


def test_changing_a_password_needs_the_current_one(db):
    ann = make_user(db, "Ann")
    auth.set_password(db, ann["id"], PW)
    with pytest.raises(auth.WrongPassword):
        auth.set_password(db, ann["id"], "a new password", current_password="nope")
    with pytest.raises(ValueError, match="at least 8"):
        auth.set_password(db, ann["id"], "short", current_password=PW)
    auth.set_password(db, ann["id"], "a new password", current_password=PW)
    assert auth.authenticate(db, "Ann", "a new password")["id"] == ann["id"]


# ------------------------------------------------------------ the cookie


def test_cookie_is_signed(db):
    ann = auth.authenticate(db, "Ann")
    good = auth.cookie_value(TEST_SECRET_KEY, ann)
    assert auth.user_from_cookie(db, TEST_SECRET_KEY, good)["display_name"] == "Ann"

    user_id, _, signature = good.partition(".")
    bo = auth.authenticate(db, "Bo")
    for forged in (user_id,                                   # the old unsigned format
                   f"{bo['id']}.{signature}",                 # someone else's id, Ann's signature
                   f"{user_id}.{signature[:-1]}x",            # tampered signature
                   "9" * 30 + ".abc", "", None, "abc.def"):
        assert auth.user_from_cookie(db, TEST_SECRET_KEY, forged) is None, forged
    assert auth.user_from_cookie(db, "another-secret-key-of-at-least-32-chars", good) is None


def test_setting_a_password_invalidates_old_cookies(db):
    ann = auth.authenticate(db, "Ann")
    old = auth.cookie_value(TEST_SECRET_KEY, ann)
    ann = auth.set_password(db, ann["id"], PW)
    assert auth.user_from_cookie(db, TEST_SECRET_KEY, old) is None
    fresh = auth.user_from_cookie(db, TEST_SECRET_KEY, auth.cookie_value(TEST_SECRET_KEY, ann))
    assert fresh["has_password"] is True and "password_hash" not in fresh


def test_app_refuses_to_start_without_a_secret_key(database_url):
    settings = Settings(database_url=database_url, statement_timeout_ms=5000, connect_timeout_s=5,
                        worker_poll_interval_s=1, worker_reconnect_every=360, secret_key="too short")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        with TestClient(create_app(settings)):
            pass


# ------------------------------------------------------------ sign-in pages


def test_protected_names_ask_for_the_password(client, db):
    make_organiser(db, make_club(db)["id"], "Ann")

    first = client.post("/signin", data={"display_name": "ann", "next": "/clubs/1"})
    assert first.status_code == 200
    assert 'type="password"' in first.text and "protected with a password" in first.text
    assert COOKIE not in client.cookies

    wrong = client.post("/signin", data={"display_name": "ann", "password": "nope", "next": "/"})
    assert wrong.status_code == 400 and "That password isn't right." in body(wrong)

    resp = client.post("/signin", data={"display_name": "ann", "password": PW, "next": "/clubs/1"},
                       follow_redirects=False)
    assert (resp.status_code, resp.headers["location"]) == (303, "/clubs/1")
    assert "Ann" in client.get("/").text


def test_a_hand_made_cookie_does_not_sign_you_in(client, db):
    ann = make_user(db, "Ann")
    client.cookies.set(COOKIE, str(ann["id"]))  # what the cookie used to contain
    page = client.get("/")
    assert "Sign in" in page.text and "Ann" not in page.text


def test_setting_a_password_signs_out_other_browsers(make_client, db):
    mine, theirs = make_client(), make_client()
    sign_in(mine, "Ann")
    sign_in(theirs, "Ann")  # someone else typing my name, before I had a password

    resp = mine.post("/me/account", data={"new_password": PW, "confirm_password": PW}, follow_redirects=False)
    assert resp.headers["location"] == "/me/account?saved=true"
    assert "Password saved." in mine.get("/me/account?saved=true").text
    assert "Ann" in mine.get("/").text          # still signed in: the response set a new cookie
    assert "Ann" not in theirs.get("/").text    # signed out
    theirs_again = theirs.post("/signin", data={"display_name": "Ann", "next": "/"})
    assert 'type="password"' in theirs_again.text


def test_account_page_asks_for_the_current_password(client, db):
    sign_in(client, "Ann")
    client.post("/me/account", data={"new_password": PW, "confirm_password": PW})
    page = client.get("/me/account")
    assert "Change your password" in page.text and "current_password" in page.text

    resp = client.post("/me/account", data={"current_password": "nope", "new_password": "another one",
                                            "confirm_password": "another one"})
    assert resp.status_code == 400 and "Your current password isn't right." in body(resp)
    resp = client.post("/me/account", data={"current_password": PW, "new_password": "another one",
                                            "confirm_password": "another one!"})
    assert resp.status_code == 400 and "don't match" in body(resp)


# ------------------------------------------------------------ who may manage a club


def management_requests(club_id, event_id):
    return [
        ("GET", f"/clubs/{club_id}/edit", None),
        ("POST", f"/clubs/{club_id}/edit", {"name": "Taken Over"}),
        ("GET", f"/clubs/{club_id}/events/new", None),
        ("POST", f"/clubs/{club_id}/events/new", {"title": "Spam", "location": "x",
                                                  "starts_at": "2031-01-01T10:00", "capacity": "5"}),
        ("GET", f"/events/{event_id}/edit", None),
        ("POST", f"/events/{event_id}/edit", {"title": "Spam", "location": "x",
                                              "starts_at": "2031-01-01T10:00", "capacity": "5"}),
        ("GET", f"/clubs/{club_id}/organisers", None),
        ("POST", f"/clubs/{club_id}/organisers", {"display_name": "Mallory"}),
    ]


def test_members_cannot_manage_a_club(client, db, club):
    event = make_event(db, club["id"])
    make_organiser(db, club["id"], "Ann")
    sign_in(client, "Mallory")

    assert "Edit club" not in client.get(f"/clubs/{club['id']}").text
    assert "Edit event" not in client.get(f"/events/{event['id']}").text
    for method, path, data in management_requests(club["id"], event["id"]):
        resp = client.request(method, path, data=data)
        assert resp.status_code == 403, (method, path)
        assert "Only this club's organisers can do that." in body(resp)
    assert db.execute("SELECT name FROM clubs").fetchone()["name"] == "Chess Club"
    assert db.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 1
    assert [p["display_name"] for p in organisers.club_organisers(db, club["id"])] == ["Ann"]


def test_organisers_of_one_club_cannot_manage_another(client, db, club):
    other = make_club(db, name="Go Club")
    make_organiser(db, other["id"], "Ann")
    sign_in(client, "Ann", PW)
    assert client.get(f"/clubs/{club['id']}/edit").status_code == 403
    assert client.get(f"/clubs/{other['id']}/edit").status_code == 200


def test_organisers_without_a_password_are_sent_to_set_one(client, db, club):
    event = make_event(db, club["id"])
    make_organiser(db, club["id"], "Ann", password=None)
    sign_in(client, "Ann")

    page = client.get(f"/clubs/{club['id']}")
    assert "Set a password" in page.text and "Edit club" not in page.text
    for method, path, data in management_requests(club["id"], event["id"]):
        resp = client.request(method, path, data=data, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/me/account?next="), path
    resp = client.post("/me/account", follow_redirects=False,
                       data={"new_password": PW, "confirm_password": PW, "next": f"/events/{event['id']}/edit"})
    assert resp.headers["location"] == f"/events/{event['id']}/edit"
    assert client.get(resp.headers["location"]).status_code == 200


def test_starting_a_club_needs_a_password(client, db):
    sign_in(client, "Ann")
    resp = client.post("/clubs/new", data={"name": "Sneaky Club"}, follow_redirects=False)
    assert resp.headers["location"] == "/me/account?next=/clubs/new"
    assert db.execute("SELECT count(*) AS n FROM clubs").fetchone()["n"] == 0


# ------------------------------------------------------------ the organisers page


def test_add_and_remove_organisers(make_client, db, club):
    make_organiser(db, club["id"], "Ann")
    make_user(db, "Bo")
    ann = make_client()
    sign_in(ann, "Ann", PW)

    page = ann.get(f"/clubs/{club['id']}/organisers")
    assert page.status_code == 200 and "Ann (you)" in text(page)
    assert "Step down" not in page.text  # the only organiser can't leave

    resp = ann.post(f"/clubs/{club['id']}/organisers", data={"display_name": "Nobody Yet"})
    assert resp.status_code == 400 and "Nobody called Nobody Yet has signed in yet." in body(resp)

    resp = ann.post(f"/clubs/{club['id']}/organisers", data={"display_name": "bo"}, follow_redirects=False)
    assert resp.headers["location"] == f"/clubs/{club['id']}/organisers"
    page = text(ann.get(f"/clubs/{club['id']}/organisers"))
    assert "Bo needs to set a password" in page and "by Ann" in page
    assert "Organised by Ann, Bo" in text(ann.get(f"/clubs/{club['id']}"))

    bo = make_client()
    sign_in(bo, "Bo")
    bo.post("/me/account", data={"new_password": PW, "confirm_password": PW})
    ann_id = db.execute("SELECT id FROM users WHERE display_name = 'Ann'").fetchone()["id"]
    resp = bo.post(f"/clubs/{club['id']}/organisers/{ann_id}/remove", follow_redirects=False)
    assert resp.headers["location"] == f"/clubs/{club['id']}/organisers"
    assert ann.get(f"/clubs/{club['id']}/edit").status_code == 403  # Ann has lost access

    bo_id = db.execute("SELECT id FROM users WHERE display_name = 'Bo'").fetchone()["id"]
    resp = bo.post(f"/clubs/{club['id']}/organisers/{bo_id}/remove")
    assert resp.status_code == 409 and "needs at least one organiser" in resp.text


def test_stepping_down_goes_back_to_the_club(client, db, club):
    make_organiser(db, club["id"], "Ann")
    make_organiser(db, club["id"], "Bo")
    sign_in(client, "Ann", PW)
    ann_id = db.execute("SELECT id FROM users WHERE display_name = 'Ann'").fetchone()["id"]
    resp = client.post(f"/clubs/{club['id']}/organisers/{ann_id}/remove", follow_redirects=False)
    assert resp.headers["location"] == f"/clubs/{club['id']}"
    assert [p["display_name"] for p in organisers.club_organisers(db, club["id"])] == ["Bo"]


def test_two_organisers_removing_each_other_at_once_leave_one(db, database_url, club):
    """Each removal checks that someone is left. Without the club lock, both
    could see the other still there and the club would end up with nobody."""
    a = make_organiser(db, club["id"], "Ann")
    b = make_organiser(db, club["id"], "Bo")
    start = threading.Barrier(2)
    results = []

    def remove(user_id):
        with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
            start.wait()
            try:
                organisers.remove_organiser(conn, club["id"], user_id)
                results.append("removed")
            except organisers.LastOrganiser:
                results.append("refused")

    for _ in range(10):
        db.execute("TRUNCATE club_organisers")
        db.execute("INSERT INTO club_organisers (club_id, user_id) VALUES (%s, %s), (%s, %s)",
                   (club["id"], a["id"], club["id"], b["id"]))
        results.clear()
        threads = [threading.Thread(target=remove, args=(u["id"],)) for u in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert sorted(results) == ["refused", "removed"]
        assert len(organisers.club_organisers(db, club["id"])) == 1


def test_add_organiser_rules(db, club):
    ann = make_user(db, "Ann")
    organisers.add_organiser(db, club["id"], "ann")
    organisers.add_organiser(db, club["id"], "Ann")  # twice is harmless
    assert organisers.is_organiser(db, club["id"], ann["id"])
    assert not organisers.is_organiser(db, club["id"], None)
    with pytest.raises(rsvps.UserNotFound):
        organisers.add_organiser(db, club["id"], "Ghost")
    with pytest.raises(ClubNotFound):
        organisers.add_organiser(db, 999, "Ann")
    assert organisers.add_organiser(db, club["id"], "Ghost", create_user=True)["display_name"] == "Ghost"


# ------------------------------------------------------------ command line and seed


def test_organisers_command_line(db, club, monkeypatch, capsys, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    assert organisers.main(["add", str(club["id"]), "Kai Ito"]) == 0
    out = capsys.readouterr().out
    assert "Kai Ito now organises club" in out and "set a password" in out

    assert organisers.main(["list"]) == 0
    assert "Chess Club: Kai Ito (no password yet)" in capsys.readouterr().out

    assert organisers.main(["remove", str(club["id"]), "kai ito"]) == 0  # may remove the last one
    assert organisers.club_organisers(db, club["id"]) == []
    assert organisers.main(["add", "999", "Kai Ito"]) == 1
    assert "there's no club 999" in capsys.readouterr().err


def test_seeded_clubs_each_have_an_organiser_who_can_sign_in(client, db):
    seed(db, now=db.execute("SELECT now() AS t").fetchone()["t"])
    rows = db.execute(
        """SELECT c.id, u.display_name FROM clubs c
             JOIN club_organisers o ON o.club_id = c.id JOIN users u ON u.id = o.user_id ORDER BY c.id"""
    ).fetchall()
    assert len(rows) == 5 and len({r["display_name"] for r in rows}) == 5
    sign_in(client, rows[0]["display_name"], SEED_ORGANISER_PASSWORD)
    assert client.get(f"/clubs/{rows[0]['id']}/edit").status_code == 200
