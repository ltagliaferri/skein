"""Account authorization — who may write to a mesh instance (Thread F1 / 81fm §E).

Orthogonal to signing. A manifest's signature proves *who* signed (a verified
Sigstore ``issuer``/``subject`` pair); authorization asks whether that verified
identity is a *bound, non-revoked author* on THIS instance. The two compose: the
ingress admits a constituent only when its covering manifest verifies AND the
manifest signer is bound here (``can_write``), and the read verdict recomputes the
same binding live per read.

The only gateable thing is the cert identity (the ``Principal``). ``created_by``
on a folio and ``weaver`` on a thread are unsigned display content — never an
authorization input (A5). Bindings live in the per-instance ``account_bindings``
sidecar (the :class:`~skein.station_store.StationStore` methods); this module owns
the pure predicate (``can_write``) and the bootstrap policy (``bootstrap_operator``)
over a :class:`BindingStore`, so they unit-test against an in-memory fake with no
crypto and no DB.

Re-homed from ``skein_next/authorization.py`` (station re-home Stage 3); the pure
predicate + bootstrap policy are unchanged, only the ``BindingStore`` provider
moved from ``SkeinNextStore`` to ``StationStore``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Optional, Protocol

# The station tier vocabulary, top-down (rev 6 §2). Built day-one:
# operator, administrator, steward, originator. Reserved-but-unbuilt:
# contributor, curator, reader (deliberately absent so an unbuilt tier
# never satisfies any check). Backs the DB role CHECK and the CLI Choices.
STATION_ROLES = ("operator", "administrator", "steward", "originator")

# The tiers a WIRE invite may bind on redeem. ``administrator`` is a privileged
# LOCAL bind only (station account add), and ``operator`` is NEVER wire-redeemable
# (the single-active-operator invariant, A7/D13) — so a self-service redeem may
# install ONLY these two (§2). The redeem cheap pre-check AND the authoritative
# redeem_invite_cas backstop both gate on this one set.
WIRE_REDEEMABLE_ROLES = frozenset({"originator", "steward"})


@dataclass(frozen=True)
class Principal:
    """A verified cert identity — the only authorization-gateable thing.

    Built from an already-verified ``{"issuer", "subject"}`` (A9); this module
    never parses a cert. A non-VERIFIED result yields no Principal and never
    reaches :func:`can_write`."""

    issuer: str
    subject: str


@dataclass(frozen=True)
class Binding:
    """An account binding row: a (issuer, subject) authorized as operator or author.

    ``revoked_at`` NULL means active. ``created_at`` is the immutable first-insert
    timestamp (preserved across revoke/reactivate, B5). ``vouched_by_*`` records
    who authorized the binding (self for the bootstrapped operator)."""

    issuer: str
    subject: str
    role: str  # 'operator' | 'author'
    vouched_by_issuer: Optional[str]
    vouched_by_subject: Optional[str]
    created_at: Optional[str]
    revoked_at: Optional[str]


class OperatorAlreadyBootstrapped(Exception):
    """An attempt to bootstrap a second active operator (the single-operator
    invariant; refuse iff an ACTIVE operator already exists, A7)."""


class BindingStore(Protocol):
    """The binding surface ``can_write`` / ``bootstrap_operator`` consume.

    Satisfied structurally by :class:`~skein.station_store.StationStore` (its
    account_bindings methods) and by the in-memory fake the unit cells use."""

    def get_binding(self, issuer: str, subject: str) -> Optional[Binding]: ...

    def get_operator(self) -> Optional[Binding]: ...

    def add_binding(
        self,
        issuer: str,
        subject: str,
        role: str,
        vouched_by_issuer: Optional[str] = None,
        vouched_by_subject: Optional[str] = None,
    ) -> Binding: ...

    def transaction(self) -> AbstractContextManager["BindingStore"]: ...


def can_write(actor: Principal, bindings: BindingStore) -> bool:
    """True iff ``actor`` has an ACTIVE binding (exists AND ``revoked_at`` is NULL).

    Keys on the verified cert (issuer, subject) PAIR — same email under a
    different IdP inherits nothing (A4). Has no ``created_by`` parameter: the
    function never receives unsigned display content (A5)."""
    binding = bindings.get_binding(actor.issuer, actor.subject)
    return binding is not None and binding.revoked_at is None


def bootstrap_operator(bindings: BindingStore, operator: Principal) -> Binding:
    """Install the self-vouched root operator, refusing a second active one.

    The existence-check and the insert run inside ONE write transaction (the
    store's ``BEGIN IMMEDIATE``), so two concurrent callers serialize on the
    write lock instead of both observing "no operator yet" — a bare
    check-then-insert let 8 concurrent ``init-operator`` invocations against an
    empty store install 4 active operators (probe-confirmed) before this fix.
    Refuse iff an ACTIVE operator already exists (A7); allowed once the prior
    operator is revoked (A8). The bootstrapped row vouches for itself (A6)."""
    with bindings.transaction():
        if bindings.get_operator() is not None:
            raise OperatorAlreadyBootstrapped(
                "an active operator already exists; refusing to bootstrap a second"
            )
        return bindings.add_binding(
            operator.issuer,
            operator.subject,
            role="operator",
            vouched_by_issuer=operator.issuer,
            vouched_by_subject=operator.subject,
        )


def default_bindings(store) -> BindingStore:
    """The default BindingStore for the ingress/CLI: the station's own store.

    ``StationStore`` implements the BindingStore surface directly, so this is
    the identity wrapper that names the intent at call sites (Thread E CD6/C13)."""
    return store
