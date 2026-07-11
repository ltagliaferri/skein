"""The station env-key choke point: SKEIN_STATION_* with SKEIN_NEXT_* aliases.

Station re-home Stage 6 (design §5 Stage 6, §10 #5). Every station env read goes
through ``skein.station_env.station_env`` — new canonical SKEIN_STATION_* names,
with the retired build's SKEIN_NEXT_* keys accepted as fallback aliases until
skein_next is deleted (Stage 8 removes the alias table; nothing else may spell
the old names). The transition rules are deliberately loud:

- a legacy key alone works, with a FutureWarning naming the new key;
- new + legacy set to DIFFERENT values refuses with StationEnvError — a
  half-configured box must never pick a posture silently (the require_signed
  case is the one that matters: a silent pick could boot the public ingress
  open);
- SKEIN_NEXT_PROJECT maps to SKEIN_STATION_NAME (the "project" wording is
  deprecated).

Also pins the p4n5 #1 carry: a malformed origin becomes a clean config error at
ingress startup (not a raw ValueError traceback) and a PublishError from
post_batch.
"""

from __future__ import annotations

import pytest

from skein import station_env as se


def _clear(monkeypatch):
    for k in (
        "SKEIN_STATION_DATA_DIR", "SKEIN_NEXT_DATA_DIR",
        "SKEIN_STATION_ORIGIN", "SKEIN_NEXT_ORIGIN",
        "SKEIN_STATION_REQUIRE_SIGNED", "SKEIN_NEXT_REQUIRE_SIGNED",
        "SKEIN_STATION_NAME", "SKEIN_NEXT_PROJECT",
        "SKEIN_STATION_AUTHORITY", "SKEIN_NEXT_AUTHORITY",
        "SKEIN_STATION_BASE_URL", "SKEIN_NEXT_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)


# --- resolution rules ---------------------------------------------------------


def test_new_key_wins_without_warning(monkeypatch, recwarn):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", "/new")
    assert se.station_env("DATA_DIR") == "/new"
    assert not [w for w in recwarn if issubclass(w.category, FutureWarning)]


def test_legacy_key_falls_back_with_warning(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_NEXT_DATA_DIR", "/old")
    with pytest.warns(FutureWarning, match="SKEIN_NEXT_DATA_DIR.*SKEIN_STATION_DATA_DIR"):
        assert se.station_env("DATA_DIR") == "/old"


def test_legacy_project_maps_to_name(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_NEXT_PROJECT", "interskein")
    with pytest.warns(FutureWarning, match="SKEIN_NEXT_PROJECT.*SKEIN_STATION_NAME"):
        assert se.station_env("NAME") == "interskein"


def test_conflicting_keys_refuse(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", "/new")
    monkeypatch.setenv("SKEIN_NEXT_DATA_DIR", "/old")
    with pytest.raises(se.StationEnvError) as exc:
        se.station_env("DATA_DIR")
    msg = str(exc.value)
    assert "SKEIN_STATION_DATA_DIR" in msg and "SKEIN_NEXT_DATA_DIR" in msg


def test_both_set_identical_returns_value_and_nudges(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", "/same")
    monkeypatch.setenv("SKEIN_NEXT_DATA_DIR", "/same")
    with pytest.warns(FutureWarning, match="SKEIN_NEXT_DATA_DIR"):
        assert se.station_env("DATA_DIR") == "/same"


def test_neither_set_returns_none(monkeypatch, recwarn):
    _clear(monkeypatch)
    assert se.station_env("DATA_DIR") is None


def test_unknown_suffix_is_a_programming_error():
    with pytest.raises(KeyError):
        se.station_env("NO_SUCH_KEY")


# --- require_signed drives the boot gate through the shim ---------------------


def test_require_signed_new_key_garbage_refuses_boot(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "onn")
    with pytest.raises(ingress.RequireSignedConfigError):
        ingress.create_app()


def test_require_signed_new_key_enforces_operator_invariant(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    with pytest.raises(ingress.OperatorInvariantError):
        ingress.create_app()


def test_require_signed_legacy_key_still_enforces(tmp_path, monkeypatch):
    """The alias path must drive the SAME gate: a box still exporting only the
    old key keeps its signed posture (never silently boots open)."""
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_NEXT_REQUIRE_SIGNED", "1")
    with pytest.warns(FutureWarning):
        with pytest.raises(ingress.OperatorInvariantError):
            ingress.create_app()


def test_require_signed_conflicting_keys_refuse_boot(tmp_path, monkeypatch):
    """new=0 old=1 (or any disagreement) refuses boot. The half-configured box
    must never pick the open posture by precedence."""
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "0")
    monkeypatch.setenv("SKEIN_NEXT_REQUIRE_SIGNED", "1")
    with pytest.raises(se.StationEnvError):
        ingress.create_app()


# --- origin totality (p4n5 #1 carry) ------------------------------------------


def test_malformed_origin_is_a_clean_config_error(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://h:notaport")
    with pytest.raises(se.StationEnvError) as exc:
        ingress.create_app()
    msg = str(exc.value)
    assert "SKEIN_STATION_ORIGIN" in msg and "notaport" in msg


def test_malformed_ipv6_origin_is_a_clean_config_error(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://[::1/")
    with pytest.raises(se.StationEnvError):
        ingress.create_app()


def test_valid_noncanonical_origin_boots(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "HTTPS://Interskein.com:443/")
    assert ingress.create_app() is not None


def test_post_batch_malformed_url_raises_publish_error():
    from skein.publish import PublishError, post_batch

    with pytest.raises(PublishError, match="notaport"):
        post_batch("https://h:notaport", {"folios": []})


# --- web read surface resolves through the shim --------------------------------


def test_web_reads_new_keys(tmp_path, monkeypatch):
    from skein.web import app as web_app

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SKEIN_STATION_AUTHORITY", "interskein.com")
    monkeypatch.setenv("SKEIN_STATION_BASE_URL", "https://interskein.com")
    assert web_app.get_data_dir() == str(tmp_path / "d")
    assert web_app.get_authority() == "interskein.com"
    # configured base URL returns early — the request is never touched
    assert web_app.public_base_url(None) == "https://interskein.com"


def test_web_legacy_keys_still_read(tmp_path, monkeypatch):
    from skein.web import app as web_app

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_NEXT_AUTHORITY", "interskein.com")
    with pytest.warns(FutureWarning):
        assert web_app.get_authority() == "interskein.com"


def test_web_conflicting_display_keys_refuse_at_boot(tmp_path, monkeypatch):
    """AUTHORITY/BASE_URL are read per-request, so without a boot check a
    new/legacy conflict would boot a healthy-looking read server that 500s
    every content page. create_app must refuse at startup instead (fell r1)."""
    from skein.station import Station
    from skein.web import app as web_app

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SKEIN_STATION_NAME", "conflicted")
    Station(tmp_path / "d").close()  # a bootable (empty) corpus
    monkeypatch.setenv("SKEIN_STATION_BASE_URL", "https://interskein.com")
    monkeypatch.setenv("SKEIN_NEXT_BASE_URL", "https://www.interskein.com")
    with pytest.raises(se.StationEnvError):
        web_app.create_app()
    # same refusal for the AUTHORITY pair
    monkeypatch.delenv("SKEIN_NEXT_BASE_URL")
    monkeypatch.setenv("SKEIN_STATION_AUTHORITY", "interskein.com")
    monkeypatch.setenv("SKEIN_NEXT_AUTHORITY", "other.example")
    with pytest.raises(se.StationEnvError):
        web_app.create_app()
    # and the direct run_server path exits CLEAN (SystemExit 2), never a raw
    # traceback — the choke point's stated contract on the python -m path.
    with pytest.raises(SystemExit) as exc:
        web_app.run_server()
    assert exc.value.code == 2
