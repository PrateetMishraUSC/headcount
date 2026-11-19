"""Load deterministic sample data.

    python -m headcount.seed           # into an empty database
    python -m headcount.seed --reset   # wipe clubs/events/users/RSVPs first

Every run produces the same clubs, the same ~200 people and the same RSVP
pattern (a fixed random seed). Event times are relative to *now*: one event
per club in the next few hours, the rest on evenings over the coming four
weeks, never in the past.

RSVPs go through the real RSVP code, a few are then cancelled, and the
worker's promotion step runs once, so the data obeys the same rules as live
traffic (no overbooking, nobody waiting while a seat is free) and some people
already have "You got a spot" notifications.
"""

import argparse
import itertools
import random
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from headcount import rsvps
from headcount.config import get_settings
from headcount.db import connect
from headcount.worker import MAX_EVENTS_PER_POLL, promote_one_event

RANDOM_SEED = 20261118
NUM_USERS = 200
CANCEL_RATE = 0.12
APP_TABLES = "notifications, rsvps, events, clubs, users"

FIRST_NAMES = [
    "Aaliyah", "Ben", "Camila", "Daniel", "Elena", "Farah", "Gabriel", "Hana", "Isaac", "Jasmine",
    "Kai", "Lena", "Mateo", "Nora", "Omar", "Priya", "Quinn", "Rosa", "Sam", "Tariq",
    "Uma", "Victor", "Wen", "Ximena", "Yusuf", "Zoe", "Arjun", "Bea", "Chen", "Diego",
]
LAST_NAMES = [
    "Adams", "Bose", "Castillo", "Duarte", "Evans", "Fischer", "Garcia", "Hughes", "Ito", "Jensen",
    "Khan", "Lopez", "Mehta", "Nguyen", "Okafor", "Park", "Reyes", "Singh", "Tanaka", "Walsh",
]

# (club name, description, [(title, location, description, capacity, demand)])
# demand = RSVP attempts as a multiple of capacity: >1 means a waitlist.
CLUBS = [
    ("Trojan Chess Club", "Casual and rated games every week. All levels welcome.", [
        ("Blitz Night", "Student Union 204", "Five-minute games, clocks provided.", 24, 1.6),
        ("Beginners' Lesson", "Leavey Library B12", "Rules, openings and your first tournament game.", 12, 0.6),
        ("Simul vs. Coach", "Student Union 204", "Our coach plays everyone at once.", 20, 2.0),
        ("Rated Swiss Tournament", "Ronald Tutor Hall 150", "Four rounds, USCF rated. Bring ID.", 40, 0.9),
        ("Puzzle Rush Social", "Seeds Marketplace", "Puzzles, snacks, no pressure.", 30, 0.4),
    ]),
    ("Hiking Society", "Weekend trail trips around Los Angeles. Carpools from campus.", [
        ("Griffith Observatory Sunset Hike", "Meet at Tommy Trojan", "Easy 4 miles, headlamps advised.", 25, 1.8),
        ("Eaton Canyon Falls", "Meet at Parking Structure X", "Moderate, some stream crossings.", 15, 1.3),
        ("Mt. Baldy Summit", "Meet at Parking Structure X", "Strenuous, 11 miles. Experience required.", 10, 1.5),
        ("Trail Maintenance Day", "Meet at Tommy Trojan", "Help the Forest Service; gloves provided.", 20, 0.5),
        ("Gear Swap", "Student Union Ballroom", "Bring what you don't use, take what you need.", 60, 0.7),
    ]),
    ("Robotics Club", "We build robots for competitions and for fun.", [
        ("Soldering Workshop", "Seaver Science Lab 112", "Learn to solder; kits included.", 16, 1.9),
        ("Intro to ROS", "Ronald Tutor Hall 210", "Robot Operating System from zero.", 30, 1.1),
        ("Competition Build Night", "Fabrication Lab", "All hands for the spring competition.", 18, 0.8),
        ("Industry Talk: Warehouse Robots", "Bovard Auditorium", "Engineers from a logistics robotics team.", 120, 0.6),
        ("Drone Racing", "Cromwell Field", "Bring your own quad or borrow one.", 12, 1.4),
    ]),
    ("Film Society", "Screenings, discussions and the occasional student short.", [
        ("Midnight Classic Screening", "Norris Cinema Theatre", "A cult favourite on the big screen.", 100, 0.8),
        ("Director Q&A", "Norris Cinema Theatre", "Screening followed by a conversation.", 80, 1.5),
        ("Short Film Workshop", "SCA 112", "Write, shoot and cut a 3-minute film.", 14, 1.7),
        ("Film Trivia Night", "Ground Zero Café", "Teams of four, prizes for the top three.", 40, 1.0),
        ("Animation Showcase", "SCA 108", "Student animation from this year.", 60, 0.3),
    ]),
    ("Culinary Club", "Cooking classes, potlucks and food crawls.", [
        ("Dumpling Workshop", "Parkside Kitchen", "Fold three kinds of dumplings.", 16, 2.0),
        ("Knife Skills 101", "Parkside Kitchen", "Hold, chop and sharpen like a pro.", 12, 1.2),
        ("Thanksgiving Potluck", "Student Union Ballroom", "Bring a dish to share.", 80, 0.9),
        ("Koreatown Food Crawl", "Meet at Expo/Vermont station", "Five stops, small plates.", 20, 1.6),
        ("Pasta From Scratch", "Parkside Kitchen", "Eggs, flour, and patience.", 14, 1.0),
    ]),
]

