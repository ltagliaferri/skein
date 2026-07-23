#!/usr/bin/env python3
"""Source-checkout launcher for the SKEIN API service.

The service itself now lives in the package at :mod:`skein.server`, so a wheel
install has a real entrypoint (``skein-server`` and ``python -m skein.server``).
This file stays because the Makefile dev target boots the service through it
(``uvicorn skein_server:app``).
"""

from skein.server import app, get_config, main  # noqa: F401  (re-exported)

__all__ = ["app", "get_config", "main"]


if __name__ == "__main__":
    main()
