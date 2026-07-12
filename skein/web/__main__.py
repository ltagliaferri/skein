"""``python -m skein.web`` -> serve the read surface on port 9001."""

import sys

from ..station import StationBootError
from ..station_env import StationEnvError
from .app import run_server


def main() -> None:
    """The direct-entry twin of the ``skein station serve`` launcher's
    ClickException: a misconfigured env or an unusable corpus db exits with a
    clean one-line message, never a raw traceback. These two types deliberately
    propagate out of ``run_server`` so presentation lives at the entry points
    (see the run_server comment); this wrapper is that presentation here."""
    try:
        run_server()
    except (StationEnvError, StationBootError) as e:
        print(f"station will not start: {e}", file=sys.stderr)
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
