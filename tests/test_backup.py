"""Tests for SKEIN backup and recovery."""

import json
import os
import shutil
import sqlite3
import stat
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from client.backup import BackupManager


def _make_project(projects_root: Path, name: str, db_size_rows: int = 10) -> Path:
    """Create a fake ~/projects/<name>/.skein/data layout with a populated db."""
    data_dir = projects_root / name / ".skein" / "data"
    data_dir.mkdir(parents=True)

    # Populate skein.db with a real, integrity-clean sqlite db.
    conn = sqlite3.connect(str(data_dir / "skein.db"))
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(db_size_rows)],
    )
    conn.commit()
    conn.close()

    # Add the canonical sibling pieces.
    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sites").mkdir()
    site = data_dir / "sites" / name
    site.mkdir()
    (site / "metadata.json").write_text(json.dumps({"site": name}))
    (data_dir / "sessions.json").write_text("{}")
    return data_dir


def test_discover_skips_empty_or_missing_db(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    # Real project — should be discovered
    _make_project(projects_root, "alpha")

    # Project dir with empty skein.db — must be skipped
    empty_data = projects_root / "empty" / ".skein" / "data"
    empty_data.mkdir(parents=True)
    (empty_data / "skein.db").touch()

    # Project dir with no skein.db at all
    (projects_root / "no_db" / ".skein" / "data").mkdir(parents=True)

    discovered = BackupManager.discover_project_data_dirs(projects_root)
    names = {d["project"] for d in discovered}
    assert names == {"alpha"}


def test_create_full_backup_all_projects_writes_per_project_artifacts(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _make_project(projects_root, "alpha")
    _make_project(projects_root, "bravo", db_size_rows=20)

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(backup_dir=backup_dir)
    summary = mgr.create_full_backup_all_projects(projects_root=projects_root)

    assert summary["discovered"] == 2
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0

    full_dir = backup_dir / "full"
    tarballs = sorted(p.name for p in full_dir.glob("*.tar.gz"))
    metadata_files = sorted(p.name for p in full_dir.glob("*.json"))
    assert len(tarballs) == 2
    assert len(metadata_files) == 2

    # Metadata exists for each project, with per-project fields
    for meta_path in full_dir.glob("*.json"):
        meta = json.loads(meta_path.read_text())
        assert meta["layout"] == "per-project"
        assert meta["project"] in {"alpha", "bravo"}
        assert meta["skein_db_method"] == "sqlite3.backup"
        assert meta["skein_db_size"] > 0
        assert meta["checksum"]
        assert meta["source_dir"].endswith(f"/{meta['project']}/.skein/data")


def test_extracted_skein_db_passes_integrity_check(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = _make_project(projects_root, "alpha", db_size_rows=50)
    backup_dir = tmp_path / "backups"

    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir)
    result = mgr.create_full_backup()

    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(result["backup_path"], "r:gz") as tar:
        tar.extractall(extract_dir)

    extracted_db = extract_dir / "skein.db"
    assert extracted_db.exists()
    conn = sqlite3.connect(str(extracted_db))
    integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    conn.close()
    assert integrity == "ok"
    assert rows == 50


def test_backup_skips_wal_and_shm_sidecars(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = _make_project(projects_root, "alpha")
    # Simulate a live writer leaving WAL/SHM behind
    (data_dir / "skein.db-wal").write_bytes(b"wal-junk")
    (data_dir / "skein.db-shm").write_bytes(b"shm-junk")

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir)
    result = mgr.create_full_backup()

    with tarfile.open(result["backup_path"], "r:gz") as tar:
        names = set(tar.getnames())
    assert "skein.db" in names
    assert "skein.db-wal" not in names
    assert "skein.db-shm" not in names


def test_cleanup_keep_last_rotates_per_project(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir_a = _make_project(projects_root, "alpha")
    data_dir_b = _make_project(projects_root, "bravo")
    backup_dir = tmp_path / "backups"
    mgr = BackupManager(backup_dir=backup_dir)

    # Make 4 backups for alpha, 2 for bravo. _backup_one timestamps to second
    # resolution, so use distinct tags to keep names unique.
    for i in range(4):
        mgr._backup_one(data_dir_a, "alpha", tag=f"a{i}")
    for i in range(2):
        mgr._backup_one(data_dir_b, "bravo", tag=f"b{i}")

    full_dir = backup_dir / "full"
    assert len(list(full_dir.glob("*.tar.gz"))) == 6

    # keep_last=2 should keep 2 alpha + 2 bravo, removing 2 alpha
    res = mgr.cleanup_old_backups(keep_last=2, dry_run=False)
    assert res["success"]
    assert len(res["removed"]) == 2
    assert all("alpha" in name for name in res["removed"])
    # Both a tarball and its sidecar metadata should have been removed
    remaining_tars = sorted(p.name for p in full_dir.glob("*.tar.gz"))
    remaining_jsons = sorted(p.name for p in full_dir.glob("*.json"))
    assert len(remaining_tars) == 4
    assert len(remaining_jsons) == 4


def test_cleanup_buckets_legacy_backups_without_project_field(
    tmp_path: Path,
) -> None:
    backup_dir = tmp_path / "backups"
    full_dir = backup_dir / "full"
    full_dir.mkdir(parents=True)

    # Hand-craft three legacy-style metadata files (no `project` field).
    for i in range(3):
        name = f"skein_full_2025-12-0{i + 1}_00-00-00"
        (full_dir / f"{name}.tar.gz").write_bytes(b"x")
        (full_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "timestamp": f"2025-12-0{i + 1}T00:00:00+00:00",
                    "backup_name": f"{name}.tar.gz",
                    "checksum": "deadbeef",
                    "backup_size": 1,
                    "source_dir": "/legacy",
                }
            )
        )

    mgr = BackupManager(backup_dir=backup_dir)
    res = mgr.cleanup_old_backups(keep_last=1, dry_run=False)
    assert res["success"]
    # All three share the legacy bucket — keep_last=1 removes the oldest two.
    assert len(res["removed"]) == 2


def test_backup_succeeds_when_source_parent_is_read_only(tmp_path: Path) -> None:
    """Backup must succeed against a WAL-mode db whose parent dir is unwritable.

    Reproduces the failure mode where SQLite would otherwise try to materialize
    skein.db-shm in a chmod 555 parent dir and raise "attempt to write a
    readonly database", causing _backup_one to throw and the project to be
    silently omitted from the run.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    # Build a WAL-mode db so the read path requires SHM materialization.
    conn = sqlite3.connect(str(data_dir / "skein.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(25)],
    )
    conn.commit()
    # Truncate the WAL but the db file's header still records WAL mode, which
    # is what triggers the SHM materialization attempt on the next read.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    # Add the canonical sibling pieces.
    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sessions.json").write_text("{}")

    # Lock down the parent dir: nothing under it is writable.
    original_mode = data_dir.stat().st_mode
    os.chmod(
        data_dir,
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH,
    )
    try:
        backup_dir = tmp_path / "backups"
        mgr = BackupManager(
            data_dir=data_dir, backup_dir=backup_dir, project_name="alpha"
        )
        result = mgr.create_full_backup()
    finally:
        os.chmod(data_dir, original_mode)

    assert result["skein_db_method"] == "sqlite3.backup"
    assert result["skein_db_size"] > 0

    # Extract and verify the snapshot is a valid integrity-clean db with the
    # rows we wrote (no silent loss).
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    with tarfile.open(result["backup_path"], "r:gz") as tar:
        tar.extractall(extract_dir)

    extracted_db = extract_dir / "skein.db"
    assert extracted_db.exists()
    rconn = sqlite3.connect(str(extracted_db))
    integrity = rconn.execute("PRAGMA integrity_check;").fetchone()[0]
    rows = rconn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    rconn.close()
    assert integrity == "ok"
    assert rows == 25


def test_cleanup_keep_last_mixes_legacy_and_per_project_artifacts(
    tmp_path: Path,
) -> None:
    """--keep-last must rotate per-project without cross-deleting legacy entries.

    Mixed shared backup dir: a few per-project artifacts (with `project` set)
    alongside legacy single-project artifacts (no `project` field). Each group
    rotates independently — neither bucket cannibalizes the other.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = _make_project(projects_root, "alpha")
    backup_dir = tmp_path / "backups"
    full_dir = backup_dir / "full"
    full_dir.mkdir(parents=True)

    mgr = BackupManager(backup_dir=backup_dir)

    # 3 real per-project alpha backups via the manager.
    for i in range(3):
        mgr._backup_one(data_dir, "alpha", tag=f"a{i}")

    # 3 hand-crafted legacy artifacts (no project field) in the same dir.
    for i in range(3):
        name = f"skein_full_2025-12-0{i + 1}_00-00-00"
        (full_dir / f"{name}.tar.gz").write_bytes(b"x")
        (full_dir / f"{name}.json").write_text(
            json.dumps(
                {
                    "timestamp": f"2025-12-0{i + 1}T00:00:00+00:00",
                    "backup_name": f"{name}.tar.gz",
                    "checksum": "deadbeef",
                    "backup_size": 1,
                    "source_dir": "/legacy",
                }
            )
        )

    assert len(list(full_dir.glob("*.tar.gz"))) == 6
    assert len(list(full_dir.glob("*.json"))) == 6

    res = mgr.cleanup_old_backups(keep_last=1, dry_run=False)
    assert res["success"]

    removed = res["removed"]
    keeping = res["keeping"]
    # Each bucket (alpha + legacy) trims to 1, so 4 removed and 2 kept.
    assert len(removed) == 4
    assert len(keeping) == 2

    # Each bucket is represented in `keeping` exactly once — neither side
    # cross-deleted the other.
    kept_alpha = [n for n in keeping if "alpha" in n]
    kept_legacy = [n for n in keeping if "alpha" not in n]
    assert len(kept_alpha) == 1
    assert len(kept_legacy) == 1


def test_backup_cleans_up_temp_files_on_failure(tmp_path: Path) -> None:
    """An exception mid-backup must not leave .tmp orphans behind.

    Inject failure at _calculate_checksum, which runs AFTER the tarball .tmp
    has been written but BEFORE the metadata .tmp is created. This exercises
    the cleanup path with a real orphan tarball .tmp on disk.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = _make_project(projects_root, "alpha")
    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir, project_name="alpha")

    full_dir = backup_dir / "full"

    def boom(_path: Path) -> str:
        # Sanity check: the tarball .tmp must exist by the time we fire,
        # otherwise the cleanup assertion below would pass vacuously.
        assert list(full_dir.glob("*.tar.gz.tmp")), (
            "no tarball .tmp present when checksum fired; test would be vacuous"
        )
        raise RuntimeError("simulated failure mid-backup")

    mgr._calculate_checksum = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        mgr.create_full_backup()

    leftovers = list(full_dir.glob("*.tmp"))
    assert leftovers == [], f"unexpected temp files left behind: {leftovers}"
    finals = list(full_dir.glob("*.tar.gz")) + list(full_dir.glob("*.json"))
    assert finals == [], f"partial finalized files: {finals}"


def test_backup_fails_loudly_on_unreadable_wal(tmp_path: Path) -> None:
    """An unreadable skein.db-wal next to a real WAL-mode db must surface as a
    per-project failure, not a silent wal-less snapshot.

    Round 3 fix: previously this fixture used a DELETE-mode db with a fake
    skein.db-wal sidecar, which only exercised permission-failure
    propagation — not the original data-loss scenario. We now build a real
    WAL-mode db with uncheckpointed rows and hold the connection open so
    skein.db-wal actually exists as a SQLite WAL file when the backup runs.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "skein.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    # Disable auto-checkpoint so the WAL keeps growing rather than being
    # folded into the main db. Combined with the held-open connection this
    # guarantees skein.db-wal exists with real content for the duration of
    # the test (the WAL would otherwise be truncated/removed on last close).
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(25)],
    )
    conn.commit()

    # Canonical sibling pieces (matches _make_project layout).
    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sites").mkdir()
    site = data_dir / "sites" / "alpha"
    site.mkdir()
    (site / "metadata.json").write_text(json.dumps({"site": "alpha"}))
    (data_dir / "sessions.json").write_text("{}")

    wal_path = data_dir / "skein.db-wal"
    # Sanity: without this, a future regression that breaks the WAL setup
    # would silently turn this test back into the previous sidecar-only check.
    assert wal_path.exists(), "fixture WAL missing — test setup regression"
    assert wal_path.stat().st_size > 0, (
        "fixture WAL is empty — test setup regression (auto-checkpoint or "
        "close cleanup may have run)"
    )

    os.chmod(wal_path, 0)

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir)

    try:
        result = mgr.create_full_backup_all_projects(projects_root=projects_root)
    finally:
        os.chmod(wal_path, stat.S_IRUSR | stat.S_IWUSR)
        conn.close()

    assert result["succeeded"] == 0, f"expected zero successes, got {result}"
    assert result["failed"] == 1, f"expected one failure, got {result}"
    assert result["errors"], "expected an error entry for the failed project"
    assert result["errors"][0]["project"] == "alpha"

    full_dir = backup_dir / "full"
    leftovers = list(full_dir.glob("*.tmp"))
    finals = list(full_dir.glob("*.tar.gz")) + list(full_dir.glob("*.json"))
    assert leftovers == [], f"unexpected temp files left behind: {leftovers}"
    assert finals == [], f"partial finalized files: {finals}"


