"""Stage-6 dress rehearsal: the full local round-trip, both roles, ONE build.

login-token -> publish from a real 8001 workbench (the actual routes.py /publish
route over a real ``LogDatabase``) -> station ingress (require_signed=1) verifies
and stores -> the content is queryable and provenance-correct on the station read
surface. The station is bootstrapped with the NEW ``skein station`` ops verbs, and
every server reads its config through the NEW SKEIN_STATION_* env keys — this is
the design §5 Stage-6 exit criterion in one test.

Seams (each pinned elsewhere, substituted here so the ASSEMBLY is what's under
test): the Sigstore ceremony (``signer_from_token`` -> the helpers' fake manifest
signer; the crypto-real path is E1 in test_station_e2e_publish), signature
verification (``signing.verify_multi`` -> VERIFIED as the bound author; guard
firing is pinned by the invite/require-signed suites), and the network hop
(``post_batch`` -> the ingress TestClient; transport totality is pinned in
test_station_env).
"""

from __future__ import annotations

from datetime import datetime, timezone

from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.cli import cli
from skein import publish as _pub
from skein import routes as R
from skein import signing
from skein.models import Folio
from skein.storage import LogDatabase

from tests import station_publish_helpers as h

OP_I, OP_S = "https://accounts.google.com", "op@example.com"
AUTH_I, AUTH_S = h.I, h.ALICE


def _station_cli(corpus, *args):
    return CliRunner().invoke(cli, ["station", "--data-dir", str(corpus), *args])


def _clear_station_env(monkeypatch):
    for suffix in ("DATA_DIR", "ORIGIN", "REQUIRE_SIGNED", "AUTHORITY", "BASE_URL"):
        monkeypatch.delenv(f"SKEIN_STATION_{suffix}", raising=False)
        monkeypatch.delenv(f"SKEIN_NEXT_{suffix}", raising=False)
    monkeypatch.delenv("SKEIN_STATION_NAME", raising=False)
    monkeypatch.delenv("SKEIN_NEXT_PROJECT", raising=False)


def test_full_local_roundtrip(tmp_path, monkeypatch):
    _clear_station_env(monkeypatch)
    corpus = tmp_path / "corpus"

    # --- 1. bootstrap the station with the NEW ops verbs: operator + bound author
    r = _station_cli(corpus, "account", "init-operator", "--issuer", OP_I, "--subject", OP_S)
    assert r.exit_code == 0, r.output
    r = _station_cli(corpus, "account", "add", "--issuer", AUTH_I, "--subject", AUTH_S)
    assert r.exit_code == 0, r.output

    # --- 2. a real workbench holding real content (versions/refs store)
    wb = LogDatabase(tmp_path / "workbench.db")
    folio = Folio(
        folio_id="finding-roundtrip",
        type="finding",
        site_id="proj",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_by="alice",
        title="Roundtrip Design Overview",
        content="the roundtrip body",
    )
    assert wb.save_folio(folio)
    content_hash = folio.content_hash  # save_folio recomputes at the chokepoint
    assert wb.get_version_by_hash(content_hash) is not None

    # --- 3. the station ingress, signed posture, NEW env keys
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(corpus))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    from skein import ingress as ingress_mod

    ingress_client = TestClient(ingress_mod.create_app())

    # --- 4. the seams: verified-as-the-bound-author crypto, in-process transport
    def verified_as_author(cb, b):
        return signing.MultiVerifyResult(
            results=[
                signing.VerifyResult(
                    status=signing.VerifyStatus.VERIFIED, issuer=AUTH_I, subject=AUTH_S
                )
            ],
            overall=signing.VerifyStatus.VERIFIED,
        )

    monkeypatch.setattr(signing, "verify_multi", verified_as_author)
    monkeypatch.setattr(
        _pub, "signer_from_token", lambda token: h.make_signer(AUTH_I, AUTH_S)
    )
    monkeypatch.setattr(
        _pub,
        "post_batch",
        lambda url, batch: ingress_client.post("/publish/v0/folios", json=batch).json(),
    )

    # --- 5. the 8001 workbench app; login-token publish through the REAL route
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_project_log_db] = lambda: wb
    wb_client = TestClient(app)
    resp = wb_client.post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "manifest": {"folios": [content_hash], "threads": []},
            "token": "login-token-from-the-ceremony",
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["signed"] is True and result["sent"] is True
    assert content_hash in result["ack"]["accepted"]

    # --- 6. the read surface over the SAME corpus: queryable + provenance-correct
    monkeypatch.setenv("SKEIN_STATION_NAME", "roundtrip")
    from skein.web.app import create_app as web_create_app

    read_client = TestClient(web_create_app())

    env = read_client.get(f"/folio/{content_hash}.json")
    assert env.status_code == 200
    body = env.json()["body"]
    assert body["content"] == "the roundtrip body"
    assert body["created_by"] == "alice"

    page = read_client.get(f"/folio/{content_hash}")
    assert page.status_code == 200
    assert f"SIGNED — {AUTH_S}" in page.text  # the verified manifest signer
    assert "NOT VERIFIED" not in page.text

    found = read_client.get("/search.json", params={"q": "Roundtrip"})
    assert found.status_code == 200
    assert content_hash in found.text


def test_roundtrip_unbound_signer_is_rejected(tmp_path, monkeypatch):
    """The same loop with NO author binding: the station must refuse the publish
    (the round-trip's authorization half — accepting only bound signers is what
    require_signed means)."""
    _clear_station_env(monkeypatch)
    corpus = tmp_path / "corpus"
    r = _station_cli(corpus, "account", "init-operator", "--issuer", OP_I, "--subject", OP_S)
    assert r.exit_code == 0, r.output
    # deliberately NO `account add` for the author

    wb = LogDatabase(tmp_path / "workbench.db")
    folio = Folio(
        folio_id="finding-unbound",
        type="finding",
        site_id="proj",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        created_by="alice",
        title="Unbound publish",
        content="should not land",
    )
    assert wb.save_folio(folio)

    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(corpus))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    from skein import ingress as ingress_mod

    ingress_client = TestClient(ingress_mod.create_app())

    def verified_as_stranger(cb, b):
        return signing.MultiVerifyResult(
            results=[
                signing.VerifyResult(
                    status=signing.VerifyStatus.VERIFIED, issuer=AUTH_I, subject=AUTH_S
                )
            ],
            overall=signing.VerifyStatus.VERIFIED,
        )

    monkeypatch.setattr(signing, "verify_multi", verified_as_stranger)
    monkeypatch.setattr(
        _pub, "signer_from_token", lambda token: h.make_signer(AUTH_I, AUTH_S)
    )
    monkeypatch.setattr(
        _pub,
        "post_batch",
        lambda url, batch: ingress_client.post("/publish/v0/folios", json=batch).json(),
    )

    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_project_log_db] = lambda: wb
    resp = TestClient(app).post(
        "/publish",
        json={
            "to": "https://ingress.example",
            "manifest": {"folios": [folio.content_hash], "threads": []},
            "token": "login-token",
        },
    )
    assert resp.status_code == 200, resp.text
    ack = resp.json()["ack"]
    assert folio.content_hash not in ack.get("accepted", [])
    rejected = {r_["content_hash"]: r_["reason"] for r_ in ack.get("rejected", [])}
    assert rejected.get(folio.content_hash) == "unbound signer"
