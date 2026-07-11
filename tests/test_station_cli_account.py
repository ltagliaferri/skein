"""Station-ops CLI: account verbs (D1-D12, D19), read-app exemption (D17), and
the verify-cache backfill verb (VC9).

Re-homed from skein_next/tests/test_cli_account.py (station re-home Stage 6) onto
the ``skein station`` group. The startup-invariant tests that never touched the
CLI (D13-D16, D18, D20, finding-8 x2) were ported at Stage 3 and live in
tests/test_station_ingress_boot.py; D17 and VC9 ride here with the CLI verbs
that exercise them. Operator/author verbs are direct-store admin on the corpus
data dir (they must run before a signed ingress can boot), invoked as
``skein station --data-dir DIR account ...``.
"""

from __future__ import annotations

from click.testing import CliRunner

from client.cli import cli
from skein.station import Station


ISS, S = "https://accounts.google.com", "op@example.com"
ISS2, S2 = "https://accounts.google.com", "author2@example.com"


def _run(tmp_path, *args):
    return CliRunner().invoke(
        cli, ["station", "--data-dir", str(tmp_path / ".skein-station"), *args]
    )


def _open(tmp_path):
    return Station(tmp_path / ".skein-station")


# --- D1-D3: init-operator ---------------------------------------------------


def test_init_operator_happy(tmp_path):  # D1
    r = _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        op = st.store.get_operator()
        assert (op.issuer, op.subject) == (ISS, S)
        assert [e["event"] for e in st.store.get_binding_events()] == ["created"]


def test_init_operator_double_init_refuses(tmp_path):  # D2
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "init-operator", "--issuer", "https://x", "--subject", "y")
    assert r.exit_code != 0 and "OperatorAlreadyBootstrapped" in r.output
    with _open(tmp_path) as st:
        assert st.store.count_active_operators() == 1


def test_init_operator_after_revoke_succeeds(tmp_path):  # D3
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    with _open(tmp_path) as st:
        st.store.revoke_binding(ISS, S)
    r = _run(tmp_path, "account", "init-operator", "--issuer", "https://x", "--subject", "y")
    assert r.exit_code == 0


# --- D4-D5: add -------------------------------------------------------------


def test_account_add_happy(tmp_path):  # D4
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "add", "--issuer", ISS2, "--subject", S2, "--role", "author")
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        b = st.store.get_binding(ISS2, S2)
        assert b.role == "author" and b.revoked_at is None
        assert b.vouched_by_subject == S
        assert "created" in [e["event"] for e in st.store.get_binding_events(ISS2, S2)]


def test_account_add_requires_existing_operator(tmp_path):  # D5
    r = _run(tmp_path, "account", "add", "--issuer", ISS2, "--subject", S2)
    assert r.exit_code != 0 and "no operator" in r.output


def test_account_add_on_active_operator_echoes_stored_role(tmp_path):
    """`account add --role author` on the ACTIVE OPERATOR's own identity hits
    add_binding's already-active idempotent no-op: the binding correctly STAYS
    operator. The CLI must echo the STORED role ("operator"), not the requested
    one ("author")."""
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "add", "--issuer", ISS, "--subject", S, "--role", "author")
    assert r.exit_code == 0
    assert f"operator {ISS}/{S}" in r.output
    assert "author" not in r.output  # the requested role is NOT echoed
    with _open(tmp_path) as st:
        b = st.store.get_binding(ISS, S)
        assert b.role == "operator" and b.revoked_at is None  # unchanged in the store


# --- D6-D8: revoke ----------------------------------------------------------


def test_account_revoke_happy(tmp_path):  # D6
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", ISS2, "--subject", S2)
    r = _run(tmp_path, "account", "revoke", "--issuer", ISS2, "--subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert st.store.get_binding(ISS2, S2).revoked_at is not None
        assert "revoked" in [e["event"] for e in st.store.get_binding_events(ISS2, S2)]


def test_account_revoke_nonexistent_errors(tmp_path):  # D7
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "revoke", "--issuer", "https://x", "--subject", "ghost")
    assert r.exit_code != 0 and "no active binding for https://x/ghost" in r.output


def test_account_revoke_active_operator_refused(tmp_path):  # D8
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "revoke", "--issuer", ISS, "--subject", S)
    assert r.exit_code != 0 and "refusing to revoke the active operator" in r.output
    with _open(tmp_path) as st:
        assert st.store.get_operator() is not None  # unchanged


# --- D9-D11, D19: rotate-operator -------------------------------------------


def test_rotate_operator_atomic(tmp_path):  # D9
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", ISS2, "--new-subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        assert (st.store.get_operator().issuer, st.store.get_operator().subject) == (ISS2, S2)
        events = [e["event"] for e in st.store.get_binding_events()]
        assert "rotated_out" in events and "rotated_in" in events
        from skein.authorization import Principal, can_write
        assert can_write(Principal(ISS, S), st.store) is False
        assert can_write(Principal(ISS2, S2), st.store) is True


def test_rotate_operator_requires_existing_operator(tmp_path):  # D10
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", ISS2, "--new-subject", S2)
    assert r.exit_code != 0 and "no operator to rotate" in r.output


