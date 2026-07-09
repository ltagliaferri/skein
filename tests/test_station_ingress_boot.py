"""Ingress startup invariant — require_signed + the single-active-operator gate (D13-D20,
finding-8), re-homed from the create_app cells of skein_next/tests/test_cli_account.py.

These pin the ingress-side boot behavior (gotcha #4 of the Stage-3 brief): under
require_signed, create_app refuses boot unless EXACTLY ONE active operator exists in the
account_bindings sidecar, and an unrecognized SKEIN_NEXT_REQUIRE_SIGNED value refuses boot
rather than silently running open. Operators are seeded directly through the store here —
the operator/invite `account` CLI verbs ride with Stage 5/6 (test_cli_account); this suite
owns only the ingress create_app invariant.
"""

from __future__ import annotations

import logging

import pytest

from skein import ingress
from skein.station import Station

I, S = "https://accounts.google.com", "operator@example.com"


def _seed_operator(data_dir, issuer=I, subject=S):
    st = Station(data_dir)
    try:
        st.store.add_binding(issuer, subject, role="operator",
                             vouched_by_issuer=issuer, vouched_by_subject=subject)
    finally:
        st.close()


def _wire_env(tmp_path, monkeypatch, value):
    monkeypatch.setenv(ingress.ENV_DATA_DIR, str(tmp_path / ".skein-next"))
    if value is None:
        monkeypatch.delenv(ingress.ENV_REQUIRE_SIGNED, raising=False)
    else:
        monkeypatch.setenv(ingress.ENV_REQUIRE_SIGNED, value)
    return tmp_path / ".skein-next"


def test_create_app_require_signed_without_operator_refuses(tmp_path, monkeypatch):  # D13
    d = _wire_env(tmp_path, monkeypatch, "1")
    Station(d).close()  # empty corpus, no operator
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "account init-operator" in str(exc.value)


def test_create_app_require_signed_with_operator_starts(tmp_path, monkeypatch):  # D14
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    assert ingress.create_app() is not None


def test_require_signed_off_no_operator_ok(tmp_path, monkeypatch):  # D15
    d = _wire_env(tmp_path, monkeypatch, None)
    Station(d).close()
    assert ingress.create_app() is not None


def test_operator_identity_sourced_from_sidecar(tmp_path, monkeypatch):  # D16
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    assert ingress.create_app() is not None
    st = Station(d)
    try:
        assert st.store.get_operator().subject == S
    finally:
        st.close()


def test_create_app_require_signed_multiple_operators_refuses(tmp_path, monkeypatch):  # D20
    d = _wire_env(tmp_path, monkeypatch, "1")
    st = Station(d)
    try:
        st.store.add_binding("https://idpA", "first", role="operator")
        st.store.add_binding("https://idpB", "second", role="operator")
    finally:
        st.close()
    with pytest.raises(ingress.OperatorInvariantError) as exc:
        ingress.create_app()
    assert "single-active-operator" in str(exc.value) or "active operators" in str(exc.value)


def test_ingress_startup_logs_operator_status(tmp_path, monkeypatch, caplog):  # D18
    d = _wire_env(tmp_path, monkeypatch, "1")
    _seed_operator(d)
    with caplog.at_level(logging.INFO, logger="skein.ingress"):
        ingress.create_app()
    assert any("operator" in rec.message for rec in caplog.records)


def test_create_app_require_signed_on_spelling_still_enforces(tmp_path, monkeypatch):  # finding-8
    """A wider truthy spelling (e.g. 'on') must drive the SAME startup invariant as
    '1' — no operator present must still refuse boot, not silently run open."""
    d = _wire_env(tmp_path, monkeypatch, "on")
    Station(d).close()
    with pytest.raises(ingress.OperatorInvariantError):
        ingress.create_app()
    _seed_operator(d)
    assert ingress.create_app() is not None  # with an operator present, boots fine


def test_create_app_require_signed_garbage_value_refuses_boot(tmp_path, monkeypatch):  # finding-8
    """An unrecognized SKEIN_NEXT_REQUIRE_SIGNED value must refuse to boot at all — never
    silently fall back to require_signed=False and accept unsigned content wide open."""
    _wire_env(tmp_path, monkeypatch, "onn")
    with pytest.raises(ingress.RequireSignedConfigError):
        ingress.create_app()
