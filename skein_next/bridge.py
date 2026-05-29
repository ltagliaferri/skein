"""Import bridge: a legacy SKEIN project store -> a content-hash new-skein store.

Slice 2 of the parallel station (brief-20260529-i6fy). The bridge reads a legacy
``.skein/data/skein.db`` strictly read-only and produces a faithful new-skein
store under ``.skein-next/``. It never writes the legacy database.

What it does, and the decisions behind each step:

1. **Folios** are re-hashed from their five canonical fields with a normalized
   ``created_at`` (``store.create_folio`` does the hashing). The legacy
   ``content_hash`` column is never trusted — the audit proved it was written
   over at least three incompatible ``created_at`` encodings, so roughly half of
   the stored hashes do not reproduce. Each ``legacy_id -> new hash`` mapping is
   recorded in the alias table (open-call #1).

2. **Thread endpoints** are classified three ways (open-call #2):
   - a folio id present in this project -> resolved to its content hash;
   - an actor (agent name, shard hex, session string — anything that is not a
     folio id here) -> this is the *weaver*, not an edge endpoint, so it is
     routed to the thread's weaver and the edge position is cleared;
   - a folio-id-shaped reference that is not present in this project (dangling or
     cross-project ``proj:id``) -> kept verbatim as the legacy id so it resolves
     lazily through the alias table if the target ever imports.
   Thread types pass through untouched; the sole rename is
   ``succession -> supersedes`` when the link actually joins two folios.

3. **Sites** (filesystem JSON under ``.skein/data/sites/``) each become a
   ``type=site`` folio (content = its purpose); every member folio gets a
   ``within`` thread (folio -> site folio); a station-internal slug table maps
   ``slug -> site folio hash``.

4. **Idempotent.** Content-hash identity dedups folios and threads, so running
   twice against a frozen source yields an identical store.

5. **No silent loss.** :class:`ImportReport` accounts for what carried, what was
   reclassified (actors->weaver, unresolved refs kept), and anything dropped or
   merged — including genuinely-distinct edges the thread hash collapses
   (a Slice 1 fell note).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .store import SkeinNextStore

# A folio id looks like ``<type>-<8-digit-date>-<suffix>``. The date shape alone
# does not separate a folio ref from a session id (``qm-20260105-201613`` matches
# too), so classification also requires the prefix to be a real folio *type* —
# the whitelist is derived per-project from the legacy ``folios.type`` column and
# the prefixes of actual folio ids, so sessions and agents never pass.
_FOLIO_ID_SHAPE = re.compile(r"^([a-z]+)-\d{8}-\w+$", re.IGNORECASE)

_OPEN_STATUSES = {None, "", "open"}


def open_legacy(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a legacy SKEIN sqlite database read-only.

    Tries ``mode=ro`` first (the documented safe default on a writable
    filesystem). Falls back to ``immutable=1`` when the database sits on a
    read-only mount, where ``mode=ro`` cannot create the shared-memory/journal
    files SQLite wants. Both modes are read-only; neither writes the source.
    """
    p = str(Path(db_path))
    last_err: Optional[Exception] = None
    for uri in (f"file:{p}?mode=ro", f"file:{p}?immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM folios LIMIT 1")  # force the open to resolve
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            try:
                conn.close()  # type: ignore[has-type]
            except Exception:
                pass
    raise sqlite3.OperationalError(
        f"could not open {p} read-only (mode=ro or immutable=1): {last_err}"
    )


def _folio_types(conn: sqlite3.Connection) -> Set[str]:
    """The set of real folio type prefixes in this legacy db (the whitelist)."""
    types = {
        r[0].lower()
        for r in conn.execute("SELECT DISTINCT type FROM folios")
        if r[0]
    }
    for r in conn.execute("SELECT folio_id FROM folios"):
        fid = r[0]
        if fid and "-" in fid:
            types.add(fid.split("-", 1)[0].lower())
    return types


def classify_endpoint(
    endpoint: Optional[str],
    folio_ids: Set[str],
    folio_types: Set[str],
) -> Tuple[str, Optional[str]]:
    """Classify a legacy thread endpoint.

    Returns ``(kind, value)`` where ``kind`` is one of:
    - ``"none"``        — endpoint is absent;
    - ``"folio"``       — a folio present in this project (resolve via alias);
    - ``"unresolved"``  — a folio-id-shaped ref not present here (dangling or
                          cross-project); ``value`` is the legacy id to keep;
    - ``"actor"``       — anything else (agent/session/shard); route to weaver.
    """
    if endpoint is None:
        return ("none", None)
    if endpoint in folio_ids:
        return ("folio", endpoint)
    base = endpoint.split(":", 1)[1] if ":" in endpoint else endpoint
    m = _FOLIO_ID_SHAPE.match(base)
    if m and m.group(1).lower() in folio_types:
        return ("unresolved", endpoint)
    return ("actor", endpoint)


