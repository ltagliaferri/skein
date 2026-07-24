#!/usr/bin/env python3
"""
SKEIN API service - Structured Knowledge Exchange & Integration Nexus

The local API service the ``skein`` CLI talks to. It lives inside the package
(rather than as a loose script at the repo root) so a plain wheel install has a
runnable service: ``skein-server`` and ``python -m skein.server`` both land here.
Nothing in this module resolves anything relative to the working directory —
project data comes from the ``<SKEIN_HOME>/projects.json`` registry — so the
service runs correctly from any cwd, including one with no source checkout in
sight.
"""

import argparse
import contextvars
import logging
import os
import uuid
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from skein.routes import router as skein_router

# The bind-address ladder lives in skein.service_address (import-light, so the
# CLI can share it as the floor of its URL resolution). Re-exported here because
# this is where they lived first — the launcher shim and tests import them.
from skein.service_address import (  # noqa: F401
    DEFAULT_CONFIG,
    REPO_CONFIG_PATH,
    REPO_ROOT,
    config_search_paths,
    get_config,
    is_source_checkout,
    parse_port,
    resolve_service_config,
)
from skein.storage import skein_home
from skein.version import package_version

# Context variable for request ID - accessible throughout the request lifecycle
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add request ID tracking to all API calls.

    - Uses X-Request-ID header if provided by client
    - Otherwise generates a new UUID
    - Sets request ID in context var for use in logging
    - Returns X-Request-ID header in response
    """

    async def dispatch(self, request: Request, call_next):
        # Get request ID from header or generate new one
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

        # Store in context var for access throughout request
        request_id_var.set(request_id)

        # Also attach to request state for easy access in handlers
        request.state.request_id = request_id

        # Log the incoming request with request ID
        logger.info(f"[{request_id}] {request.method} {request.url.path}")

        # Process the request
        response = await call_next(request)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        return response


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# The version both ends of the install report. Read once at import so every
# response and the /health probe agree within a single process.
SERVICE_VERSION = package_version()

# Create FastAPI app
app = FastAPI(
    title="SKEIN API",
    description="Structured Knowledge Exchange & Integration Nexus - Agent collaboration infrastructure",
    version=SERVICE_VERSION,
)


# Global exception handler for unhandled errors
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch-all exception handler to prevent 500 errors from crashing requests.
    Logs full stack trace and returns structured error response with request ID.
    """
    request_id = getattr(request.state, "request_id", None) or request_id_var.get() or "unknown"

    logger.error(
        f"[{request_id}] Unhandled exception on {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        exc_info=True,
    )

    response = JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path,
            "request_id": request_id,
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


# Request ID middleware - must be added before CORS so it runs first
app.add_middleware(RequestIDMiddleware)

# CORS middleware (allow all origins for development)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],  # Expose request ID header to clients
)

# Include SKEIN routes
app.include_router(skein_router, prefix="/skein", tags=["skein"])


