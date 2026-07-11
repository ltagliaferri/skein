"""Station environment-key resolution — the single choke point for station env.

Station re-home Stage 6 (design §5 Stage 6, §10 #5). The canonical keys are
``SKEIN_STATION_*``; the retired ``skein_next`` build's ``SKEIN_NEXT_*`` keys are
accepted as fallback aliases ONLY here, so a box mid-transition keeps working —
loudly. Stage 8 (delete skein_next) removes ``_LEGACY_ALIASES`` and the fallback
branch; after that a grep for SKEIN_NEXT must come up empty in ``skein/``.

Transition rules (each deliberate):

- New key set → used. If the legacy key is ALSO set to the same value, a
  FutureWarning nudges removing it.
- Legacy key alone → used, with a FutureWarning naming the new key. This keeps a
  not-yet-migrated deployment (e.g. the live compose during the Stage-7 window)
  on its configured posture instead of silently reverting to defaults — the
  ``REQUIRE_SIGNED`` case is the one that matters: dropping it to the unset
  default would boot the public ingress open.
- Both set to DIFFERENT values → :class:`StationEnvError`. A half-configured box
  must refuse rather than pick a winner by precedence.
"""

from __future__ import annotations

import os
import warnings
from typing import Optional

NEW_PREFIX = "SKEIN_STATION_"
LEGACY_PREFIX = "SKEIN_NEXT_"

# new-name suffix -> legacy-name suffix. Identical except NAME: the legacy key
# said PROJECT, a wording that is deprecated (the value bootstraps the station's
# display name; stationfile.name wins over it either way).
_LEGACY_ALIASES = {
    "DATA_DIR": "DATA_DIR",
    "ORIGIN": "ORIGIN",
    "REQUIRE_SIGNED": "REQUIRE_SIGNED",
    "NAME": "PROJECT",
    "AUTHORITY": "AUTHORITY",
    "BASE_URL": "BASE_URL",
}


# The one full key name callers need to WRITE (the launchers export the resolved
# data dir for the server process); reads all go through station_env().
ENV_DATA_DIR = NEW_PREFIX + "DATA_DIR"


class StationEnvError(RuntimeError):
    """Station env misconfiguration that must refuse loudly at startup —
    conflicting new/legacy keys, or a value that fails validation (e.g. a
    malformed origin URL). Never a raw traceback, never a silent default."""


def station_env(suffix: str) -> Optional[str]:
    """Resolve one station env value by its ``SKEIN_STATION_<suffix>`` name.

    Returns the configured string (possibly empty — a present-but-empty value is
    state, e.g. an explicit require_signed falsy), or ``None`` when neither the
    new nor the legacy key is set. Unknown suffixes are a programming error
    (KeyError), not a quiet None — every station key is enumerated above.
    """
    new_key = NEW_PREFIX + suffix
    legacy_key = LEGACY_PREFIX + _LEGACY_ALIASES[suffix]
    new_val = os.environ.get(new_key)
    legacy_val = os.environ.get(legacy_key)
    if new_val is not None and legacy_val is not None and new_val != legacy_val:
        raise StationEnvError(
            f"{new_key}={new_val!r} and legacy {legacy_key}={legacy_val!r} disagree; "
            f"unset {legacy_key} (it is a deprecated alias) and configure only {new_key}"
        )
    if legacy_val is not None:
        # Fires for legacy-only AND for both-set-identical: either way the box
        # still exports a retired key and should migrate before Stage 8 drops it.
        warnings.warn(
            f"{legacy_key} is a deprecated alias from the retired skein_next build; "
            f"set {new_key} instead (the alias is removed when skein_next is deleted)",
            FutureWarning,
            stacklevel=2,
        )
    if new_val is not None:
        return new_val
    return legacy_val
