"""The station env-key choke point: SKEIN_STATION_* resolution.

Every station env read goes through ``skein.station_env.station_env`` — the
canonical SKEIN_STATION_* names, resolved from the process environment. An unknown
suffix is a programming error (KeyError), never a silent None (the require_signed
case is why: its unset default is the open posture, so a mistyped key that read as
None would boot the public ingress open).

Also pins the p4n5 #1 carry: a malformed origin becomes a clean config error at
ingress startup (not a raw ValueError traceback) and a PublishError from
post_batch, and that `python -m skein.ingress` exits clean on a config error
rather than dumping a traceback.

The retired ``skein_next`` build's SKEIN_NEXT_* fallback aliases and the
new/legacy conflict machinery were removed in station re-home Stage 8; the tests
that pinned that transition shim went with it.
"""

from __future__ import annotations

import pytest

from skein import station_env as se


def _clear(monkeypatch):
    for k in (
        "SKEIN_STATION_DATA_DIR",
        "SKEIN_STATION_ORIGIN",
        "SKEIN_STATION_REQUIRE_SIGNED",
        "SKEIN_STATION_NAME",
        "SKEIN_STATION_AUTHORITY",
        "SKEIN_STATION_BASE_URL",
    ):
        monkeypatch.delenv(k, raising=False)


# --- resolution rules ---------------------------------------------------------


def test_new_key_wins_without_warning(monkeypatch, recwarn):
    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", "/new")
    assert se.station_env("DATA_DIR") == "/new"
    assert not [w for w in recwarn if issubclass(w.category, FutureWarning)]


def test_neither_set_returns_none(monkeypatch, recwarn):
    _clear(monkeypatch)
    assert se.station_env("DATA_DIR") is None


def test_unknown_suffix_raises_key_error():
    with pytest.raises(KeyError):
        se.station_env("NO_SUCH_KEY")


# --- require_signed drives the boot gate --------------------------------------


def test_require_signed_garbage_config_error(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "onn")
    with pytest.raises(ingress.RequireSignedConfigError):
        ingress.create_app()


def test_require_signed_operator_invariant(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_REQUIRE_SIGNED", "1")
    with pytest.raises(ingress.OperatorInvariantError):
        ingress.create_app()


# --- origin totality (p4n5 #1 carry) ------------------------------------------


def test_malformed_origin_clean_config_error(tmp_path, monkeypatch):
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://h:notaport")
    with pytest.raises(se.StationEnvError) as exc:
        ingress.create_app()
    msg = str(exc.value)
    assert "SKEIN_STATION_ORIGIN" in msg and "notaport" in msg


def test_malformed_ipv6_origin_clean_error(tmp_path, monkeypatch):
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


def test_post_batch_bad_url_publish_error():
    from skein.publish import PublishError, post_batch

    with pytest.raises(PublishError, match="notaport"):
        post_batch("https://h:notaport", {"folios": []})


# --- web read surface ---------------------------------------------------------


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


# --- clean-exit presentation (fell r4) ----------------------------------------


def test_ingress_main_config_error_exits_2(tmp_path, monkeypatch):
    """`python -m skein.ingress` on a misconfigured env exits with a clean
    SystemExit 2, never a raw traceback (fell r4, codex) — the direct-entry twin
    of the launcher's ClickException. A malformed origin is the live trigger
    (post-Stage-8 the new/legacy conflict that used to drive this is gone)."""
    from skein import ingress

    _clear(monkeypatch)
    monkeypatch.setenv("SKEIN_STATION_DATA_DIR", str(tmp_path / ".skein-station"))
    monkeypatch.setenv("SKEIN_STATION_ORIGIN", "https://h:notaport")
    with pytest.raises(SystemExit) as exc:
        ingress.main()
    assert exc.value.code == 2


def test_redeem_2xx_nonjson_publish_error(monkeypatch):
    """A station answering 200 with a non-JSON body must surface as the typed
    PublishError the redeem CLI catches, never a raw JSONDecodeError."""
    import urllib.request

    from skein.publish import PublishError, post_redeem

    class _FakeResp:
        status = 200

        def read(self):
            return b"<html>not json</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _FakeResp())
    with pytest.raises(PublishError, match="invalid response"):
        post_redeem("https://station.example", "tok", {"p": 1})


def test_redeem_2xx_body_drop_publish_error(monkeypatch):
    """A 2xx whose body drops mid-read (IncompleteRead / reset) must be the
    typed PublishError — the success path's twin of the rejection-read guard
    (fell r4: the guard was asymmetric)."""
    import http.client
    import urllib.request

    from skein.publish import PublishError, post_redeem

    class _DropResp:
        status = 200

        def read(self):
            raise http.client.IncompleteRead(b"")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _DropResp())
    with pytest.raises(PublishError, match="connection lost"):
        post_redeem("https://station.example", "tok", {"p": 1})