def test_backup_retries_when_wal_mutates_during_snapshot(tmp_path: Path) -> None:
    """If the live WAL changes between the DB copy and the WAL copy, the
    snapshot must retry rather than capture an inconsistent DB/WAL pair.

    Simulates the live SKEIN service writing to skein.db-wal during the
    snapshot by patching shutil.copy2 to bump the source WAL's mtime on the
    first DB copy. The retry-on-mtime loop should detect the change, retry
    once with a clean window, and produce a valid backup.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "skein.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(20)],
    )
    conn.commit()

    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sessions.json").write_text("{}")

    src_wal = data_dir / "skein.db-wal"
    assert src_wal.exists() and src_wal.stat().st_size > 0, (
        "fixture WAL missing or empty — retry test would be vacuous"
    )

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir, project_name="alpha")

    real_copy2 = shutil.copy2
    state = {"db_copies": 0}

    def patched_copy2(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        # On the first DB copy, mutate the source WAL's mtime to simulate a
        # live writer flushing a transaction between our DB and WAL copies.
        # The WAL contents stay valid — only the mtime changes — which is
        # exactly the inconsistency the retry guard is meant to catch.
        if Path(src).name == "skein.db":
            if state["db_copies"] == 0:
                sb = src_wal.stat()
                os.utime(
                    src_wal,
                    ns=(sb.st_atime_ns, sb.st_mtime_ns + 1_000_000),
                )
            state["db_copies"] += 1
        return result

    try:
        with mock.patch("client.backup.shutil.copy2", side_effect=patched_copy2):
            result = mgr.create_full_backup()
    finally:
        conn.close()

    # Two DB copies = the first attempt's mtime mismatch triggered exactly
    # one retry, which then succeeded.
    assert state["db_copies"] >= 2, (
        f"expected at least one retry (>=2 DB copies), got {state['db_copies']}"
    )
    assert result["skein_db_method"] == "sqlite3.backup"
    assert result["skein_db_size"] > 0


def test_backup_fails_loudly_when_wal_mutates_continuously(tmp_path: Path) -> None:
    """If the WAL mutates on every retry attempt, the snapshot must raise
    rather than silently capture a torn DB/WAL pair."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "skein.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(20)],
    )
    conn.commit()

    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sessions.json").write_text("{}")

    src_wal = data_dir / "skein.db-wal"
    assert src_wal.exists() and src_wal.stat().st_size > 0

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir, project_name="alpha")

    real_copy2 = shutil.copy2
    state = {"db_copies": 0}

    def patched_copy2(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        # Every DB copy bumps the WAL — the retry loop can never see a
        # stable window and must raise after MAX_RETRIES.
        if Path(src).name == "skein.db":
            sb = src_wal.stat()
            os.utime(
                src_wal,
                ns=(
                    sb.st_atime_ns,
                    sb.st_mtime_ns + (state["db_copies"] + 1) * 1_000_000,
                ),
            )
            state["db_copies"] += 1
        return result

    try:
        with mock.patch("client.backup.shutil.copy2", side_effect=patched_copy2):
            with pytest.raises(RuntimeError, match="mutated during snapshot copy"):
                mgr.create_full_backup()
    finally:
        conn.close()

    # We should have hit the retry cap rather than bailing earlier.
    assert state["db_copies"] >= 3, (
        f"expected MAX_RETRIES=3 attempts, got {state['db_copies']}"
    )


def test_backup_retries_when_wal_size_changes_within_same_mtime_tick(
    tmp_path: Path,
) -> None:
    """If the WAL grows but its mtime is unchanged (sub-tick write on a
    coarse-mtime filesystem like NFS/SMB/FUSE, or under clock manipulation),
    the snapshot must still detect the change via the size component of the
    token and retry. mtime alone is not sufficient.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "skein.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(20)],
    )
    conn.commit()

    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sessions.json").write_text("{}")

    src_wal = data_dir / "skein.db-wal"
    assert src_wal.exists() and src_wal.stat().st_size > 0, (
        "fixture WAL missing or empty — retry test would be vacuous"
    )

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir, project_name="alpha")

    real_copy2 = shutil.copy2
    state = {"db_copies": 0}

    def patched_copy2(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        # On the first DB copy, append bytes to the source WAL while
        # preserving its mtime. This is the mtime-only-token blind spot:
        # content changed (size grew), mtime did not. The size component
        # of the token must catch it.
        if Path(src).name == "skein.db":
            if state["db_copies"] == 0:
                sb = src_wal.stat()
                with open(src_wal, "ab") as wal_fh:
                    wal_fh.write(b"\x00" * 4096)
                # Restore the original mtime so only size has changed.
                os.utime(src_wal, ns=(sb.st_atime_ns, sb.st_mtime_ns))
                # Sanity: mtime preserved, size grew.
                sb_after = src_wal.stat()
                assert sb_after.st_mtime_ns == sb.st_mtime_ns, (
                    "fixture failed to preserve mtime — test is vacuous"
                )
                assert sb_after.st_size > sb.st_size, (
                    "fixture failed to grow WAL — test is vacuous"
                )
            state["db_copies"] += 1
        return result

    try:
        with mock.patch("client.backup.shutil.copy2", side_effect=patched_copy2):
            result = mgr.create_full_backup()
    finally:
        conn.close()

    # Two DB copies = the first attempt's size mismatch triggered exactly
    # one retry, which then succeeded (no further mutation after attempt 0).
    assert state["db_copies"] >= 2, (
        f"expected at least one retry (>=2 DB copies), got {state['db_copies']}"
    )
    assert result["skein_db_method"] == "sqlite3.backup"
    assert result["skein_db_size"] > 0


def test_backup_retries_when_wal_content_changes_with_same_mtime_and_size(
    tmp_path: Path,
) -> None:
    """If the WAL is rewritten in place — same byte count, same mtime, but
    different content — the snapshot must still detect the change via the
    content_hash component of the token and retry. mtime+size alone are
    blind to this case (e.g. a SQLite frame overwrite that lands on the
    same nanosecond tick), so the strengthened token must hash WAL bytes.
    """
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    data_dir = projects_root / "alpha" / ".skein" / "data"
    data_dir.mkdir(parents=True)

    db_path = data_dir / "skein.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany(
        "INSERT INTO entries (val) VALUES (?)",
        [(f"row-{i}",) for i in range(20)],
    )
    conn.commit()

    (data_dir / "roster").mkdir()
    (data_dir / "roster" / "agents.json").write_text("{}")
    (data_dir / "sessions.json").write_text("{}")

    src_wal = data_dir / "skein.db-wal"
    assert src_wal.exists() and src_wal.stat().st_size > 0, (
        "fixture WAL missing or empty — retry test would be vacuous"
    )

    backup_dir = tmp_path / "backups"
    mgr = BackupManager(data_dir=data_dir, backup_dir=backup_dir, project_name="alpha")

    real_copy2 = shutil.copy2
    state = {"db_copies": 0}

    def patched_copy2(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        # On the first DB copy, overwrite the first byte of the source WAL
        # in place — preserving file size — and reset its mtime. This is
        # the mtime+size-only-token blind spot: stat metadata is identical
        # before and after, but the content differs. The content_hash
        # component of the strengthened token must catch it.
        if Path(src).name == "skein.db":
            if state["db_copies"] == 0:
                sb = src_wal.stat()
                with open(src_wal, "r+b") as wal_fh:
                    wal_fh.seek(0)
                    original = wal_fh.read(1)
                    wal_fh.seek(0)
                    # Flip a bit so the byte differs regardless of value.
                    wal_fh.write(bytes([original[0] ^ 0xFF]))
                # Restore the original mtime so only content has changed.
                os.utime(src_wal, ns=(sb.st_atime_ns, sb.st_mtime_ns))
                # Sanity: mtime preserved, size unchanged.
                sb_after = src_wal.stat()
                assert sb_after.st_mtime_ns == sb.st_mtime_ns, (
                    "fixture failed to preserve mtime — test is vacuous"
                )
                assert sb_after.st_size == sb.st_size, (
                    "fixture changed WAL size — test would conflate with size-test"
                )
            state["db_copies"] += 1
        return result

    try:
        with mock.patch("client.backup.shutil.copy2", side_effect=patched_copy2):
            result = mgr.create_full_backup()
    finally:
        conn.close()

    # Two DB copies = the first attempt's content-hash mismatch triggered
    # exactly one retry, which then succeeded.
    assert state["db_copies"] >= 2, (
        f"expected at least one retry (>=2 DB copies), got {state['db_copies']}"
    )
    assert result["skein_db_method"] == "sqlite3.backup"
    assert result["skein_db_size"] > 0