@app.get("/")
async def root():
    """Root endpoint - API info."""
    return {
        "name": "SKEIN API",
        "version": SERVICE_VERSION,
        "description": "Structured Knowledge Exchange & Integration Nexus",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Health check endpoint.

    ``version`` is the installed ``interskein`` distribution version. ``skein
    doctor`` compares it against the CLI's own version to catch a service left
    running from a different install.
    """
    return {
        "status": "healthy",
        "version": SERVICE_VERSION,
        "distribution": "interskein",
        "pid": os.getpid(),
        "skein_home": str(skein_home()),
    }


def unit_template_path() -> Path:
    """Path to the packaged systemd unit template (with the ExecStart placeholder)."""
    return Path(__file__).resolve().parent / "units" / "skein.service"


def _systemd_quote(token: str) -> str:
    """Quote one ExecStart token for systemd if it needs it.

    Three systemd rules, per systemd.exec(5) / systemd.unit(5):
    - It splits ExecStart on whitespace, so a token with a space (a macOS
      "Application Support" tree, a home directory with a space) must be quoted
      or systemd execs a truncated path.
    - It treats both ``'`` and ``"`` as quote characters, so a bare token
      containing either is "unbalanced quoting"; double-quoting the whole token
      makes an apostrophe literal.
    - It expands ``%`` specifiers even inside quotes, so a literal ``%`` in the
      path must be doubled to ``%%``.
    """
    token = token.replace("%", "%%")
    if token and not any(c in token for c in " \t'\"\\"):
        return token
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def server_command() -> List[str]:
    """The argv that launches THIS install's service, for a supervisor unit.

    The invariant is "launch the exact install doing the rendering." That is
    this process's own ``skein-server`` console script when it was run as one
    (``sys.argv[0]``, executable), and otherwise ``<this python> -m
    skein.server`` — which always runs this package under this interpreter with
    no PATH lookup. PATH is deliberately never searched: a ``skein-server`` on
    PATH can be a different, stale install than the one rendering the unit.
    """
    import os
    import sys

    argv0 = Path(sys.argv[0])
    if argv0.name == "skein-server" and argv0.is_file() and os.access(argv0, os.X_OK):
        return [str(argv0.resolve())]
    # Fallback: this interpreter running the module. sys.executable is left
    # unresolved on purpose — resolving a venv's python symlink to the base
    # interpreter loses the venv, and the base python cannot import skein.server.
    return [sys.executable, "-m", "skein.server"]


def server_execstart() -> str:
    """The ExecStart value for the rendered unit: this install's argv, quoted."""
    return " ".join(_systemd_quote(t) for t in server_command())


def render_unit() -> str:
    """The packaged unit with its ExecStart placeholder resolved to this install.

    Printed by ``skein-server --print-unit``; the caller redirects it into
    ``~/.config/systemd/user/skein.service``. Runs inside the service's own
    environment, so it resolves the right command even for an isolated
    ``uv tool`` / ``pipx`` install a bare ``python`` could not import.
    """
    return unit_template_path().read_text().replace("__SKEIN_SERVER__", server_execstart())


def plist_template_path() -> Path:
    """Path to the packaged launchd plist template."""
    return Path(__file__).resolve().parent / "units" / "skein.plist"


def render_plist() -> str:
    """The packaged launchd plist resolved to this install, for macOS.

    Printed by ``skein-server --print-plist``; the caller redirects it into
    ``~/Library/LaunchAgents/net.interskein.skein-server.plist``. Each argv
    token becomes one XML-escaped ``<string>`` in ProgramArguments — launchd
    takes an argv array, so no shell quoting is involved. The log path is
    rendered absolute because launchd does not expand ``~``.
    """
    from xml.sax.saxutils import escape

    args = "\n".join(f"\t\t<string>{escape(token)}</string>" for token in server_command())
    log_path = str(Path.home() / "Library" / "Logs" / "skein-server.log")
    return (
        plist_template_path()
        .read_text()
        .replace("__SKEIN_SERVER_ARGS__", args)
        .replace("__SKEIN_LOG_PATH__", escape(log_path))
    )


def main(argv: Optional[List[str]] = None) -> None:
    """Console-script entrypoint (``skein-server``) and ``python -m skein.server``."""
    config = get_config()

    parser = argparse.ArgumentParser(
        prog="skein-server", description="Run the local SKEIN API service."
    )
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8001)")
    parser.add_argument("--log-level", default=None, help="uvicorn log level")
    parser.add_argument("--version", action="store_true", help="Print the service version and exit")
    parser.add_argument(
        "--print-unit",
        action="store_true",
        help="Print a systemd user unit for this install (redirect into "
        "~/.config/systemd/user/skein.service) and exit",
    )
    parser.add_argument(
        "--print-plist",
        action="store_true",
        help="Print a macOS launchd agent plist for this install (redirect into "
        "~/Library/LaunchAgents/net.interskein.skein-server.plist) and exit",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(SERVICE_VERSION)
        return

    # No trailing newline munging on either: the templates end with one.
    if args.print_unit:
        print(render_unit(), end="")
        return

    if args.print_plist:
        print(render_plist(), end="")
        return

    # Resolution ignores an unparseable SKEIN_PORT so that the CLI (which
    # shares it) never crashes on one — but for the service itself, silently
    # binding a port the operator did not ask for is worse than refusing to
    # start. An explicit --port outranks the env var, so only fail without one.
    port_env = os.getenv("SKEIN_PORT")
    if args.port is None and port_env and parse_port(port_env) is None:
        raise SystemExit(f"skein-server: SKEIN_PORT={port_env!r} is not a valid port (1-65535)")

    host_env = os.getenv("SKEIN_HOST")
    if args.host is None and host_env and not host_env.strip():
        raise SystemExit(f"skein-server: SKEIN_HOST={host_env!r} is blank")

    # Same invariant for the explicit config-file override: an operator who
    # set SKEIN_SERVER_CONFIG must not be silently served defaults because the
    # value cannot even become a path. There is no flag override here; unset it.
    config_override = os.getenv("SKEIN_SERVER_CONFIG")
    if config_override:
        try:
            Path(config_override).expanduser()
        except (RuntimeError, ValueError):
            raise SystemExit(
                f"skein-server: SKEIN_SERVER_CONFIG={config_override!r} is not a usable path"
            )

    host = args.host or config["host"]
    port = args.port if args.port is not None else config["port"]
    log_level = args.log_level or config["log_level"]

    logger.info("=" * 80)
    logger.info("🧵 Starting SKEIN Server")
    logger.info("=" * 80)
    logger.info(f"Version: {SERVICE_VERSION}")
    logger.info(f"Host: {host}")
    logger.info(f"Port: {port}")
    logger.info(f"SKEIN home: {skein_home()}")
    logger.info(f"Docs: http://localhost:{port}/docs")
    logger.info("=" * 80)

    uvicorn.run(app, host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
