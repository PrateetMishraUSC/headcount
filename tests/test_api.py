import json
from datetime import timedelta

import psycopg
import pytest

from headcount import rsvps
from headcount.worker import Worker
from tests.factories import make_club, make_event, make_user, make_users


@pytest.fixture
def setup(db):
    club = make_club(db)
    event = make_event(db, club["id"], capacity=1, title="Blitz Night")
    ann, bo = make_users(db, "Ann", "Bo")
    return club, event, ann, bo


# ------------------------------------------------------------ reads


def test_get_club_lists_its_events_with_seat_counts(client, db, setup):
    club, event, ann, _ = setup
    past = make_event(db, club["id"], title="Last Week", starts_in=timedelta(days=-7))
    rsvps.rsvp(db, event["id"], ann["id"])

    resp = client.get(f"/api/clubs/{club['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert (body["id"], body["name"]) == (club["id"], "Chess Club")
    assert [(e["title"], e["is_past"]) for e in body["events"]] == [("Blitz Night", False), ("Last Week", True)]
    first = body["events"][0]
    assert {k: first[k] for k in ("capacity", "confirmed", "waitlisted", "seats_left")} == {
        "capacity": 1, "confirmed": 1, "waitlisted": 0, "seats_left": 0,
    }
    assert past["id"] == body["events"][1]["id"]


def test_get_event_shows_attendees_and_waitlist(client, db, setup):
    club, event, ann, bo = setup
    cy = make_user(db, "Cy")
    for u in (ann, bo, cy):
        rsvps.rsvp(db, event["id"], u["id"])

    body = client.get(f"/api/events/{event['id']}").json()

    assert body["title"] == "Blitz Night"
    assert body["club"] == {"id": club["id"], "name": "Chess Club"}
    assert body["location"] == "Student Union 204"
    assert (body["confirmed"], body["waitlisted"], body["seats_left"]) == (1, 2, 0)
    assert body["attendees"] == [{"user_id": ann["id"], "display_name": "Ann"}]
    assert body["waitlist"] == [
        {"position": 1, "user_id": bo["id"], "display_name": "Bo"},
        {"position": 2, "user_id": cy["id"], "display_name": "Cy"},
    ]


@pytest.mark.parametrize("path", ["/api/clubs/999", "/api/events/999", "/api/users/999/rsvps",
                                  "/api/users/999/notifications"])
def test_unknown_ids_are_404(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ------------------------------------------------------------ rsvp / cancel


def test_rsvp_confirms_then_waitlists(client, setup):
    _, event, ann, bo = setup

    a = client.post(f"/api/events/{event['id']}/rsvp", json={"user_id": ann["id"]})
    b = client.post(f"/api/events/{event['id']}/rsvp", json={"user_id": bo["id"]})
    again = client.post(f"/api/events/{event['id']}/rsvp", json={"user_id": bo["id"]})

    assert a.status_code == 201
    assert (a.json()["status"], a.json()["position"]) == ("confirmed", None)
    assert b.status_code == 201
    assert (b.json()["status"], b.json()["position"]) == ("waitlisted", 1)
    assert again.status_code == 200
    assert again.json()["id"] == b.json()["id"]


def test_cancel_then_worker_promotes_and_notifies(client, database_url, setup):
    _, event, ann, bo = setup
    client.post(f"/api/events/{event['id']}/rsvp", json={"user_id": ann["id"]})
    client.post(f"/api/events/{event['id']}/rsvp", json={"user_id": bo["id"]})

    resp = client.post(f"/api/events/{event['id']}/cancel", json={"user_id": ann["id"]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    # Cancelling alone doesn't promote anyone.
    assert client.get(f"/api/events/{event['id']}").json()["waitlisted"] == 1

    Worker(database_url, log=lambda _l: None).poll_once()

    mine = client.get(f"/api/users/{bo['id']}/rsvps").json()
    assert mine["user"] == {"id": bo["id"], "display_name": "Bo"}
    assert [(r["status"], r["event"]["title"]) for r in mine["rsvps"]] == [("confirmed", "Blitz Night")]
    notes = client.get(f"/api/users/{bo['id']}/notifications").json()["notifications"]
    assert [(n["message"], n["event_id"], n["read_at"]) for n in notes] == [
        ("You got a spot at Blitz Night!", event["id"], None)
    ]


def test_rsvp_and_cancel_errors(client, db, setup):
    club, event, ann, _ = setup
    started = make_event(db, club["id"], starts_in=timedelta(minutes=-5))
    url = f"/api/events/{event['id']}"

    assert client.post("/api/events/999/rsvp", json={"user_id": ann["id"]}).status_code == 404
    assert client.post(f"{url}/rsvp", json={"user_id": 999}).status_code == 404
    assert client.post(f"{url}/cancel", json={"user_id": ann["id"]}).status_code == 404
    assert client.post(f"/api/events/{started['id']}/rsvp", json={"user_id": ann["id"]}).status_code == 409
    assert client.post(f"{url}/rsvp", json={}).status_code == 422
    assert client.post(f"{url}/rsvp", json={"user_id": "ann"}).status_code == 422


# ------------------------------------------------------------ production touches


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_health_is_503_when_the_database_is_unreachable(db, make_client):
    down = make_client(database_url="postgresql://localhost:1/nowhere", connect_timeout_s=1)
    resp = down.get("/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unavailable"
    # Other API requests say so too, as JSON.
    resp = down.get("/api/events/1")
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"]


def test_slow_query_returns_504(db, database_url, make_client, setup):
    _, event, _, _ = setup
    client = make_client(statement_timeout_ms=200)
    with psycopg.connect(database_url) as blocker:
        # Make every query on events wait, so the request's query hits its timeout.
        blocker.execute("LOCK TABLE events IN ACCESS EXCLUSIVE MODE")
        resp = client.get(f"/api/events/{event['id']}")
        blocker.rollback()

    assert resp.status_code == 504
    assert resp.headers["content-type"].startswith("application/json")
    assert "too long" in resp.json()["detail"]
    # And the app is fine afterwards.
    assert client.get(f"/api/events/{event['id']}").status_code == 200


def test_one_json_log_line_per_request(client, capsys, setup):
    _, event, _, _ = setup
    capsys.readouterr()

    client.get(f"/api/events/{event['id']}?extra=1")
    client.get("/api/events/999")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(l["method"], l["path"], l["status"]) for l in lines] == [
        ("GET", f"/api/events/{event['id']}", 200),
        ("GET", "/api/events/999", 404),
    ]
    for line in lines:
        assert set(line) == {"timestamp", "method", "path", "status", "duration_ms"}
        assert line["timestamp"].endswith("Z")
        assert line["duration_ms"] >= 0


def test_each_request_gets_its_own_connection_with_the_timeout(client, setup, monkeypatch):
    from headcount import deps

    opened = []
    real_connect = deps.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append((conn, conn.info.backend_pid))
        return conn

    monkeypatch.setattr(deps, "connect", spy)
    _, event, _, _ = setup
    for _ in range(3):
        assert client.get(f"/api/events/{event['id']}").status_code == 200

    assert len(opened) == 3
    assert len({pid for _, pid in opened}) == 3
    assert all(conn.closed for conn, _ in opened)  # closed when the request ends
    # The timeout is set on the session itself.
    probe = real_connect(client.app.state.settings.database_url, statement_timeout_ms=5000)
    assert probe.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "5s"
    probe.close()
