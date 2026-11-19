# Headcount runbook

How Headcount runs in production, how to deploy it, which jobs run when, and what to do when something looks wrong.

## What runs where

| Component | How many | Runs as | Talks to |
|---|---|---|---|
| Web app (`uvicorn headcount.web:app`) | 2+ servers behind the load balancer | long-running service | Postgres: one new connection per request |
| Worker (`python -m headcount.worker`) | 1 (more is safe) | long-running service | Postgres: one connection, replaced every 360 polls |
| Nightly report (`python -m headcount.reports`) | 1 host | cron | Postgres: one connection per run |
| Migrations (`python -m headcount.migrate`) | once per deploy, from one host | by hand / deploy script | Postgres |
| Organiser admin (`python -m headcount.organisers`) | any host, by hand | when a club has no organiser | Postgres |

The web servers also need `SECRET_KEY` (at least 32 random characters), **identical on every web server**, or a person signed in on one server looks signed out on the next. The web app refuses to start without it. Changing it signs everyone out, which is also how to cut off every session at once if it ever leaks.

All of them read `DATABASE_URL` (and the optional settings in the README) from the environment or from `.env` in the project directory. Cron doesn't pass your shell's environment, but because the code reads `<project>/.env` itself, a `.env` next to the code is enough.

### Load balancer health check

Point it at `GET /health`. It returns `200 {"status": "ok"}` when that server can reach Postgres, and `503` when it can't (it gives up after 2 s). Suggested settings: check every 5 s, mark unhealthy after 2 failures, healthy again after 2 successes.

### Example service definitions

```ini
# /etc/systemd/system/headcount-web.service
[Service]
WorkingDirectory=/srv/headcount
ExecStart=/srv/headcount/.venv/bin/uvicorn headcount.web:app \
    --host 0.0.0.0 --port 8000 --workers 2 --no-access-log \
    --proxy-headers --forwarded-allow-ips=<load balancer address> \
    --timeout-graceful-shutdown 15
Restart=always

# /etc/systemd/system/headcount-worker.service
[Service]
WorkingDirectory=/srv/headcount
ExecStart=/srv/headcount/.venv/bin/python -m headcount.worker
Restart=always
KillSignal=SIGTERM
```

`--timeout-graceful-shutdown` lets requests already in progress finish when a web server is restarted. The worker stops cleanly on SIGTERM: it finishes the current poll, closes its connection and logs `stopped`.

## Deploying

A deploy is: **run migrations, then restart the web servers one at a time. The worker keeps running.**

1. **Get the new code onto every host** (web servers, the worker host and the cron host) and install dependencies:
   ```bash
   cd /srv/headcount && git fetch && git checkout <release>
   .venv/bin/pip install -r requirements.txt
   ```
   Running processes keep using the code they loaded, so nothing changes yet.

2. **Run migrations once**, from any one host:
   ```bash
   .venv/bin/python -m headcount.migrate --list   # what's pending
   .venv/bin/python -m headcount.migrate
   ```
   - Each migration runs in its own transaction and is recorded in `schema_migrations`. If one fails, it's rolled back completely, the command exits non-zero and later migrations are not attempted. Fix the problem and run the command again; it picks up where it stopped.
   - Two people running it at once is safe: the second waits for the first (a Postgres advisory lock).
   - If a migration needs a table lock and can't get it within 5 s (because of busy traffic), it gives up instead of blocking every request behind it. Run it again, ideally at a quieter time (`--lock-timeout 15s` to wait longer).
   - **The old code is still serving traffic while and after this runs**, so migrations must be backwards-compatible: add columns, tables and indexes freely, but don't rename or drop anything the running version uses. Do removals in a later deploy, once no running code needs the old thing.

3. **Restart the web servers one at a time:**
   ```bash
   sudo systemctl restart headcount-web
   curl -fsS localhost:8000/health        # wait for {"status": "ok"} before moving on
   ```
   Wait for the load balancer to show the server healthy again before restarting the next one, so there's always capacity serving requests. Each request opens its own database connection, so a restarted server needs no warm-up.

4. **Leave the worker running.** It doesn't need a restart for schema changes. If the release changed `headcount/worker.py` or `headcount/rsvps.py`, restart it afterwards: `sudo systemctl restart headcount-worker`. Seats freed during the few seconds it's down are filled as soon as it's back. If you want no gap at all, start a second worker before stopping the old one; two workers at once are safe.

