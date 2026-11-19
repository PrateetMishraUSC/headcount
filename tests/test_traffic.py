"""The traffic simulator, driven against the real app through TestClient."""

import importlib.util
import io
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from headcount import rsvps
from tests.factories import make_club, make_event, make_users

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "traffic.py"
spec = importlib.util.spec_from_file_location("traffic", SCRIPT)
traffic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(traffic)


class AppClientAdapter:
    """Gives TestClient the same interface as traffic.HttpClient."""

    def __init__(self, client):
        self.client = client
        self.paths = []

    def request(self, method, path, body=None):
        self.paths.append((method, path))
        resp = self.client.request(method, path, json=body)
        return resp.status_code, resp.json() if resp.content else None


@pytest.fixture
def world(db):
    clubs = [make_club(db, name=f"Club {i}") for i in range(2)]
    events = [make_event(db, c["id"], capacity=3, title=f"E{c['id']}-{j}") for c in clubs for j in range(3)]
    make_event(db, clubs[0]["id"], title="Over", starts_in=timedelta(days=-1))
    users = make_users(db, *[f"User {i}" for i in range(20)])
    rsvps.rsvp(db, events[0]["id"], users[0]["id"])
    return clubs, events, users


def test_discovers_clubs_and_upcoming_events_only(client, world):
    clubs, events, _ = world
    sim = traffic.Simulator(AppClientAdapter(client), max_user_id=20, rng=random.Random(1))
    sim.discover()
    assert sim.club_ids == [c["id"] for c in clubs]
    assert sorted(sim.event_ids) == sorted(e["id"] for e in events)  # not the past one


def test_action_mix_is_mostly_reads(client, world):
    sim = traffic.Simulator(AppClientAdapter(client), rng=random.Random(7))
    picks = Counter(sim.pick_action() for _ in range(20_000))
    reads = sum(picks[a] for a in ("view_event", "view_club", "view_user_rsvps", "view_notifications"))
    assert 0.83 < reads / 20_000 < 0.87
    assert 0.08 < picks["rsvp"] / 20_000 < 0.12
    assert 0.03 < picks["cancel"] / 20_000 < 0.07


def test_steps_hit_the_api_without_errors(client, db, world):
    adapter = AppClientAdapter(client)
    sim = traffic.Simulator(adapter, max_user_id=20, rng=random.Random(3))
    sim.discover()

    for _ in range(300):
        sim.step()

    s = sim.stats
    assert sum(s.count.values()) == 300
    assert s.failures == 0 and s.unexpected == 0
    assert s.count["rsvp"] > 0 and s.count["cancel"] > 0
    posted = {p.rsplit("/", 1)[-1] for m, p in adapter.paths if m == "POST"}
    assert posted == {"rsvp", "cancel"}
    # The simulated traffic left the data consistent: no event overbooked.
    over = db.execute(
        """SELECT e.id FROM events e
            WHERE (SELECT count(*) FROM rsvps r WHERE r.event_id = e.id AND r.status = 'confirmed') > e.capacity"""
    ).fetchall()
    assert over == []


def test_run_paces_requests_and_reports(client, world):
    sim = traffic.Simulator(AppClientAdapter(client), max_user_id=20, rng=random.Random(5))
    sim.discover()
    out = io.StringIO()

    stats = sim.run(rate=40, duration=1.0, concurrency=1, report_every=0.5, out=out)

    sent = sum(stats.count.values()) + stats.skipped
    assert 30 <= sent <= 45  # ~40 scheduled in one second
    assert stats.failures == 0
    lines = out.getvalue().splitlines()
    assert lines[-2].startswith("done:") and "p95" in lines[-2]
    assert lines[-1].startswith("by action: view_event")


def test_discover_explains_an_empty_database(client, db):
    sim = traffic.Simulator(AppClientAdapter(client))
    with pytest.raises(RuntimeError, match="headcount.seed"):
        sim.discover()


def test_http_client_reports_connection_errors_as_status_0():
    status, body = traffic.HttpClient("http://127.0.0.1:1", timeout=1).request("GET", "/health")
    assert (status, body) == (0, None)
