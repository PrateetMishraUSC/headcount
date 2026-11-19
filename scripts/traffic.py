#!/usr/bin/env python3
"""Simulate realistic traffic against the Headcount JSON API, for local testing.

    python scripts/traffic.py                          # 5 req/s for 60 s
    python scripts/traffic.py --rate 50 --duration 0   # 50 req/s until Ctrl-C
    python scripts/traffic.py --base-url http://localhost:8000 --concurrency 8

Mix (roughly): 85% reads (event pages, club pages, people's RSVPs and
notifications), 10% RSVPs and 5% cancellations. A few popular events get
most of the traffic, so RSVPs really do race for the last seats. Run the
worker alongside to watch waitlists drain as people cancel.

It finds clubs by asking for /api/clubs/1, /2, ... until one is missing, and
assumes users 1..--max-user-id exist (true after `python -m headcount.seed`).

Standard library only. Prints a summary every few seconds and at the end;
exits 1 if any request failed with a 5xx or a connection error.
"""

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

# action -> weight
MIX = {
    "view_event": 50,
    "view_club": 15,
    "view_user_rsvps": 10,
    "view_notifications": 10,
    "rsvp": 10,
    "cancel": 5,
}
EXPECTED = {  # statuses that are normal for each action (anything else is "unexpected")
    "view_event": {200},
    "view_club": {200},
    "view_user_rsvps": {200},
    "view_notifications": {200},
    "rsvp": {200, 201, 409},       # 409: the event just started
    "cancel": {200, 404, 409},     # 404: someone (or the user) already cancelled it
}


