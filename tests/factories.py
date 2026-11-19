"""Small helpers for inserting test data directly."""

from datetime import timedelta


def make_club(conn, name="Chess Club", description="We play chess."):
    return conn.execute(
        "INSERT INTO clubs (name, description) VALUES (%s, %s) RETURNING *",
        (name, description),
    ).fetchone()


def make_event(conn, club_id, *, capacity=2, title="Blitz Night", starts_in=timedelta(days=3)):
    return conn.execute(
        """
        INSERT INTO events (club_id, title, description, location, starts_at, capacity)
        VALUES (%s, %s, 'Bring a clock.', 'Student Union 204', now() + %s, %s)
        RETURNING *
        """,
        (club_id, title, starts_in, capacity),
    ).fetchone()


def make_user(conn, name):
    return conn.execute(
        "INSERT INTO users (display_name) VALUES (%s) RETURNING *", (name,)
    ).fetchone()


def make_users(conn, *names):
    return [make_user(conn, n) for n in names]


ORGANISER_PASSWORD = "correct horse battery"


def make_organiser(conn, club_id, name, *, password=ORGANISER_PASSWORD):
    """A user (created if needed) who organises the club; password=None leaves them without one."""
    from headcount import auth

    user = conn.execute("SELECT * FROM users WHERE display_name = %s", (name,)).fetchone() or make_user(conn, name)
    if password is not None:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (auth.hash_password(password), user["id"]))
    conn.execute("INSERT INTO club_organisers (club_id, user_id) VALUES (%s, %s)", (club_id, user["id"]))
    return user
