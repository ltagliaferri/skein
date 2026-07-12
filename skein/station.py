"""Station store/runtime factory — the ingress/read server's handle on the corpus.

Station re-home Stage 3. The live ingress opens its corpus through
``Station(data_dir)`` and touches only ``station.store`` + ``station.close()``
(``ingress.py``); the read server opens :class:`~skein.station_store.StationStore`
directly. This module re-homes ONLY that store/runtime factory from
``skein_next/station.py`` — the fat-client *authoring* verbs (create_site/post/
resolve/roster/…) are DROP (the working skein authors over its 8001 API), per
docs/STATION_REHOME_DESIGN.md §3.

Under Fork B the station corpus is the working ``versions``/``threads`` tables plus
the station sidecars, presented refs-free by ``StationStore`` — so ``Station`` is a
thin, context-manageable handle, not a second store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from .station_store import StationStore


class StationBootError(RuntimeError):
    """The station corpus is unusable at server boot — garbage bytes at the db
    path, the db path a directory, the data dir itself a file, a non-station
    corpus at the path, or (read surface only) nothing there to serve. Both
    servers' ``create_app`` classify the raw ``sqlite3.Error``/``OSError``/
    ``ValueError`` into this at their ONE boot-time open, so the entry points
    (``skein station serve``/``ingress``, ``python -m skein.web``/``skein.ingress``)
    present a clean one-line refusal + exit 2, never a traceback — the same
    clean-exit treatment ``StationEnvError``/``OperatorInvariantError`` get.
    Deliberately NOT raised for a missing db on the ingress: its read_write open
    legitimately CREATES the corpus (brief-20260712-t1tf #4)."""


class Station:
    """A content-hash station corpus handle. Context-manageable.

    Holds one long-lived :class:`~skein.station_store.StationStore` connection with
    the station posture (rollback-journal, servable ``:ro``). ``check_same_thread``
    is threaded through for the ingress threadpool, which opens and closes the
    ``Station`` inside the worker thread so its SQLite connection never crosses
    threads."""

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        check_same_thread: bool = True,
    ):
        self.store = StationStore(data_dir, check_same_thread=check_same_thread)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Station":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
