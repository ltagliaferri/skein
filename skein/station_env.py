"""Station environment-key resolution — the single choke point for station env.

Every station env read goes through :func:`station_env`, which resolves the
canonical ``SKEIN_STATION_<suffix>`` name from the process environment. Suffixes
are enumerated in ``_SUFFIXES``; an unknown one is a programming error (KeyError),
not a quiet ``None`` — a typo (e.g. a mis-spelled ``REQUIRE_SIGNED``, which would
otherwise resolve to the unset default and boot the public ingress open) must fail
loudly rather than silently.
"""

from __future__ import annotations

import os
from typing import Optional

NEW_PREFIX = "SKEIN_STATION_"

# Every station env suffix, enumerated so a mistyped key can't resolve to a silent
# None. The REQUIRE_SIGNED case is the one that matters: its unset default is the
# open posture, so a typo that read as None would boot the public ingress open.
_SUFFIXES = frozenset(
    {"DATA_DIR", "ORIGIN", "REQUIRE_SIGNED", "NAME", "AUTHORITY", "BASE_URL"}
)

# The one full key name callers need to WRITE (the launchers export the resolved
# data dir for the server process); reads all go through station_env().
ENV_DATA_DIR = NEW_PREFIX + "DATA_DIR"


class StationEnvError(RuntimeError):
    """Station env misconfiguration that must refuse loudly at startup — a value
    that fails validation (e.g. a malformed origin URL). Never a raw traceback,
    never a silent default."""


def station_env(suffix: str) -> Optional[str]:
    """Resolve one station env value by its ``SKEIN_STATION_<suffix>`` name.

    Returns the configured string (possibly empty — a present-but-empty value is
    state, e.g. an explicit require_signed falsy), or ``None`` when the key is
    unset. Unknown suffixes are a programming error (KeyError), not a quiet None —
    every station key is enumerated in ``_SUFFIXES``.
    """
    if suffix not in _SUFFIXES:
        raise KeyError(suffix)
    return os.environ.get(NEW_PREFIX + suffix)
