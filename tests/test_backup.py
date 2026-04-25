"""Tests for SKEIN backup and recovery."""

import json
import sqlite3
import tarfile
from pathlib import Path

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
