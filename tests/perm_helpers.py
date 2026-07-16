"""Shared helpers for the permission-model tests (rev 6, brief-20260716-u73x).

Builds a real StationStore corpus with SIGNED folios (attribution rows so ownership
predicates resolve), supersedes lineages, tier bindings, and per-document grants.
"""

from __future__ import annotations

import hashlib

from skein.authorization import Principal
from skein.station_store import StationStore

TS = "2026-07-16T00:00:00+00:00"
I = "https://accounts.google.com"  # noqa: E741 (matches the codebase issuer-constant convention)

OWNER = Principal(I, "alice@example.com")
BOB = Principal(I, "bob@example.com")
STEWARD = Principal(I, "steward@example.com")
ADMIN = Principal(I, "admin@example.com")
OPERATOR = Principal(I, "op@example.com")


def make_store(tmp_path) -> StationStore:
    return StationStore(tmp_path / ".skein-station")


def _root_for(signer: Principal) -> str:
    """A stable per-signer manifest root (one manifest per signer suffices here)."""
    return "sha256::" + hashlib.sha256(signer.subject.encode()).hexdigest()


def folio(store, title, *, created_by="x", ftype="finding", content="body") -> str:
    return store.create_folio(
        {
            "type": ftype,
            "title": title,
            "content": content,
            "created_at": TS,
            "created_by": created_by,
        }
    )


def sign(store, content_hash, signer: Principal, *, kind="folio") -> None:
    """Attach a covering-manifest attribution for ``signer`` (manifest row first —
    attribution FK-references it). After this, owns_folio/lineage_owner resolve to
    ``signer`` for ``content_hash``."""
    root = _root_for(signer)
    mh = "sha256::" + hashlib.sha256((root + content_hash).encode()).hexdigest()
    store.add_manifest(root, mh, "{}", "[]", "{}", signer.issuer, signer.subject, 1)
    store.add_constituent_attribution(content_hash, kind, root, signer.issuer, signer.subject)


def signed_folio(store, signer: Principal, title, **kw) -> str:
    h = folio(store, title, **kw)
    sign(store, h, signer)
    return h


def bind(store, signer: Principal, role: str) -> None:
    store.add_binding(
        signer.issuer, signer.subject, role=role,
        vouched_by_issuer=OPERATOR.issuer, vouched_by_subject=OPERATOR.subject,
    )


def supersede(store, new, old) -> None:
    store.save_thread(from_id=new, to_id=old, type="supersedes", created_at=TS)


def grant(store, anchor, grantee: Principal, kind, vouched_by: Principal = ADMIN) -> None:
    store.add_grant(
        anchor, grantee.issuer, grantee.subject, kind,
        vouched_by.issuer, vouched_by.subject,
    )


def thread(ttype, from_id, to_id):
    return {"type": ttype, "from_id": from_id, "to_id": to_id}
