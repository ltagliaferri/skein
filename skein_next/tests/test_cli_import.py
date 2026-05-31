"""Tests for the ``interskein import`` / ``verify`` CLI verbs (Click CliRunner).

Builds a synthetic legacy SKEIN project on disk in the real layout a cutover
sees — ``PROJECT_ROOT/.skein/data/skein.db`` plus ``PROJECT_ROOT/.skein/data/
sites/<slug>/metadata.json`` — then exercises:

- ``import PROJECT_ROOT --verify`` succeeds, prints FIDELITY OK, and the target
  ``.skein-next`` then serves the imported folios;
- the fidelity gate exits non-zero on a forced discrepancy (collision / dropped
  actor identity), for both ``import --verify`` and the standalone ``verify``;
- re-importing into the same data dir is idempotent;
- path derivation, explicit overrides, and the missing-source error.

The legacy schema + corpus are reused from ``test_bridge`` so the fixture mirrors
exactly what the bridge reads.
"""

import pytest
from click.testing import CliRunner

from skein_next.bridge import ImportReport
from skein_next.cli import cli
from skein_next.store import SkeinNextStore
from skein_next.tests.test_bridge import (
    FOLIOS,
    SITES,
    THREADS,
    make_legacy_db,
    make_sites_dir,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project_root(tmp_path):
    """A legacy project dir: P/.skein/data/skein.db + P/.skein/data/sites/."""
    root = tmp_path / "legacyproj"
    data = root / ".skein" / "data"
    data.mkdir(parents=True)
    make_legacy_db(data / "skein.db", FOLIOS, THREADS)
    make_sites_dir(data, SITES)  # -> P/.skein/data/sites/<slug>/metadata.json
    return root


@pytest.fixture
def target(tmp_path):
    return str(tmp_path / ".skein-next")


# --- happy path: import, verify, then serve the imported data ---------------


def test_import_verify_passes_and_serves_folios(runner, project_root, target):
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(project_root), "--verify"]
    )
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output

    # The target store now resolves the imported legacy ids to content hashes.
    with SkeinNextStore(target) as store:
        h = store.resolve_alias("brief-20260101-aaaa")
        assert h is not None and h.startswith("sha256::")
        assert store.get_folio(h)["title"] == "First brief"

    # And the read verb serves it via the legacy-id alias.
    r2 = runner.invoke(cli, ["--data-dir", target, "folio", "brief-20260101-aaaa"])
    assert r2.exit_code == 0, r2.output
    assert "First brief" in r2.output


def test_import_surfaces_expected_non_failing_counts(runner, project_root, target):
    """The expected (non-gated) outcomes are printed, not silent."""
    r = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r.exit_code == 0, r.output
    # succession->supersedes, unresolved-ref breakdown, and actor folds all show.
    assert "succession renamed to supersedes: 1" in r.output
    assert (
        "unresolved refs kept as legacy ids: 2 occurrences "
        "(2 distinct; cross-project 1, dangling 1)" in r.output
    )
    assert "actor endpoints folded to weaver (lossless): 3" in r.output
    # without --verify there is no fidelity verdict line
    assert "FIDELITY" not in r.output


# --- fidelity gate fails on a forced discrepancy ----------------------------


def test_verify_flag_fails_on_hash_collision(runner, project_root, target, monkeypatch):
    """A non-zero folio_hash_collisions trips the gate and exits non-zero."""
    from skein_next import cli as cli_mod

    def fake_import(db_path, sites_dir, store):
        rep = ImportReport(source_db=str(db_path))
        rep.folios_seen = rep.folios_carried = 3
        rep.sites_seen = rep.sites_carried = 2
        rep.folio_hash_collisions = 1
        return rep

    monkeypatch.setattr(cli_mod, "import_project", fake_import)
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(project_root), "--verify"]
    )
    assert r.exit_code != 0
    assert "FIDELITY FAILED" in r.output
    assert "folio_hash_collisions == 0" in r.output


def test_verify_command_fails_on_dropped_actor(runner, project_root, target, monkeypatch):
    """A dropped actor identity (must stay 0) trips the standalone verify gate."""
    from skein_next import cli as cli_mod

    def fake_import(db_path, sites_dir, store):
        rep = ImportReport(source_db=str(db_path))
        rep.folios_seen = rep.folios_carried = 3
        rep.sites_seen = rep.sites_carried = 2
        rep.actor_endpoints_dropped = 1
        rep.dropped_examples = ["agent-x relationship lost"]
        return rep

    monkeypatch.setattr(cli_mod, "import_project", fake_import)
    r = runner.invoke(cli, ["--data-dir", target, "verify", str(project_root)])
    assert r.exit_code != 0
    assert "FIDELITY FAILED" in r.output
    assert "actor_endpoints_dropped == 0" in r.output


def test_verify_command_passes_on_clean_import(runner, project_root, target):
    r = runner.invoke(cli, ["--data-dir", target, "verify", str(project_root)])
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output


# --- idempotency ------------------------------------------------------------


def test_import_twice_is_idempotent(runner, project_root, target):
    r1 = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r1.exit_code == 0, r1.output
    with SkeinNextStore(target) as store:
        first = (store.count_folios(), store.count_threads(), store.list_slugs())

    r2 = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r2.exit_code == 0, r2.output
    with SkeinNextStore(target) as store:
        second = (store.count_folios(), store.count_threads(), store.list_slugs())

    assert first == second


# --- path derivation / overrides / errors -----------------------------------


def test_import_requires_root_or_overrides(runner, target):
    r = runner.invoke(cli, ["--data-dir", target, "import"])
    assert r.exit_code != 0
    assert "give a PROJECT_ROOT" in r.output


def test_import_with_explicit_overrides(runner, project_root, target):
    db = str(project_root / ".skein" / "data" / "skein.db")
    sd = str(project_root / ".skein" / "data" / "sites")
    r = runner.invoke(
        cli,
        ["--data-dir", target, "import", "--legacy-db", db, "--sites-dir", sd,
         "--verify"],
    )
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output


def test_import_missing_db_errors(runner, tmp_path, target):
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(tmp_path / "nonexistent")]
    )
    assert r.exit_code != 0
    assert "no legacy database" in r.output