@dataclass
class ImportReport:
    """A full accounting of an import — nothing is allowed to vanish silently."""

    source_db: str = ""
    sites_dir: str = ""

    folios_seen: int = 0
    folios_carried: int = 0          # legacy folios mapped to a new hash
    folio_hash_collisions: int = 0   # distinct legacy folios that hashed identically

    sites_seen: int = 0
    sites_carried: int = 0

    threads_seen: int = 0            # legacy thread rows read
    threads_carried: int = 0         # distinct new thread hashes from those rows
    threads_merged: int = 0          # legacy rows that collapsed onto an existing hash
    merge_examples: List[str] = field(default_factory=list)

    within_threads: int = 0          # synthesized folio->site membership edges
    within_unresolved: int = 0       # member folios whose site had no JSON

    actor_endpoints_to_weaver: int = 0   # endpoint occurrences routed off the edge
    actor_endpoints_dropped: int = 0     # actor identities lost (weaver holds only one)
    dropped_examples: List[str] = field(default_factory=list)

    unresolved_refs: int = 0         # folio-shaped endpoints kept as legacy ids
    cross_project_refs: int = 0      # subset of unresolved with a ``proj:`` prefix
    dangling_refs: int = 0           # subset of unresolved without a prefix

    succession_renamed: int = 0      # succession -> supersedes

    status_without_thread: int = 0   # non-open folio status with no status thread
    status_examples: List[str] = field(default_factory=list)

    notes: List[str] = field(default_factory=list)

    def render(self) -> str:
        """A plain-text report (no tables/markdown — screen-reader friendly)."""
        lines = [
            f"import report: {self.source_db}",
            f"folios: carried {self.folios_carried} of {self.folios_seen} seen",
            f"folio hash collisions: {self.folio_hash_collisions}",
            f"sites: carried {self.sites_carried} of {self.sites_seen} seen",
            f"within threads: {self.within_threads} (unresolved site refs {self.within_unresolved})",
            f"threads: carried {self.threads_carried} of {self.threads_seen} seen",
            f"threads merged (distinct legacy edges collapsed to one hash): {self.threads_merged}",
            f"actor endpoints routed to weaver: {self.actor_endpoints_to_weaver}",
            f"actor endpoints dropped (identity lost): {self.actor_endpoints_dropped}",
            f"unresolved refs kept as legacy ids: {self.unresolved_refs} "
            f"(cross-project {self.cross_project_refs}, dangling {self.dangling_refs})",
            f"succession renamed to supersedes: {self.succession_renamed}",
            f"non-open folios without a status thread: {self.status_without_thread}",
        ]
        for ex in self.merge_examples[:5]:
            lines.append(f"  merged: {ex}")
        for ex in self.dropped_examples[:5]:
            lines.append(f"  dropped actor: {ex}")
        for ex in self.status_examples[:5]:
            lines.append(f"  status-only: {ex}")
        for n in self.notes:
            lines.append(f"note: {n}")
        return "\n".join(lines)


_FOLIO_COLS = ("folio_id", "type", "site_id", "created_at", "created_by",
               "title", "content", "status")
_THREAD_COLS = ("thread_id", "from_id", "to_id", "type", "content", "weaver",
                "created_at")


