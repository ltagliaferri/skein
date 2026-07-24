"""The machine-level SKEIN service address.

One ladder answers "where is the local SKEIN service?" for every entrypoint:
``skein-server`` (and ``python -m skein.server``) binds it, and the ``skein``
CLI's URL resolution bottoms out on it (``client/cli.py`` ``get_base_url``).
Before this module the two halves resolved independently and shared only the
coincidence of both defaulting to 8001, so ``SKEIN_PORT=8123 skein-server``
moved the service while every CLI command kept asking 8001 and reported
nothing running — with a healthy service up (notion-20260722-95kl).

The ladder, most specific first (``--host``/``--port`` flags, handled by the
server's ``main()``, sit above all of it):

1. ``SKEIN_HOST`` / ``SKEIN_PORT`` / ``SKEIN_LOG_LEVEL`` environment variables
2. ``SKEIN_SERVER_CONFIG`` file
3. ``<SKEIN_HOME>/server.json``
4. ``<repo>/config/config.json`` — source checkouts only, see
   :func:`is_source_checkout`
5. built-in defaults: ``127.0.0.1:8001``

Resolution never raises. The CLI consults it on every command, so a typo'd
``SKEIN_PORT`` or a malformed config file must not turn every ``skein``
invocation into a traceback: invalid values are ignored and recorded in
:attr:`ServiceAddress.problems`, which ``skein doctor`` reports. The server's
``main()`` is stricter about ``SKEIN_PORT`` — binding a port the operator did
not ask for is worse than refusing to start — but it enforces that itself.

Kept import-light on purpose (no FastAPI, no requests): the CLI pays this
module's import cost on every command. ``skein.server`` re-exports the names
that used to live there.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from skein.storage import skein_home

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


class ServiceAddress(NamedTuple):
    """A resolved service config plus where each value came from.

    ``sources`` maps each config key to the rung that set it: ``"default"``,
    the environment variable name, or the path of the config file. ``problems``
    holds one human-readable line per value that was ignored as invalid —
    ``skein doctor`` surfaces them; nothing else acts on them.
    """

    config: Dict[str, Any]
    sources: Dict[str, str]
    problems: List[str]


def is_source_checkout() -> bool:
    """Whether the package is running from a source tree rather than an install.

    An installed package lives under a ``site-packages`` / ``dist-packages``
    tree; that is the reliable signal, and it cannot be defeated by files planted
    beside the package (a stray ``pyproject.toml`` there does not make it a
    checkout). A checkout is then confirmed by a sibling ``pyproject.toml``. This
    is what keeps a wheel install from reading an unrelated
    ``site-packages/config/config.json`` as SKEIN's server config.
    """
    if {"site-packages", "dist-packages"} & set(REPO_ROOT.parts):
        return False
    return (REPO_ROOT / "pyproject.toml").is_file()


def config_search_paths(problems: Optional[List[str]] = None) -> List[Path]:
    """Config files to try, most specific first; the first that exists wins.

    ``SKEIN_SERVER_CONFIG`` is the explicit override. ``<SKEIN_HOME>/server.json``
    is the install-independent location a wheel user can write. The repo config is
    consulted only from an actual checkout, so an install cannot pick up a config
    file that merely happens to sit at the same relative path in site-packages.

    An override that cannot even be turned into a path — ``~nosuchuser/...``
    (expanduser raises), an embedded NUL — is dropped rather than raised, and
    the drop is recorded in ``problems`` when the caller passes a list.
    resolve_service_config does, so `skein doctor` reports the override an
    operator believes is in effect but is not; ``skein-server`` refuses to
    start on one (main()).
    """
    paths: List[Path] = []
    override = os.getenv("SKEIN_SERVER_CONFIG")
    if override:
        try:
            paths.append(Path(override).expanduser())
        except (RuntimeError, ValueError) as e:
            if problems is not None:
                problems.append(
                    f"SKEIN_SERVER_CONFIG={override!r} is not a usable path "
                    f"({type(e).__name__}: {e}), ignored"
                )
    paths.append(skein_home() / "server.json")
    if is_source_checkout():
        paths.append(REPO_CONFIG_PATH)
    return paths


def parse_port(value: Any) -> Optional[int]:
    """``value`` as a usable TCP port, or None.

    Accepts an int or an int-shaped string in 1-65535. Returns None for
    anything else (bool included: JSON ``true`` is not port 1).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 else None


