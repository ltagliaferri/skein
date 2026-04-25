"""
SKEIN Backup & Recovery System

Provides backup and restore functionality for SKEIN data:
- Full backups (tar.gz of entire data directory)
- Multi-project discovery (per-project tarballs)
- Verification (checksums)
- Restore with dry-run and confirmation
"""

import json
import sqlite3
import tarfile
import hashlib
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


class BackupManager:
    """Manages SKEIN backup and restore operations."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        project_name: Optional[str] = None,
    ):
        """
        Initialize backup manager.

        Args:
            data_dir: Path to .skein/data directory. Optional — when omitted,
                the manager only supports multi-project ops (discover + backup-all)
                and read-only ops (list/verify/cleanup) on the shared backup dir.
            backup_dir: Path to store backups (default: ~/.skein/backups)
            project_name: Optional project name used in backup naming. If not
                provided and data_dir looks like ~/projects/<name>/.skein/data,
                derived from the path.
        """
        self.data_dir = Path(data_dir) if data_dir else None
        self.backup_dir = (
            Path(backup_dir) if backup_dir else Path.home() / ".skein" / "backups"
        )

        self.full_backup_dir = self.backup_dir / "full"
        self.full_backup_dir.mkdir(parents=True, exist_ok=True)

        if project_name:
            self.project_name: Optional[str] = project_name
        elif self.data_dir is not None:
            self.project_name = self._derive_project_name(self.data_dir)
        else:
            self.project_name = None

    @staticmethod
    def _derive_project_name(data_dir: Path) -> Optional[str]:
        """Derive a project name from a path like ~/projects/<name>/.skein/data."""
        try:
            parts = data_dir.resolve().parts
        except OSError:
            return None
        # Look for the .skein dir and use its parent's name as the project.
        for i, part in enumerate(parts):
            if part == ".skein" and i > 0:
                return parts[i - 1]
        return None

    @staticmethod
    def discover_project_data_dirs(
        projects_root: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """Find all ~/projects/*/.skein/data/ dirs with a non-empty skein.db.

        Args:
            projects_root: Defaults to ~/projects.

        Returns:
            List of {"project": <name>, "data_dir": Path} dicts, sorted by name.
        """
        root = Path(projects_root) if projects_root else Path.home() / "projects"
        if not root.exists():
            return []

        result = []
        for skein_db in sorted(root.glob("*/.skein/data/skein.db")):
            try:
                if not skein_db.is_file():
                    continue
                if skein_db.stat().st_size == 0:
                    continue
            except OSError:
                continue
            data_dir = skein_db.parent
            project = data_dir.parent.parent.name
            result.append({"project": project, "data_dir": data_dir})
        return result

    @staticmethod
    def _snapshot_skein_db(source_db: Path, dest_db: Path) -> None:
        """Snapshot a SQLite db using the .backup API.

        Safe under live WAL writers — the backup API takes a consistent
        snapshot even while another process is reading or writing.
        """
        src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(dest_db))
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _get_dir_stats(self, directory: Path) -> Dict[str, Any]:
        """Get statistics for a directory."""
        stats: Dict[str, Any] = {"total_files": 0, "total_size": 0, "file_types": {}}

        for file_path in directory.rglob("*"):
            if file_path.is_file():
                stats["total_files"] += 1
                stats["total_size"] += file_path.stat().st_size
                ext = file_path.suffix or "no_ext"
                stats["file_types"][ext] = stats["file_types"].get(ext, 0) + 1

        return stats

    def _build_backup_name(
        self, project: Optional[str], timestamp: str, tag: Optional[str]
    ) -> str:
        """Build the canonical base backup name (no extension)."""
        if project:
            base = f"skein_full_{project}_{timestamp}"
        else:
            base = f"skein_full_{timestamp}"
        if tag:
            base += f"_{tag}"
        return base

    def _backup_one(
        self,
        data_dir: Path,
        project_name: Optional[str],
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a tarball + metadata for a single project's data dir."""
        if not data_dir.exists():
            raise ValueError(f"Data directory does not exist: {data_dir}")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        base = self._build_backup_name(project_name, timestamp, tag)
        backup_name = base + ".tar.gz"
        backup_path = self.full_backup_dir / backup_name

        # Files we never copy directly: the live db (snapshotted instead)
        # and its WAL/SHM sidecars (transient state).
        live_db = data_dir / "skein.db"
        skip_names = {"skein.db", "skein.db-wal", "skein.db-shm"}

        db_method: Optional[str] = None
        db_size: Optional[int] = None

        with tempfile.TemporaryDirectory(prefix="skein-backup-") as tmpdir:
            snapshot_db = Path(tmpdir) / "skein.db"
            if live_db.exists() and live_db.stat().st_size > 0:
                self._snapshot_skein_db(live_db, snapshot_db)
                db_method = "sqlite3.backup"
                db_size = snapshot_db.stat().st_size

            with tarfile.open(backup_path, "w:gz", compresslevel=6) as tar:
                if snapshot_db.exists():
                    tar.add(snapshot_db, arcname="skein.db")
                for item in sorted(data_dir.iterdir()):
                    if item.name in skip_names:
                        continue
                    tar.add(item, arcname=item.name)

        checksum = self._calculate_checksum(backup_path)
        backup_size = backup_path.stat().st_size
        source_stats = self._get_dir_stats(data_dir)

        metadata = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "backup_name": backup_name,
            "project": project_name,
            "layout": "per-project",
            "checksum": checksum,
            "backup_size": backup_size,
            "source_dir": str(data_dir),
            "source_stats": source_stats,
            "skein_db_method": db_method,
            "skein_db_size": db_size,
            "tag": tag,
            "skein_version": "1.0",
        }

        metadata_path = self.full_backup_dir / (base + ".json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2, default=str)

        return {
            "backup_path": str(backup_path),
            "metadata_path": str(metadata_path),
            "backup_name": backup_name,
            "project": project_name,
            "checksum": checksum,
            "backup_size": backup_size,
            "source_dir": str(data_dir),
            "source_stats": source_stats,
            "skein_db_method": db_method,
            "skein_db_size": db_size,
        }

    def create_full_backup(self, tag: Optional[str] = None) -> Dict[str, Any]:
        """Create a full backup of THIS manager's data_dir.

        Used for explicit single-project backups (e.g. pre-restore snapshots).
        For the daily multi-project run, use create_full_backup_all_projects.
        """
        if self.data_dir is None:
            raise ValueError(
                "create_full_backup requires the manager to be initialized with data_dir"
            )
        return self._backup_one(self.data_dir, self.project_name, tag=tag)

    def create_full_backup_all_projects(
        self,
        projects_root: Optional[Path] = None,
        tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Discover all SKEIN project data dirs and back each up to its own tarball.

        Continues past per-project failures so a single corrupt db does not
        abort the run. Returns a summary with successes and errors.
        """
        projects = self.discover_project_data_dirs(projects_root)
        successes: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for proj in projects:
            try:
                result = self._backup_one(
                    proj["data_dir"], proj["project"], tag=tag
                )
                successes.append(result)
            except Exception as e:
                errors.append({"project": proj["project"], "error": str(e)})

        return {
            "discovered": len(projects),
            "succeeded": len(successes),
            "failed": len(errors),
            "projects": successes,
            "errors": errors,
        }

    def list_backups(self, backup_type: str = "all") -> List[Dict[str, Any]]:
        """
        List available backups.

        Args:
            backup_type: 'full', 'incremental', or 'all'

        Returns:
            List of backup metadata dicts, sorted by date (newest first)
        """
        backups = []

        if backup_type in ("full", "all"):
            for metadata_file in self.full_backup_dir.glob("*.json"):
                try:
                    with open(metadata_file) as f:
                        metadata = json.load(f)
                    metadata["type"] = "full"
                    # metadata_file is name.json, backup is name.tar.gz
                    backup_file = metadata_file.parent / (
                        metadata_file.stem + ".tar.gz"
                    )
                    metadata["exists"] = backup_file.exists()
                    backups.append(metadata)
                except Exception:
                    # Skip invalid metadata files
                    pass

        # Sort by timestamp, newest first
        backups.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return backups

    def get_backup(self, backup_id: str) -> Optional[Dict[str, Any]]:
        """
        Get backup metadata by ID (backup name without extension).

        Args:
            backup_id: Backup identifier (e.g., 'skein_full_2025-11-15_00-00-00')

        Returns:
            Backup metadata dict or None if not found
        """
        # Try full backups
        metadata_path = self.full_backup_dir / f"{backup_id}.json"
        if not metadata_path.exists():
            # Try with .tar.gz extension stripped
            if backup_id.endswith(".tar.gz"):
                backup_id = backup_id[:-7]
                metadata_path = self.full_backup_dir / f"{backup_id}.json"

        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)
                metadata["type"] = "full"
                # metadata_path is name.json, backup is name.tar.gz
                backup_file = metadata_path.parent / (metadata_path.stem + ".tar.gz")
                metadata["exists"] = backup_file.exists()
                metadata["backup_file"] = str(backup_file)
                return metadata
            except Exception:
                pass

        return None

    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """
        Verify backup integrity.

        Args:
            backup_id: Backup identifier

        Returns:
            Dict with verification results
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            return {"valid": False, "error": f"Backup not found: {backup_id}"}

        backup_path = Path(metadata["backup_file"])
        if not backup_path.exists():
            return {"valid": False, "error": f"Backup file missing: {backup_path}"}

        # Verify checksum
        actual_checksum = self._calculate_checksum(backup_path)
        expected_checksum = metadata.get("checksum")

        if actual_checksum != expected_checksum:
            return {
                "valid": False,
                "error": f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}",
            }

        # Try to read the archive
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                members = tar.getnames()
        except Exception as e:
            return {"valid": False, "error": f"Failed to read archive: {e}"}

        return {
            "valid": True,
            "checksum": actual_checksum,
            "file_count": len(members),
            "backup_size": backup_path.stat().st_size,
        }

    def restore_backup(
        self,
        backup_id: str,
        dry_run: bool = False,
        confirm: bool = False,
        destination: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Restore from a backup.

        Args:
            backup_id: Backup identifier
            dry_run: If True, show what would be restored without making changes
            confirm: Must be True to actually perform restore
            destination: Override the restore destination. Defaults to the
                manager's data_dir if set, otherwise to the backup's recorded
                source_dir.

        Returns:
            Dict with restore results
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            return {"success": False, "error": f"Backup not found: {backup_id}"}

        backup_path = Path(metadata["backup_file"])
        if not backup_path.exists():
            return {"success": False, "error": f"Backup file missing: {backup_path}"}

        # Resolve destination
        if destination is not None:
            target_dir = Path(destination)
        elif self.data_dir is not None:
            target_dir = self.data_dir
        elif metadata.get("source_dir"):
            target_dir = Path(metadata["source_dir"])
        else:
            return {
                "success": False,
                "error": "No destination known: pass destination or initialize with data_dir",
            }

        # Verify backup first
        verification = self.verify_backup(backup_id)
        if not verification["valid"]:
            return {
                "success": False,
                "error": f"Backup verification failed: {verification.get('error')}",
            }

        # Get list of files in backup
        with tarfile.open(backup_path, "r:gz") as tar:
            members = tar.getnames()

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "would_restore": {
                    "files": len(members),
                    "source_stats": metadata.get("source_stats", {}),
                    "to_directory": str(target_dir),
                    "members": members[:20],  # Show first 20 files
                },
            }

        if not confirm:
            return {
                "success": False,
                "error": "Restore requires --confirm flag. Use --dry-run to preview.",
            }

        # Create backup of current state before restore
        pre_restore_backup = None
        if target_dir.exists() and any(target_dir.iterdir()):
            try:
                pre_restore_manager = BackupManager(
                    data_dir=target_dir,
                    backup_dir=self.backup_dir,
                    project_name=metadata.get("project")
                    or self._derive_project_name(target_dir),
                )
                pre_restore = pre_restore_manager.create_full_backup(tag="pre-restore")
                pre_restore_backup = pre_restore["backup_name"]
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to backup current state before restore: {e}",
                }

        # Clear existing data directory
        if target_dir.exists():
            for item in target_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        else:
            target_dir.mkdir(parents=True, exist_ok=True)

        # Extract backup
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                tar.extractall(target_dir)
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to extract backup: {e}",
                "pre_restore_backup": pre_restore_backup,
            }

        return {
            "success": True,
            "restored_from": metadata["backup_name"],
            "restored_to": str(target_dir),
            "files_restored": len(members),
            "pre_restore_backup": pre_restore_backup,
        }

    def _remove_backup_files(self, backup_name: str) -> None:
        """Remove a backup tarball and its sidecar metadata file."""
        backup_path = self.full_backup_dir / backup_name
        # Metadata sidecar drops the .tar.gz before adding .json
        if backup_name.endswith(".tar.gz"):
            metadata_path = self.full_backup_dir / (backup_name[:-7] + ".json")
        else:
            metadata_path = self.full_backup_dir / (
                Path(backup_name).stem + ".json"
            )

        if backup_path.exists():
            backup_path.unlink()
        if metadata_path.exists():
            metadata_path.unlink()

    def cleanup_old_backups(
        self,
        keep_last: Optional[int] = None,
        older_than_days: Optional[int] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Remove old backups based on retention policy.

        With per-project backups, --keep-last rotates per project (each
        project keeps its own last-N history). --older-than is global by
        absolute timestamp.

        Args:
            keep_last: Keep only the N most recent backups per project
            older_than_days: Remove backups older than N days
            dry_run: Show what would be removed without actually removing

        Returns:
            Dict with cleanup results
        """
        backups = self.list_backups(backup_type="full")

        to_remove: List[Dict[str, Any]] = []
        to_keep: List[Dict[str, Any]] = []

        if keep_last:
            # Group by project for per-project rotation. Backups missing a
            # project key (legacy single-project artifacts) bucket together.
            by_project: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for b in backups:
                project = b.get("project") or "<legacy>"
                by_project[project].append(b)
            for group in by_project.values():
                # list_backups returned newest-first
                to_keep.extend(group[:keep_last])
                to_remove.extend(group[keep_last:])
        elif older_than_days:
            cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
            for backup in backups:
                try:
                    backup_time = datetime.fromisoformat(
                        backup["timestamp"].replace("Z", "+00:00")
                    )
                    if backup_time.timestamp() < cutoff:
                        to_remove.append(backup)
                    else:
                        to_keep.append(backup)
                except Exception:
                    to_keep.append(backup)  # Keep if can't parse date
        else:
            return {
                "success": False,
                "error": "Must specify --keep-last or --older-than",
            }

        removed: List[str] = []
        errors: List[str] = []

        if not dry_run:
            for backup in to_remove:
                try:
                    backup_name = backup["backup_name"]
                    self._remove_backup_files(backup_name)
                    removed.append(backup_name)
                except Exception as e:
                    errors.append(f"{backup.get('backup_name', 'unknown')}: {e}")

        return {
            "success": len(errors) == 0,
            "dry_run": dry_run,
            "would_remove" if dry_run else "removed": [
                b["backup_name"] for b in to_remove
            ],
            "keeping": [b["backup_name"] for b in to_keep],
            "errors": errors if errors else None,
        }


def get_backup_manager_for_project() -> Optional[BackupManager]:
    """
    Get BackupManager for the current project (detects .skein directory).

    Returns:
        BackupManager instance or None if not in a project
    """
    # Find project root (directory containing .skein)
    current = Path.cwd()
    while current != current.parent:
        skein_dir = current / ".skein"
        if skein_dir.exists() and skein_dir.is_dir():
            data_dir = skein_dir / "data"
            return BackupManager(data_dir)
        current = current.parent

    return None