class HttpClient:
    """Tiny JSON-over-HTTP client. Returns (status, body or None); status 0 = no response."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return 0, None


def _json(raw: bytes) -> dict | None:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.count = Counter()
        self.statuses: dict[str, Counter] = defaultdict(Counter)
        self.latencies: list[float] = []
        self.failures = 0     # 5xx or no response
        self.unexpected = 0   # 4xx we didn't expect
        self.skipped = 0      # client was too busy to send on schedule

    def record(self, action: str, status: int, seconds: float) -> None:
        with self.lock:
            self.count[action] += 1
            self.statuses[action][status] += 1
            self.latencies.append(seconds)
            if status == 0 or status >= 500:
                self.failures += 1
            elif status not in EXPECTED[action]:
                self.unexpected += 1

    def summary(self, elapsed: float) -> str:
        with self.lock:
            total = sum(self.count.values())
            lat = sorted(self.latencies)

        def pct(p):
            return lat[min(len(lat) - 1, int(p * len(lat)))] * 1000 if lat else 0.0

        codes = Counter()
        for c in self.statuses.values():
            codes.update(c)
        code_text = " ".join(f"{k if k else 'conn-error'}:{v}" for k, v in sorted(codes.items()))
        return (
            f"{elapsed:6.1f}s  {total} requests ({total / elapsed if elapsed else 0:.1f}/s)  "
            f"p50 {pct(0.50):.0f}ms  p95 {pct(0.95):.0f}ms  max {pct(1.0):.0f}ms  "
            f"[{code_text}]  failures {self.failures}  unexpected {self.unexpected}"
            + (f"  skipped {self.skipped}" if self.skipped else "")
        )


class Simulator:
    def __init__(self, client, *, max_user_id: int = 200, rng: random.Random | None = None,
                 stats: Stats | None = None):
        self.client = client
        self.max_user_id = max_user_id
        self.rng = rng or random.Random()
        self.stats = stats or Stats()
        self.club_ids: list[int] = []
        self.event_ids: list[int] = []
        self.event_weights: list[float] = []
        self.live: set[tuple[int, int]] = set()  # (event_id, user_id) with a live RSVP, as far as we know
        self.lock = threading.Lock()

    # -- setup

    def discover(self, max_clubs: int = 1000) -> None:
        """Find clubs (1, 2, ... until a 404) and their upcoming events."""
        for club_id in range(1, max_clubs + 1):
            status, body = self.client.request("GET", f"/api/clubs/{club_id}")
            if status == 404:
                break
            if status != 200 or body is None:
                raise RuntimeError(f"GET /api/clubs/{club_id} returned {status}; is the server running?")
            self.club_ids.append(club_id)
            self.event_ids += [e["id"] for e in body["events"] if not e["is_past"]]
        if not self.event_ids:
            raise RuntimeError("no upcoming events found; run `python -m headcount.seed` first")
        # A few events are much more popular than the rest (roughly Zipf).
        order = self.event_ids[:]
        self.rng.shuffle(order)
        rank = {event_id: i + 1 for i, event_id in enumerate(order)}
        self.event_weights = [1 / rank[e] for e in self.event_ids]

    # -- choosing what to do

    def pick_event(self) -> int:
        return self.rng.choices(self.event_ids, weights=self.event_weights)[0]

    def pick_user(self) -> int:
        return self.rng.randint(1, self.max_user_id)

    def pick_action(self) -> str:
        return self.rng.choices(list(MIX), weights=list(MIX.values()))[0]

    # -- doing it

    def step(self) -> tuple[str, int]:
        action = self.pick_action()
        target = None
        if action == "cancel":
            with self.lock:
                target = self.rng.choice(sorted(self.live)) if self.live else None
            if target is None:
                action = "rsvp"  # nothing known to cancel yet
        started = time.perf_counter()
        status = self._do(action, target)
        self.stats.record(action, status, time.perf_counter() - started)
        return action, status

    def _do(self, action: str, target: tuple[int, int] | None) -> int:
        c = self.client
        if action == "view_event":
            return c.request("GET", f"/api/events/{self.pick_event()}")[0]
        if action == "view_club":
            return c.request("GET", f"/api/clubs/{self.rng.choice(self.club_ids)}")[0]
        if action == "view_notifications":
            return c.request("GET", f"/api/users/{self.pick_user()}/notifications")[0]
        if action == "view_user_rsvps":
            user_id = self.pick_user()
            status, body = c.request("GET", f"/api/users/{user_id}/rsvps")
            if status == 200 and body:
                live = {(r["event_id"], user_id) for r in body["rsvps"]
                        if r["status"] != "cancelled" and r["event_id"] in self.event_ids}
                with self.lock:
                    self.live |= live
            return status
        if action == "rsvp":
            event_id, user_id = self.pick_event(), self.pick_user()
            status, _ = c.request("POST", f"/api/events/{event_id}/rsvp", {"user_id": user_id})
            if status in (200, 201):
                with self.lock:
                    self.live.add((event_id, user_id))
            return status
        if action == "cancel":
            event_id, user_id = target
            status, _ = c.request("POST", f"/api/events/{event_id}/cancel", {"user_id": user_id})
            with self.lock:
                self.live.discard(target)
            return status
        raise ValueError(action)

    # -- pacing

    def run(self, *, rate: float, duration: float, concurrency: int, report_every: float = 5.0,
            out=sys.stdout) -> Stats:
        """Send `rate` requests per second for `duration` seconds (0 = until Ctrl-C)."""
        interval = 1.0 / rate
        slots = threading.BoundedSemaphore(concurrency)
        start = time.monotonic()
        next_send = start
        next_report = start + report_every

        def task():
            try:
                self.step()
            finally:
                slots.release()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            try:
                while duration <= 0 or time.monotonic() - start < duration:
                    now = time.monotonic()
                    if now < next_send:
                        time.sleep(min(next_send - now, 0.05))
                        continue
                    next_send += interval
                    if slots.acquire(blocking=False):
                        pool.submit(task)
                    else:
                        self.stats.skipped += 1  # server too slow for this rate/concurrency
                    if report_every and now >= next_report:
                        print(self.stats.summary(now - start), file=out, flush=True)
                        next_report += report_every
            except KeyboardInterrupt:
                print("\nstopping…", file=out)
        print("done: " + self.stats.summary(time.monotonic() - start), file=out, flush=True)
        with self.stats.lock:
            by_action = "  ".join(f"{a} {self.stats.count[a]}" for a in MIX if self.stats.count[a])
        print(f"by action: {by_action}", file=out, flush=True)
        return self.stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--rate", type=float, default=5.0, help="requests per second (default 5)")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds to run; 0 = until Ctrl-C (default 60)")
    parser.add_argument("--concurrency", type=int, default=4, help="max requests in flight (default 4)")
    parser.add_argument("--max-user-id", type=int, default=200, help="users 1..N are assumed to exist (default 200)")
    parser.add_argument("--seed", type=int, help="random seed, for a repeatable sequence of actions")
    args = parser.parse_args(argv)
    if args.rate <= 0 or args.concurrency < 1:
        parser.error("--rate must be > 0 and --concurrency >= 1")

    sim = Simulator(HttpClient(args.base_url), max_user_id=args.max_user_id, rng=random.Random(args.seed))
    try:
        sim.discover()
    except RuntimeError as exc:
        print(f"traffic: {exc}", file=sys.stderr)
        return 2
    print(f"found {len(sim.club_ids)} clubs and {len(sim.event_ids)} upcoming events; "
          f"sending {args.rate:g} req/s to {args.base_url}"
          + (f" for {args.duration:g}s" if args.duration > 0 else " until Ctrl-C"), flush=True)
    stats = sim.run(rate=args.rate, duration=args.duration, concurrency=args.concurrency)
    return 1 if stats.failures else 0


if __name__ == "__main__":
    sys.exit(main())
