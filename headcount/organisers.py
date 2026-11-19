"""Club organisers: who may edit a club, create and edit its events, and add
other organisers.

Whoever creates a club on the website becomes its first organiser. Organisers
add more by display name and can remove any organiser except the last one.
Organiser powers also need a password on the account (see auth.py); the pages
check that.

Clubs created before organisers existed (or by the seed) start with none. An
administrator adds the first one from the command line:

    python -m headcount.organisers list               # every club and its organisers
    python -m headcount.organisers add 3 "Kai Ito"     # creates the user if needed
    python -m headcount.organisers remove 3 "Kai Ito"  # may remove the last one
"""

import argparse
import sys

import psycopg

from headcount import rsvps
from headcount.config import get_settings
from headcount.db import connect
from headcount.manage import ClubNotFound


class LastOrganiser(Exception):
    """A club needs at least one organiser."""


class NotAnOrganiser(rsvps.NotFound):
    pass


def is_organiser(conn: psycopg.Connection, club_id: int, user_id: int | None) -> bool:
    if user_id is None:
        return False
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM club_organisers WHERE club_id = %s AND user_id = %s) AS x",
        (club_id, user_id),
    ).fetchone()["x"]


def club_organisers(conn: psycopg.Connection, club_id: int) -> list[dict]:
    """The club's organisers in the order they were added."""
    return conn.execute(
        """
        SELECT u.id AS user_id, u.display_name, u.password_hash IS NOT NULL AS has_password,
               o.created_at AS added_at, adder.display_name AS added_by
          FROM club_organisers o
          JOIN users u ON u.id = o.user_id
          LEFT JOIN users adder ON adder.id = o.added_by
         WHERE o.club_id = %s
         ORDER BY o.created_at, u.id
        """,
        (club_id,),
    ).fetchall()


def clubs_organised_by(conn: psycopg.Connection, user_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT c.id, c.name FROM club_organisers o JOIN clubs c ON c.id = o.club_id
         WHERE o.user_id = %s ORDER BY c.name
        """,
        (user_id,),
    ).fetchall()


def _lock_club(conn: psycopg.Connection, club_id: int) -> None:
    """Serializes changes to one club's organiser list (e.g. two removals at once)."""
    if conn.execute("SELECT id FROM clubs WHERE id = %s FOR UPDATE", (club_id,)).fetchone() is None:
        raise ClubNotFound(club_id)


def add_organiser(conn: psycopg.Connection, club_id: int, display_name: str, *,
                  added_by: int | None = None, create_user: bool = False) -> dict:
    """Make the named person an organiser. Returns the user. Adding twice is harmless.

    The person must already have signed in once, unless create_user (the
    command line) is set, so a typo on the website doesn't invent an account.
    """
    name = rsvps.clean_display_name(display_name)
    with conn.transaction():
        _lock_club(conn, club_id)
        if create_user:
            user = rsvps.find_or_create_user(conn, name)
        else:
            user = conn.execute(
                "SELECT id, display_name FROM users WHERE lower(display_name) = lower(%s)", (name,)
            ).fetchone()
            if user is None:
                raise rsvps.UserNotFound(name)
        conn.execute(
            """
            INSERT INTO club_organisers (club_id, user_id, added_by) VALUES (%s, %s, %s)
            ON CONFLICT (club_id, user_id) DO NOTHING
            """,
            (club_id, user["id"], added_by),
        )
    return user


def remove_organiser(conn: psycopg.Connection, club_id: int, user_id: int, *,
                     allow_last: bool = False) -> None:
    with conn.transaction():
        _lock_club(conn, club_id)
        removed = conn.execute(
            "DELETE FROM club_organisers WHERE club_id = %s AND user_id = %s RETURNING user_id",
            (club_id, user_id),
        ).fetchone()
        if removed is None:
            raise NotAnOrganiser(user_id)
        left = conn.execute(
            "SELECT count(*) AS n FROM club_organisers WHERE club_id = %s", (club_id,)
        ).fetchone()["n"]
        if left == 0 and not allow_last:
            raise LastOrganiser(club_id)  # rolls the DELETE back


# ---------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m headcount.organisers",
        description="List, add or remove club organisers (for administrators).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every club and its organisers")
    for command in ("add", "remove"):
        p = sub.add_parser(command, help=f"{command} an organiser")
        p.add_argument("club_id", type=int)
        p.add_argument("display_name")
    args = parser.parse_args(argv)

    settings = get_settings()
    with connect(settings.database_url, autocommit=True, application_name="headcount-admin") as conn:
        try:
            if args.command == "list":
                for club in conn.execute("SELECT id, name FROM clubs ORDER BY id"):
                    people = club_organisers(conn, club["id"])
                    names = ", ".join(
                        p["display_name"] + ("" if p["has_password"] else " (no password yet)")
                        for p in people
                    ) or "no organisers"
                    print(f"{club['id']:>4}  {club['name']}: {names}")
            elif args.command == "add":
                user = add_organiser(conn, args.club_id, args.display_name, create_user=True)
                print(f"{user['display_name']} now organises club {args.club_id}.")
                has_password = conn.execute(
                    "SELECT password_hash IS NOT NULL AS x FROM users WHERE id = %s", (user["id"],)
                ).fetchone()["x"]
                if not has_password:
                    print("They need to sign in and set a password (Account page) before they can manage it.")
            else:
                user = conn.execute(
                    "SELECT id FROM users WHERE lower(display_name) = lower(%s)", (args.display_name,)
                ).fetchone()
                if user is None:
                    raise NotAnOrganiser(args.display_name)
                remove_organiser(conn, args.club_id, user["id"], allow_last=True)
                print(f"{args.display_name} no longer organises club {args.club_id}.")
        except ClubNotFound:
            print(f"organisers: there's no club {args.club_id}", file=sys.stderr)
            return 1
        except NotAnOrganiser:
            print(f"organisers: {args.display_name} isn't an organiser of club {args.club_id}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"organisers: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
