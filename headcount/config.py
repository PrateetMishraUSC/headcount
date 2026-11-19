"""Settings from environment variables, with an optional `.env` file.

Real environment variables always win over values in `.env`, so production
can set them directly and developers can keep a local `.env`.
"""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from `path` into os.environ without overriding.

    Deliberately tiny: blank lines and `#` comments are skipped, an optional
    `export ` prefix and matching surrounding quotes are stripped.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    database_url: str
    statement_timeout_ms: int
    connect_timeout_s: int
    worker_poll_interval_s: float
    worker_reconnect_every: int
    display_timezone: str = "America/Los_Angeles"
    secret_key: str = ""  # signs the sign-in cookie; only the web app needs it


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def require(name: str) -> str:
    load_dotenv()
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Set it in the environment or in .env "
            f"(see .env.example)."
        )
    return value


def get_settings() -> Settings:
    """Read settings now (not at import time) so tests can change the env."""
    load_dotenv()
    settings = Settings(
        database_url=require("DATABASE_URL"),
        statement_timeout_ms=_int("STATEMENT_TIMEOUT_MS", 5000),
        connect_timeout_s=_int("DB_CONNECT_TIMEOUT_S", 5),
        worker_poll_interval_s=_float("WORKER_POLL_INTERVAL_S", 1.0),
        worker_reconnect_every=_int("WORKER_RECONNECT_EVERY", 360),
        display_timezone=os.environ.get("DISPLAY_TIMEZONE", "").strip() or "America/Los_Angeles",
        secret_key=os.environ.get("SECRET_KEY", "").strip(),
    )
    if settings.worker_reconnect_every < 1:
        raise RuntimeError("WORKER_RECONNECT_EVERY must be at least 1")
    return settings