def _accept(key: str, value: Any) -> Optional[Any]:
    """The usable form of a config-file ``value`` for ``key``, or None.

    A file value the service cannot bind (a port of ``"abc"``, a host of
    ``12``) is rejected here so both ends ignore it identically, rather than
    the server crashing at bind time while the client builds a garbage URL.
    """
    if key == "port":
        return parse_port(value)
    if key in ("host", "log_level"):
        # Stripped: an unstripped " 10.0.0.5 " is unbindable and would build
        # exactly the garbage URL this function exists to prevent.
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def resolve_service_config() -> ServiceAddress:
    """Resolve the service address with provenance. Never raises.

    A config file that exists but cannot be read or parsed is skipped (a
    malformed config never keeps the service from booting on defaults); an
    individual invalid value in a readable file is ignored. Both are recorded
    in ``problems`` for ``skein doctor``.
    """
    config = dict(DEFAULT_CONFIG)
    sources = {key: "default" for key in DEFAULT_CONFIG}
    problems: List[str] = []

    for config_file in config_search_paths(problems):
        try:
            # stat, not exists(): Python 3.13's Path.exists() suppresses every
            # OSError, which would turn an unprobeable path (a sealed
            # SKEIN_HOME) into a silent skip instead of a recorded problem.
            # A genuinely absent file is the silent skip.
            try:
                config_file.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            with open(config_file) as f:
                file_config = json.load(f)
            # Repo config nests under "server"; a bare mapping is accepted too so
            # <SKEIN_HOME>/server.json can just be {"port": 8123}. A file
            # holding a non-object (.get raises) is unusable: the except below
            # records it and resolution moves on to the next path.
            section = file_config.get("server", file_config)
            if isinstance(section, dict):
                for key in DEFAULT_CONFIG:
                    if key not in section:
                        continue
                    accepted = _accept(key, section[key])
                    if accepted is None:
                        problems.append(
                            f"{key}={section[key]!r} in {config_file} is not a "
                            f"usable {key}, ignored"
                        )
                    else:
                        config[key] = accepted
                        sources[key] = str(config_file)
            break
        except Exception as e:
            # A malformed config never keeps the service from booting on
            # defaults, and never breaks a CLI command's URL resolution.
            problems.append(
                f"config file {config_file} is unusable ({type(e).__name__}: {e}), skipped"
            )
            continue

    # Environment variables take precedence over any file.
    host_env = os.getenv("SKEIN_HOST")
    if host_env:
        config["host"] = host_env
        sources["host"] = "SKEIN_HOST"
    port_env = os.getenv("SKEIN_PORT")
    if port_env:
        port = parse_port(port_env)
        if port is None:
            problems.append(f"SKEIN_PORT={port_env!r} is not a valid port (1-65535), ignored")
        else:
            config["port"] = port
            sources["port"] = "SKEIN_PORT"
    log_env = os.getenv("SKEIN_LOG_LEVEL")
    if log_env:
        config["log_level"] = log_env
        sources["log_level"] = "SKEIN_LOG_LEVEL"

    return ServiceAddress(config, sources, problems)


def get_config() -> Dict[str, Any]:
    """Load service configuration from a config file, then environment variables.

    Environment variables take precedence over the file, so ``SKEIN_PORT=8123
    skein-server`` wins over a config file that says otherwise.

    This same answer is the floor of the CLI's URL resolution (``get_base_url``
    in ``client/cli.py``), so moving the service — env var or config file —
    moves the client with it.
    """
    # Loopback by default; SKEIN has no auth, so opt in to network exposure via
    # SKEIN_HOST or a config file.
    return resolve_service_config().config


def local_service_url() -> "tuple[str, ServiceAddress]":
    """The URL a local client uses to reach the service this machine's config
    describes, plus the resolution it came from. Never raises.

    A wildcard bind is not a connectable address, so 0.0.0.0 maps to 127.0.0.1
    and :: to ::1; IPv6 hosts are bracketed for the URL.
    """
    resolved = resolve_service_config()
    host = resolved.config["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{resolved.config['port']}", resolved