def _load_sites(sites_dir: Path) -> List[Dict[str, Any]]:
    """Read each ``<site>/metadata.json`` under the sites directory."""
    sites: List[Dict[str, Any]] = []
    if not sites_dir.is_dir():
        return sites
    for meta in sorted(sites_dir.glob("*/metadata.json")):
        try:
            sites.append(json.loads(meta.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return sites


def _resolve_thread_endpoints(
    from_id: Optional[str],
    to_id: Optional[str],
    legacy_weaver: Optional[str],
    store: SkeinNextStore,
    folio_ids: Set[str],
    folio_types: Set[str],
    report: ImportReport,
) -> Tuple[Optional[str], Optional[str], Optional[str], List[str]]:
    """Map a legacy edge to new (from, to, weaver), classifying each endpoint.

    Returns ``(new_from, new_to, weaver, endpoint_kinds)``.
    """
    new_vals: List[Optional[str]] = []
    kinds: List[str] = []
    actors: List[str] = []
    for eid in (from_id, to_id):
        kind, value = classify_endpoint(eid, folio_ids, folio_types)
        kinds.append(kind)
        if kind == "folio":
            new_vals.append(store.resolve_alias(value))
        elif kind == "unresolved":
            new_vals.append(value)
            report.unresolved_refs += 1
            if ":" in value:
                report.cross_project_refs += 1
            else:
                report.dangling_refs += 1
        elif kind == "actor":
            new_vals.append(None)
            actors.append(value)
            report.actor_endpoints_to_weaver += 1
        else:  # none
            new_vals.append(None)

    # The actor is the weaver, not an edge endpoint. Prefer the legacy weaver;
    # fold in actor endpoints. With one weaver slot, any further distinct identity
    # is genuine loss — count and surface it rather than dropping it silently.
    candidates: List[str] = []
    if legacy_weaver:
        candidates.append(legacy_weaver)
    candidates.extend(actors)
    distinct = list(dict.fromkeys(candidates))
    weaver = distinct[0] if distinct else None
    if len(distinct) > 1:
        report.actor_endpoints_dropped += len(distinct) - 1
        if len(report.dropped_examples) < 20:
            report.dropped_examples.append(
                f"kept weaver {distinct[0]!r}, lost {distinct[1:]!r}"
            )
    return new_vals[0], new_vals[1], weaver, kinds


def import_project(
    legacy_db_path: Union[str, Path],
    sites_dir: Union[str, Path],
    store: SkeinNextStore,
) -> ImportReport:
    """Import one legacy SKEIN project into ``store``. Read-only on the source."""
    report = ImportReport(
        source_db=str(legacy_db_path), sites_dir=str(sites_dir)
    )
    conn = open_legacy(legacy_db_path)
    try:
        folio_ids: Set[str] = {
            r[0] for r in conn.execute("SELECT folio_id FROM folios")
        }
        folio_types = _folio_types(conn)

        # --- folios -> hashes + aliases -------------------------------------
        seen_hashes: Set[str] = set()
        with store.transaction():
            for row in conn.execute(
                f"SELECT {','.join(_FOLIO_COLS)} FROM folios"
            ):
                fields = {
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                    "created_by": row["created_by"],
                }
                h = store.create_folio(fields)
                store.set_alias(row["folio_id"], h)
                report.folios_seen += 1
                report.folios_carried += 1
                if h in seen_hashes:
                    report.folio_hash_collisions += 1
                seen_hashes.add(h)

        # --- sites -> site folios + slugs -----------------------------------
        sites = _load_sites(Path(sites_dir))
        with store.transaction():
            for site in sites:
                slug = site.get("site_id")
                if not slug:
                    continue
                site_hash = store.create_folio({
                    "type": "site",
                    "title": slug,
                    "content": site.get("purpose"),
                    "created_at": site.get("created_at"),
                    "created_by": site.get("created_by"),
                })
                store.set_slug(slug, site_hash)
                report.sites_seen += 1
                report.sites_carried += 1

        # --- threads --------------------------------------------------------
        legacy_per_hash: Dict[str, List[str]] = {}
        with store.transaction():
            for row in conn.execute(
                f"SELECT {','.join(_THREAD_COLS)} FROM threads"
            ):
                report.threads_seen += 1
                new_from, new_to, weaver, kinds = _resolve_thread_endpoints(
                    row["from_id"], row["to_id"], row["weaver"],
                    store, folio_ids, folio_types, report,
                )
                ttype = row["type"]
                if ttype == "succession" and all(
                    k in ("folio", "unresolved") for k in kinds
                ):
                    ttype = "supersedes"
                    report.succession_renamed += 1
                th = store.save_thread(
                    from_id=new_from,
                    to_id=new_to,
                    type=ttype,
                    weaver=weaver,
                    created_at=row["created_at"],
                    content=row["content"],
                )
                legacy_per_hash.setdefault(th, []).append(row["thread_id"])

        report.threads_carried = len(legacy_per_hash)
        for h, ids in legacy_per_hash.items():
            if len(ids) > 1:
                report.threads_merged += len(ids) - 1
                if len(report.merge_examples) < 20:
                    report.merge_examples.append(f"{ids} -> {h}")

        # --- within (membership) threads ------------------------------------
        with store.transaction():
            for row in conn.execute("SELECT folio_id, site_id FROM folios"):
                site_id = row["site_id"]
                if not site_id:
                    continue
                site_hash = store.resolve_slug(site_id)
                folio_hash = store.resolve_alias(row["folio_id"])
                if site_hash and folio_hash:
                    store.save_thread(
                        from_id=folio_hash, to_id=site_hash, type="within",
                    )
                    report.within_threads += 1
                else:
                    report.within_unresolved += 1
                    if site_hash is None:
                        report.notes.append(
                            f"folio {row['folio_id']} references site "
                            f"{site_id!r} with no metadata JSON; within edge skipped"
                        )

        # --- status carried-as-threads audit (no silent loss) ---------------
        status_thread_froms: Set[str] = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT from_id FROM threads WHERE type='status'"
            )
        }
        for row in conn.execute("SELECT folio_id, status FROM folios"):
            if row["status"] not in _OPEN_STATUSES and \
                    row["folio_id"] not in status_thread_froms:
                report.status_without_thread += 1
                if len(report.status_examples) < 20:
                    report.status_examples.append(
                        f"{row['folio_id']} status={row['status']!r}"
                    )
        if report.status_without_thread:
            report.notes.append(
                "some non-open folios carry status only in the folios.status "
                "column with no status thread; new-skein omits that column "
                "(status is thread-derived) so this state is not migrated — "
                "see status-only examples above"
            )
    finally:
        conn.close()
    return report
