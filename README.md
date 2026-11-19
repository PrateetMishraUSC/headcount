# Headcount

A small web app for university clubs to run events with limited seats and a waitlist.

- Sign in by picking a display name (a cookie; no passwords).
- Clubs have events with a title, description, location, start time and a seat capacity.
- RSVP to an event: if a seat is free you're **confirmed**; if not, you join the **waitlist** and see your position.
- Cancel a confirmed RSVP or leave the waitlist at any time before the event starts.
- A background **worker** fills freed seats: every second it promotes the earliest person on the waitlist and sends them a "You got a spot at …" notification.
- Pages for clubs, events (with a capacity bar, attendee list and numbered waitlist), "Your RSVPs" and a notifications inbox, plus a JSON API for scripts.

Built with Python 3.12, FastAPI + Jinja2 (server-rendered, no frontend build), PostgreSQL via psycopg 3 (no ORM) and plain CSS.

## Quick start

You need Python 3.12 and PostgreSQL (17 is what CI uses; anything recent works).

```bash
# 1. Databases
createdb headcount_dev
createdb headcount_test

# 2. Python environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt      # requirements.txt alone for production

# 3. Configuration
cp .env.example .env                     # edit if your Postgres isn't on localhost:5432

# 4. Schema and sample data
python -m headcount.migrate
python -m headcount.seed

# 5. Run it (two terminals)
uvicorn headcount.web:app --reload --no-access-log
python -m headcount.worker
```

Open <http://localhost:8000>, sign in as one of the seeded people (the seed prints a few names, such as "Kai Ito") or any new name, and RSVP. To see the waitlist move, RSVP to a full event, then sign in as a confirmed attendee in another browser and cancel: within a second the worker gives the first person on the waitlist the seat and a notification.

## Running each part

| What | Command | Notes |
|---|---|---|
| Web app | `uvicorn headcount.web:app --no-access-log` | Add `--reload` while developing. `--no-access-log` because the app logs its own JSON line per request. |
| Worker | `python -m headcount.worker` | Runs until Ctrl-C / SIGTERM. `--once` does a single poll. Safe to run several at once. |
| Migrations | `python -m headcount.migrate` | Applies new files from `migrations/` in order. `--list` shows applied/pending. Re-running is a no-op. |
| Seed data | `python -m headcount.seed` | 5 clubs, 200 people, 25 events (some full with waitlists), times relative to now. Refuses a non-empty database; `--reset` wipes and reloads. |
| Nightly report | `python -m headcount.reports` | Per club: confirmed / waitlisted / cancelled for every event so far. `--format csv`, `--all` (include upcoming). |
| Traffic simulator | `python scripts/traffic.py --rate 20 --duration 60` | Hits the JSON API with ~85% reads, 10% RSVPs, 5% cancels. Needs the web app running and seed data. |
| Tests | `python -m pytest` | Uses the real database at `TEST_DATABASE_URL`, which it wipes. |

## Configuration

Settings come from environment variables; a `.env` file in the project root fills in anything not set in the environment (see [.env.example](.env.example)). `.env` is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | *(required)* | e.g. `postgresql://localhost:5432/headcount_dev` |
| `TEST_DATABASE_URL` | *(required for tests)* | e.g. `postgresql://localhost:5432/headcount_test`. Must differ from `DATABASE_URL`. |
| `STATEMENT_TIMEOUT_MS` | `5000` | Per web request (and per worker query). Slower queries are cancelled and the request returns 504. |
| `DB_CONNECT_TIMEOUT_S` | `5` | Give up connecting to Postgres after this long (the request returns 503). |
| `WORKER_POLL_INTERVAL_S` | `1` | How often the worker looks for free seats. |
| `WORKER_RECONNECT_EVERY` | `360` | The worker replaces its database connection every N polls (survives failover). |
| `DISPLAY_TIMEZONE` | `America/Los_Angeles` | Time zone pages and reports show event times in. |

