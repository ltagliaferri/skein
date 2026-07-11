"""``python -m skein.web`` -> serve the read surface on port 9001."""

import sys

from ..station_env import StationEnvError
from .app import run_server


def main() -> None:
    try:
        run_server()
    except StationEnvError as e:
        # The direct-entry twin of the ``skein station serve`` launcher's
        # ClickException: a conflicting/malformed station env key exits with a
        # clean one-line message, never a raw traceback.
        print(f"station will not start: {e}", file=sys.stderr)
        raise SystemExit(2) from e


if __name__ == "__main__":
    main()