5. **Check:** the request logs show 2xx responses, and the worker log shows `promoted …` lines when people cancel.

### Rolling back

Check out the previous release and restart the web servers one at a time (and the worker if its code changed). Migrations are **not** undone: because they're backwards-compatible, the previous code runs fine on the newer schema. If a schema change itself must be reverted, write a new migration that does it.

## Scheduled jobs

| Job | When | Command | Output |
|---|---|---|---|
| Worker | always | `python -m headcount.worker` (systemd) | stdout → journal |
| Attendance report | nightly, 02:00 | see crontab below | a dated text file; cron mails on failure |
| Seed data | **never in production** | `python -m headcount.seed` | (dev and staging only; refuses a non-empty database) |

```cron
# crontab for the deploy user on the cron host
MAILTO=club-admins@example.edu
0 2 * * * cd /srv/headcount && .venv/bin/python -m headcount.reports > /var/log/headcount/attendance-$(date +\%F).txt
```

The report covers every event that has started so far, per club, with confirmed, waitlisted and cancelled counts. Use `--format csv` for a spreadsheet. It exits non-zero on failure, so cron mails `MAILTO`. It gives up on queries that take longer than 2 minutes.

## Logs

- **Web:** one JSON line per request on stdout:
  `{"timestamp": "2026-11-19T07:22:18.085Z", "method": "POST", "path": "/api/events/7/rsvp", "status": 201, "duration_ms": 31.4}`
- **Worker:** timestamped lines on stdout:
  `2026-11-19T07:22:23.155Z worker[16403] promoted user 118 (Diego Singh) to confirmed for event 17 (Director Q&A)`
  Other lines: `started (…)`, `reconnecting to the database (every 360 polls)`, `database error, will reconnect: …`, `stopped`.
- **Postgres:** connections are labelled `headcount-web`, `headcount-worker`, `headcount-migrate`, `headcount-reports`, `headcount-admin` and `headcount-health`:
  ```sql
  SELECT application_name, state, now() - query_start AS running_for, query
    FROM pg_stat_activity WHERE application_name LIKE 'headcount%';
  ```

## When something looks wrong

**Waitlists aren't moving after people cancel.**
Is the worker running (`systemctl status headcount-worker`) and logging? With nothing to do it stays quiet; it only logs promotions. Look for a `headcount-worker` row in `pg_stat_activity`. Repeated `database error, will reconnect` lines mean it can't reach or query Postgres. Note that it only promotes people for events that haven't started yet.

**Lots of 504s.**
Queries are taking longer than `STATEMENT_TIMEOUT_MS` (5 s). Usually something is holding locks: a long migration, or a manual transaction left open in `psql`. Check `pg_stat_activity` for old `idle in transaction` sessions or long-running queries, and `pg_locks` for what's waiting.

**503s / health check failing on every server.**
The web servers can't reach Postgres: check the database itself, the network, and `DATABASE_URL`. Requests give up after 5 s of trying to connect.

**After a database failover.**
Web servers recover immediately, because every request opens a new connection. The worker recovers on its next poll if the failover dropped its connection. If its old connection somehow stays open, it moves to the new primary within 360 polls (about 6 minutes at the default 1 s interval). For a faster cut-over, restart it.

**A migration failed.**
Nothing from that migration was applied. Read the error: if it's `lock timeout`, rerun when traffic is lower or with `--lock-timeout 15s`. For anything else, fix the migration file (it was never recorded as applied, so editing it is fine) and rerun.

**A club has no organiser (or its only organiser has left).**
An administrator adds one from any host: `.venv/bin/python -m headcount.organisers add <club id> "Their Name"` (`list` shows every club's organisers). They then sign in and set a password on the Account page before they can manage the club.

**Someone forgot their password.**
There is no email reset. An administrator clears it in `psql`: `UPDATE users SET password_hash = NULL WHERE lower(display_name) = lower('Their Name');`. They can then sign in with just the name and set a new password straight away.

**Someone ran the seed against the wrong database.**
It refuses to touch a database that already has users or clubs unless `--reset` is passed. Never pass `--reset` with production credentials. (The seed's organisers share a published dev password, another reason it must never run in production.)