## JSON API

There are no passwords in Headcount, so API calls name the person by `user_id`. Interactive docs: <http://localhost:8000/api/docs>.

| Method and path | Returns |
|---|---|
| `GET /api/clubs/{id}` | The club and its events, each with `capacity`, `confirmed`, `waitlisted`, `seats_left`, `is_past` |
| `GET /api/events/{id}` | Event details, its club, `attendees` and the `waitlist` (with `position`) |
| `POST /api/events/{id}/rsvp` | Body `{"user_id": 1}`. The RSVP with `status` (`confirmed`/`waitlisted`) and `position`. **201** new, **200** already had one. |
| `POST /api/events/{id}/cancel` | Body `{"user_id": 1}`. The cancelled RSVP. 404 if there was no live RSVP. |
| `GET /api/users/{id}/rsvps` | All of the user's RSVPs with status, waitlist position and event |
| `GET /api/users/{id}/notifications` | The user's latest 100 notifications |
| `GET /health` | `{"status": "ok"}` if this server can reach the database, otherwise **503** |

Errors are JSON `{"detail": "..."}`: 404 unknown club/event/user, 409 the event has already started, 422 bad request body, 503 database unreachable, 504 database too slow.

```bash
curl -s localhost:8000/api/events/1 | python -m json.tool
curl -s -X POST localhost:8000/api/events/1/rsvp -H 'content-type: application/json' -d '{"user_id": 1}'
```

## How seats stay correct

- **No overbooking.** Anything that can confirm someone (an RSVP, or a promotion by the worker) first locks the event row, then counts seats in a *separate* statement, so the count includes everything committed before the lock was granted. Two people racing for the last seat are served one after the other.
- **No queue jumping.** If anyone is waiting, a new RSVP joins the waitlist even when a seat has just been freed; that seat belongs to the head of the queue, and the worker hands it over within a second.
- **Cancelling never promotes.** It only marks the RSVP cancelled; promotion is always the worker's job.
- **Several workers are safe.** The worker claims an event with `SELECT … FOR UPDATE SKIP LOCKED`, so two workers never work on the same event at the same time, and the database allows only one notification per promotion.
- **History is kept.** Cancelled RSVPs stay in the table (for the report); RSVPing again puts you at the back of the queue.

The tests check all of this with real concurrent connections.

## Project layout

```
headcount/
  config.py      settings from the environment / .env
  db.py          opening psycopg connections (dict rows, timeouts)
  migrate.py     python -m headcount.migrate
  rsvps.py       users, RSVP / cancel, waitlist positions (the seat rules live here)
  worker.py      python -m headcount.worker
  queries.py     read queries for clubs, events, notifications
  web.py         FastAPI app: request logging, 503/504 handling, /health
  deps.py        per-request database connection
  api.py         JSON API
  pages.py       HTML pages and name sign-in
  templates/     Jinja2 templates
  static/        style.css
  seed.py        python -m headcount.seed
  reports.py     python -m headcount.reports
migrations/      0001_initial_schema.sql, ...
scripts/         traffic.py
tests/           pytest suite (runs against TEST_DATABASE_URL)
docs/RUNBOOK.md  deploys, scheduled jobs, troubleshooting
```

## Development notes

- **Adding a migration:** create `migrations/0002_short_name.sql`. Each file runs in its own transaction and is recorded in `schema_migrations`; never edit a migration that has been applied anywhere. Migrations must keep working with the previous version of the code, because web servers restart one at a time after migrating (see the [runbook](docs/RUNBOOK.md)).
- **Logs:** the web app prints one JSON line per request (`timestamp`, `method`, `path`, `status`, `duration_ms`); the worker prints timestamped lines for start, each promotion, each reconnect and stop.
- **CI:** [.github/workflows/tests.yml](.github/workflows/tests.yml) runs the tests against a `postgres:17` service container on every push to `main` and every pull request, then smoke-tests the command-line tools.
