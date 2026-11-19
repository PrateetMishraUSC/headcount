"""Passwords and the sign-in cookie.

Members can keep signing in with just a display name. Anyone can protect their
name with a password (the Account page); from then on, signing in as that name
needs it. Club organisers must have one: organiser powers are refused to an
account without a password, because otherwise anyone could type their name.

The cookie is "<user id>.<signature>", where the signature is an HMAC of the
user id *and their current password hash* under SECRET_KEY. Nobody can forge
it without the key. Setting or changing a password changes the signature,
which signs out every other browser using that name, including anyone who
signed in as you before you had a password.

Passwords are hashed with scrypt (standard library) and a random salt.
"""

import base64
import hashlib
import hmac
import secrets

import psycopg

from headcount import rsvps

MIN_PASSWORD = 8
MAX_PASSWORD = 200
MIN_SECRET_KEY = 32
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class PasswordRequired(Exception):
    """This name has a password; sign-in needs it."""


class WrongPassword(Exception):
    pass


# ---------------------------------------------------------------- hashing


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                   dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def check_new_password(password: str, confirm: str | None = None) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Passwords need at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise ValueError(f"Passwords can be at most {MAX_PASSWORD} characters.")
    if confirm is not None and confirm != password:
        raise ValueError("The two passwords don't match.")
    return password


# ---------------------------------------------------------------- accounts


def _user_with_hash(conn: psycopg.Connection, where: str, value) -> dict | None:
    return conn.execute(
        f"SELECT id, display_name, created_at, password_hash FROM users WHERE {where}", (value,)
    ).fetchone()


def public(user: dict) -> dict:
    """The user without the hash, plus has_password, for pages and templates."""
    user = dict(user)
    user["has_password"] = bool(user.pop("password_hash", None))
    return user


def authenticate(conn: psycopg.Connection, display_name: str, password: str = "") -> dict:
    """Sign-in. New names are created (no password); protected names need theirs.

    Returns the user row including password_hash (needed for the cookie).
    Raises ValueError for a bad name, PasswordRequired or WrongPassword.
    """
    name = rsvps.clean_display_name(display_name)
    user = _user_with_hash(conn, "lower(display_name) = lower(%s)", name)
    if user is None:
        created = rsvps.find_or_create_user(conn, name)
        user = _user_with_hash(conn, "id = %s", created["id"])
    if user["password_hash"]:
        if not password:
            raise PasswordRequired(user["display_name"])
        if not verify_password(password, user["password_hash"]):
            raise WrongPassword(user["display_name"])
    return user


def set_password(conn: psycopg.Connection, user_id: int, new_password: str, *,
                 current_password: str = "", confirm: str | None = None) -> dict:
    """Set or change a password. Changing one needs the current one."""
    with conn.transaction():
        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        if user is None:
            raise rsvps.UserNotFound(user_id)
        if user["password_hash"] and not verify_password(current_password, user["password_hash"]):
            raise WrongPassword(user_id)
        check_new_password(new_password, confirm)
        return conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s "
            "RETURNING id, display_name, created_at, password_hash",
            (hash_password(new_password), user_id),
        ).fetchone()


# ---------------------------------------------------------------- the cookie


def check_secret_key(key: str) -> str:
    if len(key or "") < MIN_SECRET_KEY:
        raise RuntimeError(
            f"SECRET_KEY must be set to a random string of at least {MIN_SECRET_KEY} characters "
            "(it signs the sign-in cookie). Generate one with:\n"
            "  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return key


def _signature(secret: str, user_id: int, password_hash: str | None) -> str:
    message = f"{user_id}:{password_hash or ''}".encode()
    return _b64(hmac.new(check_secret_key(secret).encode(), message, hashlib.sha256).digest())


def cookie_value(secret: str, user: dict) -> str:
    """`user` must include password_hash (as returned by authenticate/set_password)."""
    return f"{user['id']}.{_signature(secret, user['id'], user['password_hash'])}"


def user_from_cookie(conn: psycopg.Connection, secret: str, raw: str | None) -> dict | None:
    """The signed-in user (public fields), or None if the cookie is missing, forged or stale."""
    user_id, _, signature = (raw or "").partition(".")
    if not user_id.isdigit() or len(user_id) > 18 or not signature:  # 18 digits fits a bigint
        return None
    user = _user_with_hash(conn, "id = %s", int(user_id))
    if user is None:
        return None
    if not hmac.compare_digest(signature, _signature(secret, user["id"], user["password_hash"])):
        return None
    return public(user)
