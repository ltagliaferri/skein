#!/usr/bin/env python3
"""
SKEIN API service - Structured Knowledge Exchange & Integration Nexus

The local API service the ``skein`` CLI talks to. It lives inside the package
(rather than as a loose script at the repo root) so a plain wheel install has a
runnable service: ``skein-server``, ``python -m skein.server``, and
``skein service start`` all land here. Nothing in this module resolves anything
relative to the working directory — project data comes from the
``<SKEIN_HOME>/projects.json`` registry — so the service runs correctly from any
cwd, including one with no source checkout in sight.
"""

import argparse
import contextvars
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from skein.routes import router as skein_router
from skein.storage import skein_home
from skein.version import package_version

# Context variable for request ID - accessible throughout the request lifecycle
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# Config file in a source checkout: <repo>/config/config.json, i.e. the sibling
# of the package directory. In a wheel install that same relative spot is
# site-packages/config/config.json — which another installed package could
# create — so it is only consulted when it is genuinely a checkout (a sibling
# pyproject.toml proves that), never in an install. See is_source_checkout().
REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_CONFIG_PATH = REPO_ROOT / "config" / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8001,
    "log_level": "info",
}


def is_source_checkout() -> bool:
    """Whether the package is running from a source tree rather than an install.

    A checkout has a pyproject.toml beside the package directory; site-packages
    does not. This is what keeps a wheel install from reading an unrelated
    ``site-packages/config/config.json`` as SKEIN's server config.
    """
    return (REPO_ROOT / "pyproject.toml").is_file()


def config_search_paths() -> List[Path]:
    """Config files to try, most specific first; the first that exists wins.

    ``SKEIN_SERVER_CONFIG`` is the explicit override. ``<SKEIN_HOME>/server.json``
    is the install-independent location a wheel user can write. The repo config is
    consulted only from an actual checkout, so an install cannot pick up a config
    file that merely happens to sit at the same relative path in site-packages.
    """
    paths: List[Path] = []
    override = os.getenv("SKEIN_SERVER_CONFIG")
    if override:
        paths.append(Path(override).expanduser())
    paths.append(skein_home() / "server.json")
    if is_source_checkout():
        paths.append(REPO_CONFIG_PATH)
    return paths


def get_config() -> Dict[str, Any]:
    """Load service configuration from a config file, then environment variables.

    Environment variables take precedence over the file, so ``SKEIN_PORT=8123
    skein-server`` wins over a config file that says otherwise.

    Note what this does NOT reach: the ``skein`` CLI resolves the URL it talks to
    separately (``--url``, ``SKEIN_URL``, then ``server_url`` in the project and
    global configs, then ``http://localhost:8001``). Bind the service somewhere
    other than that default and you must point the CLI at it too. Reconciling the
    two into one ladder is deliberately out of scope here; ``skein doctor``
    reports when nothing is answering where the CLI is looking.
    """
    # Loopback by default; SKEIN has no auth, so opt in to network exposure via
    # SKEIN_HOST or a config file.
    config = dict(DEFAULT_CONFIG)

    for config_file in config_search_paths():
        try:
            if not config_file.exists():
                continue
            with open(config_file) as f:
                file_config = json.load(f)
            # Repo config nests under "server"; a bare mapping is accepted too so
            # <SKEIN_HOME>/server.json can just be {"port": 8123}.
            section = file_config.get("server", file_config)
            if isinstance(section, dict):
                config.update({k: v for k, v in section.items() if k in DEFAULT_CONFIG})
            break
        except Exception:
            # A malformed config never keeps the service from booting on defaults.
            continue

    # Environment variables take precedence
    if os.getenv("SKEIN_HOST"):
        config["host"] = os.getenv("SKEIN_HOST")
    if os.getenv("SKEIN_PORT"):
        config["port"] = int(os.getenv("SKEIN_PORT"))
    if os.getenv("SKEIN_LOG_LEVEL"):
        config["log_level"] = os.getenv("SKEIN_LOG_LEVEL")

    return config


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

    systemd splits ExecStart on whitespace, so a path containing a space (a
    macOS "Application Support" tree, a home directory with a space) must be
    double-quoted or systemd execs a truncated path. Double quotes and
    backslashes inside the token are backslash-escaped, per systemd.exec(5).
    """
    if token and not any(c in token for c in ' \t"\\'):
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
    return [str(Path(sys.executable).resolve()), "-m", "skein.server"]


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
    args = parser.parse_args(argv)

    if args.version:
        print(SERVICE_VERSION)
        return

    if args.print_unit:
        # No trailing newline munging: the template already ends with one.
        print(render_unit(), end="")
        return

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