def test_rotate_operator_is_all_or_nothing(tmp_path, monkeypatch):  # D11
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    # force the new-operator install to fail mid-rotation
    from skein.station_store import StationStore
    orig = StationStore.add_binding

    def boom(self, *a, **k):
        if k.get("event") == "rotated_in":
            raise RuntimeError("install failed")
        return orig(self, *a, **k)

    monkeypatch.setattr(StationStore, "add_binding", boom)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", ISS2, "--new-subject", S2)
    assert r.exit_code != 0
    with _open(tmp_path) as st:
        op = st.store.get_operator()
        assert (op.issuer, op.subject) == (ISS, S)  # old operator still active (rollback)


def test_rotate_operator_onto_existing_active_author_promotes(tmp_path):  # D19
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", ISS2, "--subject", S2)
    with _open(tmp_path) as st:
        t0 = st.store.get_binding(ISS2, S2).created_at
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", ISS2, "--new-subject", S2)
    assert r.exit_code == 0
    with _open(tmp_path) as st:
        b = st.store.get_binding(ISS2, S2)
        assert b.role == "operator" and b.created_at == t0  # promoted, created_at preserved
        assert st.store.count_active_operators() == 1
        assert "promoted" in [e["event"] for e in st.store.get_binding_events(ISS2, S2)]


def test_rotate_operator_onto_self_is_refused_no_audit_mutation(tmp_path):  # harden D
    """Rotating onto the CURRENT operator must refuse before any mutation, not
    revoke-then-re-promote the same identity (which churns the audit trail with a
    spurious rotated_out + promoted)."""
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    r = _run(tmp_path, "account", "rotate-operator", "--new-issuer", ISS, "--new-subject", S)
    assert r.exit_code != 0 and "onto itself" in r.output
    with _open(tmp_path) as st:
        assert st.store.count_active_operators() == 1
        # the audit trail shows ONLY the original creation — no rotated_out/promoted
        assert [e["event"] for e in st.store.get_binding_events(ISS, S)] == ["created"]


# --- D12: list --------------------------------------------------------------


def test_account_list_plain_text(tmp_path):  # D12
    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    _run(tmp_path, "account", "add", "--issuer", ISS2, "--subject", S2)
    r = _run(tmp_path, "account", "list")
    assert r.exit_code == 0
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert f"operator {ISS}/{S}" in lines
    assert f"author {ISS2}/{S2}" in lines
    assert "|" not in r.output and "<" not in r.output  # no tables/markdown
    # revoked excluded by default; included with --all
    _run(tmp_path, "account", "revoke", "--issuer", ISS2, "--subject", S2)
    assert f"author {ISS2}/{S2}" not in _run(tmp_path, "account", "list").output
    assert "(revoked)" in _run(tmp_path, "account", "list", "--all").output


# --- D17: read-app exemption from the operator boot invariant ----------------


def test_read_app_starts_without_operator_under_require_signed(tmp_path, monkeypatch):  # D17
    # The READ web app serves regardless of operator-count; the startup refusal is
    # the INGRESS's alone. It does NOT suppress folio_verdict's live binding step.
    from skein.web import app as web_app

    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    monkeypatch.setenv("SKEIN_STATION_NAME", "interskein")
    Station(tmp_path / ".skein-station").close()  # empty corpus, no operator
    app = web_app.create_app()
    assert app is not None


# --- VC9: verify-cache backfill ---------------------------------------------


def test_maintenance_verify_cache_backfills(tmp_path, monkeypatch):  # VC9
    from skein import signing, wire
    from skein.ingress import ingest
    from tests import station_publish_helpers as h

    _run(tmp_path, "account", "init-operator", "--issuer", ISS, "--subject", S)
    instance = Station(tmp_path / ".skein-station")
    instance.store.add_binding(ISS, S, role="author")

    # Client content built directly (the skein_next authoring verbs are DROP):
    # the canonical specs set, manifest-signed by a fake signer under ISS/S.
    folios, threads, slugs = h.specs_set()
    batch = wire.build_batch(folios, threads, slugs)
    batch["manifest_signature"] = h.manifest_over(folios, threads, signer=h.make_signer(ISS, S))

    def ok(cb, b):
        return signing.MultiVerifyResult(
            results=[
                signing.VerifyResult(
                    status=signing.VerifyStatus.VERIFIED, issuer=ISS, subject=S
                )
            ],
            overall=signing.VerifyStatus.VERIFIED,
        )

    ingest(instance, batch, verifier=ok, require_signed=True)
    # clear the cache the ingress wrote, then backfill
    instance.store.conn.execute("DELETE FROM verify_cache")
    instance.store.conn.commit()
    monkeypatch.setattr(signing, "verify_multi", ok)
    r = _run(tmp_path, "maintenance", "verify-cache")
    assert r.exit_code == 0 and "backfilled 1" in r.output
    assert (
        instance.store.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()[0] == 1
    )
    # idempotent second run
    assert _run(tmp_path, "maintenance", "verify-cache").exit_code == 0
    instance.close()