# Evening start times (local) for events after the first one in each club.
EVENING_HOURS = [17, 18, 18, 19, 19, 20]


class DatabaseNotEmpty(Exception):
    pass


def make_names(rng: random.Random, count: int) -> list[str]:
    combos = [f"{first} {last}" for first, last in itertools.product(FIRST_NAMES, LAST_NAMES)]
    return rng.sample(combos, count)


def event_times(rng: random.Random, now: datetime, tz: ZoneInfo, club_index: int, count: int) -> list[datetime]:
    """Start times for one club's events, all in the future.

    The first is a few hours from now if that's still before 10 pm local
    time, otherwise the next morning; the rest are on evenings over the next
    four weeks.
    """
    soon = (now + timedelta(hours=2 + club_index)).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    if soon.date() != now.astimezone(tz).date() or soon.hour >= 22 or soon.hour < 9:
        morning = now.astimezone(tz).date() + timedelta(days=1 if now.astimezone(tz).hour >= 9 else 0)
        soon = datetime.combine(morning, time(9 + 2 * club_index), tzinfo=tz)
    times = [soon]
    today = now.astimezone(tz).date()
    for _ in range(count - 1):
        day = today + timedelta(days=rng.randint(1, 27))
        start = datetime.combine(day, time(rng.choice(EVENING_HOURS)), tzinfo=tz)
        times.append(start)
    return times


def seed(conn: psycopg.Connection, *, now: datetime | None = None, tz: str = "America/Los_Angeles",
         reset: bool = False, num_users: int = NUM_USERS) -> dict:
    """Load the sample data. Returns counts of what was created."""
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(tz)
    rng = random.Random(RANDOM_SEED)

    has_data = conn.execute("SELECT EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM clubs) AS x").fetchone()["x"]
    if has_data and not reset:
        raise DatabaseNotEmpty("the database already has data; rerun with --reset to replace it")
    if reset:
        conn.execute(f"TRUNCATE {APP_TABLES} RESTART IDENTITY CASCADE")

    users = [rsvps.find_or_create_user(conn, name) for name in make_names(rng, num_users)]
    user_ids = [u["id"] for u in users]

    event_count = 0
    for club_index, (club_name, club_description, events) in enumerate(CLUBS):
        club_id = conn.execute(
            "INSERT INTO clubs (name, description) VALUES (%s, %s) RETURNING id",
            (club_name, club_description),
        ).fetchone()["id"]
        for (title, location, description, capacity, demand), starts_at in zip(
            events, event_times(rng, now, zone, club_index, len(events))
        ):
            event_id = conn.execute(
                """INSERT INTO events (club_id, title, description, location, starts_at, capacity)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (club_id, title, description, location, starts_at, capacity),
            ).fetchone()["id"]
            event_count += 1
            attendees = rng.sample(user_ids, min(round(capacity * demand), len(user_ids)))
            for user_id in attendees:
                rsvps.rsvp(conn, event_id, user_id)
            for user_id in attendees:
                if rng.random() < CANCEL_RATE:
                    rsvps.cancel(conn, event_id, user_id)

    # Let freed seats go to the waitlist, exactly as the worker would.
    handled: list[int] = []
    while len(handled) < MAX_EVENTS_PER_POLL and (result := promote_one_event(conn, handled)):
        handled.append(result[0]["id"])

    return conn.execute(
        """SELECT (SELECT count(*) FROM clubs) AS clubs,
                  (SELECT count(*) FROM users) AS users,
                  (SELECT count(*) FROM events) AS events,
                  (SELECT count(*) FROM rsvps WHERE status = 'confirmed') AS confirmed,
                  (SELECT count(*) FROM rsvps WHERE status = 'waitlisted') AS waitlisted,
                  (SELECT count(*) FROM rsvps WHERE status = 'cancelled') AS cancelled,
                  (SELECT count(*) FROM notifications) AS notifications"""
    ).fetchone()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete existing clubs, events, users and RSVPs first")
    args = parser.parse_args(argv)
    settings = get_settings()
    with connect(settings.database_url, autocommit=True, application_name="headcount-seed") as conn:
        try:
            counts = seed(conn, tz=settings.display_timezone, reset=args.reset)
        except DatabaseNotEmpty as exc:
            print(f"seed: {exc}", file=sys.stderr)
            return 1
        sample = [r["display_name"] for r in conn.execute("SELECT display_name FROM users ORDER BY id LIMIT 3")]
    print(
        "seeded {clubs} clubs, {users} users, {events} events; "
        "RSVPs: {confirmed} confirmed, {waitlisted} waitlisted, {cancelled} cancelled; "
        "{notifications} notifications".format(**counts)
    )
    print(f"sign in as e.g. {', '.join(sample)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
