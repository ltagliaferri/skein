#!/usr/bin/env python3
"""Source-checkout launcher for the SKEIN API service.

The service itself now lives in the package at :mod:`skein.server`, so a wheel
install has a real entrypoint (``skein-server``, ``python -m skein.server``,
``skein service start``). This file stays because the systemd unit template and
the fidelity harness both boot the service as ``python skein_server.py`` from
the repo root.
"""

from skein.server import app, get_config, main  # noqa: F401  (re-exported)

__all__ = ["app", "get_config", "main"]


if __name__ == "__main__":
    main()
