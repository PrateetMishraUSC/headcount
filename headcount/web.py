"""The FastAPI application.

    uvicorn headcount.web:app --no-access-log

(`--no-access-log` because the app writes its own one-JSON-line-per-request
log; uvicorn's access log would duplicate it.)

Every request that touches the database gets its own connection, opened with
a `statement_timeout`. A query that runs longer is cancelled by Postgres and
the request answers 504; if the database can't be reached it answers 503.
API paths get JSON errors, pages get a small HTML error page.
"""

import json
import sys
import time
from datetime import datetime, timezone
from html import escape

import psycopg
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from headcount import api, pages
from headcount.config import Settings
from headcount.db import connect
from headcount.deps import settings_for


class RequestLogMiddleware:
    """Pure ASGI middleware: one JSON line per HTTP request on stdout."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # what the client gets if the app raises before responding

        async def send_and_record(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_record)
        finally:
            line = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "method": scope["method"],
                "path": scope["path"],
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            sys.stdout.write(json.dumps(line) + "\n")
            sys.stdout.flush()


def wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/api/") or path == "/health"


def error_page(status: int, title: str, message: str) -> HTMLResponse:
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)} · Headcount</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/style.css"></head>
<body><main class="page error"><h1>{escape(title)}</h1><p>{escape(message)}</p>
<p><a href="/">Back to clubs</a></p></main></body></html>"""
    return HTMLResponse(body, status_code=status)


def _error(request: Request, status: int, title: str, message: str):
    if wants_json(request):
        return JSONResponse({"detail": message}, status_code=status)
    return error_page(status, title, message)


async def on_query_canceled(request: Request, exc: psycopg.errors.QueryCanceled):
    return _error(request, 504, "That took too long", "The database took too long to answer. Please try again.")


async def on_database_unavailable(request: Request, exc: psycopg.OperationalError):
    return _error(request, 503, "Temporarily unavailable", "The database is unavailable right now. Please try again shortly.")


async def on_http_exception(request: Request, exc: StarletteHTTPException):
    if wants_json(request):
        return await http_exception_handler(request, exc)
    titles = {404: "Not found", 409: "Not possible right now"}
    title = titles.get(exc.status_code, "Something went wrong")
    message = exc.detail if isinstance(exc.detail, str) else title
    return error_page(exc.status_code, title, message)


async def on_validation_error(request: Request, exc: RequestValidationError):
    """The API keeps FastAPI's detailed 422. A page whose URL doesn't parse
    (/clubs/abc) is simply a page that doesn't exist."""
    if wants_json(request):
        return await request_validation_exception_handler(request, exc)
    if any(err.get("loc", [None])[0] == "path" for err in exc.errors()):
        return error_page(404, "Not found", "There's no such page.")
    return error_page(400, "Something went wrong", "That request didn't make sense. Go back and try again.")


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Headcount", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.settings = settings
    app.add_middleware(RequestLogMiddleware)
    # QueryCanceled subclasses OperationalError; the more specific handler wins.
    app.add_exception_handler(psycopg.errors.QueryCanceled, on_query_canceled)
    app.add_exception_handler(psycopg.OperationalError, on_database_unavailable)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/health", tags=["ops"])
    def health(request: Request):
        """For the load balancer: 200 if this server can reach the database, else 503."""
        settings = settings_for(request)
        try:
            with connect(
                settings.database_url,
                autocommit=True,
                statement_timeout_ms=1000,
                connect_timeout_s=min(settings.connect_timeout_s, 2),
                application_name="headcount-health",
            ) as conn:
                conn.execute("SELECT 1")
        except psycopg.Error:
            return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    app.include_router(api.router)
    app.include_router(pages.router)
    return app


app = create_app()
