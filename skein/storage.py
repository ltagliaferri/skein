"""
SKEIN storage layer: SQLite for logs/threads/folios, JSON for roster/sites.
Multi-project support via ~/.skein/projects.json registry.
"""

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from .models import AgentInfo, Site, Folio, Thread, LogLine

try:
    from knurl import canon, hash as knurl_hash

    KNURL_AVAILABLE = True
except ImportError:
    KNURL_AVAILABLE = False

logger = logging.getLogger(__name__)


def ensure_aware(dt_value) -> Optional[datetime]:
    """Ensure a datetime value is timezone-aware (UTC). Handles strings and datetime objects."""
    if dt_value is None:
        return None
    if isinstance(dt_value, str):
        if dt_value.endswith("Z"):
            dt_value = datetime.fromisoformat(dt_value.replace("Z", "+00:00"))
        else:
            dt_value = datetime.fromisoformat(dt_value)
    if isinstance(dt_value, datetime) and dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)
    return dt_value


def compute_folio_hash(folio: Folio) -> str:
    """Compute content-addressable hash of folio's immutable fields."""
    if not KNURL_AVAILABLE:
        return None

    # Only hash immutable fields
    immutable = {
        "type": folio.type,
        "title": folio.title,
        "content": folio.content,
        "created_at": folio.created_at.isoformat() if folio.created_at else None,
        "created_by": folio.created_by,
    }
    canonical = canon.serialize(immutable)
    return knurl_hash.compute(canonical.decode("utf-8"), prefix="folio")


# Project Registry
def load_project_registry() -> Dict[str, Dict[str, Any]]:
    """Load project registry from ~/.skein/projects.json."""
    registry_file = Path.home() / ".skein" / "projects.json"
    if not registry_file.exists():
        logger.warning("No ~/.skein/projects.json found, using default data dir")
        return {}

    try:
        with open(registry_file) as f:
            data = json.load(f)
            return data.get("projects", {})
    except Exception as e:
        logger.error(f"Failed to load project registry: {e}")
        return {}


def get_data_dir_for_project(project_id: Optional[str] = None) -> Path:
    """
    Get data directory for a project.

    If project_id is provided, looks up in registry.
    Otherwise uses default data directory.
    """
    if project_id:
        registry = load_project_registry()
        if project_id in registry:
            data_dir = Path(registry[project_id]["data_dir"])
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir
        else:
            raise ValueError(f"Project '{project_id}' not found in registry")

    # No project_id provided - this shouldn't happen in normal operation
    raise ValueError("No project_id provided and no default available")


def search_folio_across_projects(
    folio_id: str, current_project_id: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """
    Search for a folio across all registered projects (except the current one).

    Returns {"project_name": ..., "project_path": ...} if found, None otherwise.
    Uses raw SQLite queries to avoid LogDatabase init overhead.
    """
    registry = load_project_registry()
    for project_name, project_info in registry.items():
        if project_name == current_project_id:
            continue
        try:
            data_dir = Path(project_info["data_dir"])
            db_path = data_dir / "skein.db"
            if not db_path.exists():
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cursor = conn.execute(
                    "SELECT 1 FROM folios WHERE folio_id = ? LIMIT 1", (folio_id,)
                )
                if cursor.fetchone():
                    project_path = project_info.get("path", str(data_dir))
                    return {"project_name": project_name, "project_path": project_path}
            finally:
                conn.close()
        except Exception as e:
            logger.debug(
                f"Skipping project '{project_name}' during cross-project folio search: {e}"
            )
            continue
    return None


# Legacy module-level variables removed - use project-specific instances via get_data_dir_for_project()


# SQLite Database for Logs


class LogDatabase:
    """SQLite database for log storage and querying."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    level TEXT,
                    source TEXT,
                    message TEXT NOT NULL,
                    metadata JSON
                )
            """
            )

            # Create indexes
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_stream_time
                ON logs(stream_id, timestamp DESC)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_stream_level
                ON logs(stream_id, level)
            """
            )

            # Full-text search
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts
                USING fts5(message, content=logs)
            """
            )

            # Screenshots table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS screenshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    screenshot_id TEXT UNIQUE NOT NULL,
                    strand_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    turn_number INTEGER,
                    label TEXT,
                    file_path TEXT NOT NULL,
                    file_size INTEGER,
                    metadata JSON
                )
            """
            )

            # Create indexes for screenshots
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_screenshots_strand
                ON screenshots(strand_id, timestamp DESC)
            """
            )

            # Sacks table - stores yields from chain participants
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sack_id TEXT UNIQUE NOT NULL,
                    chain_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,

                    -- Yield fields (structured, queryable)
                    status TEXT,
                    outcome TEXT,
                    artifacts JSON,
                    notes TEXT,

                    -- Enrichment (added by system)
                    duration_seconds INTEGER,
                    tokens_used INTEGER,
                    shard_path TEXT,
                    tender_id TEXT,

                    -- Catchall for future fields
                    metadata JSON
                )
            """
            )

            # Create indexes for sacks
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_chain
                ON sacks(chain_id, timestamp)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_agent
                ON sacks(agent_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sacks_status
                ON sacks(status)
            """
            )

            # Threads table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT,
                    weaver TEXT,
                    created_at DATETIME NOT NULL
                )
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_from
                ON threads(from_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_to
                ON threads(to_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_type
                ON threads(type)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_created
                ON threads(created_at)
            """
            )

            # Compound indexes for batch status/assignment lookups
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_to_type_created
                ON threads(to_id, type, created_at DESC)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_threads_from_type_created
                ON threads(from_id, type, created_at DESC)
            """
            )

            # Folios table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS folios (
                    folio_id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    site_id TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    created_by TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT DEFAULT 'open',
                    assigned_to TEXT,
                    target_agent TEXT,
                    omlet TEXT,
                    archived INTEGER DEFAULT 0,
                    metadata JSON,
                    acknowledged_at DATETIME,
                    content_hash TEXT
                )
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_site
                ON folios(site_id)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_type
                ON folios(type)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_status
                ON folios(status)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_created
                ON folios(created_at DESC)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_created_by
                ON folios(created_by)
            """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_folios_assigned
                ON folios(assigned_to)
            """
            )

            # FTS5 for folio search
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS folios_fts
                USING fts5(
                    folio_id,
                    title,
                    content,
                    content=folios,
                    content_rowid=rowid
                )
            """
            )

            # FTS5 triggers to keep index in sync
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS folios_ai AFTER INSERT ON folios BEGIN
                    INSERT INTO folios_fts(rowid, folio_id, title, content)
                    VALUES (new.rowid, new.folio_id, new.title, new.content);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS folios_ad AFTER DELETE ON folios BEGIN
                    INSERT INTO folios_fts(folios_fts, rowid, folio_id, title, content)
                    VALUES('delete', old.rowid, old.folio_id, old.title, old.content);
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS folios_au AFTER UPDATE ON folios BEGIN
                    INSERT INTO folios_fts(folios_fts, rowid, folio_id, title, content)
                    VALUES('delete', old.rowid, old.folio_id, old.title, old.content);
                    INSERT INTO folios_fts(rowid, folio_id, title, content)
                    VALUES (new.rowid, new.folio_id, new.title, new.content);
                END
            """)

            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Get database connection context manager."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add_logs(self, stream_id: str, source: str, lines: List[Dict[str, Any]]) -> int:
        """Add log lines to database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            count = 0

            for line in lines:
                cursor.execute(
                    """
                    INSERT INTO logs (stream_id, level, source, message, metadata)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        stream_id,
                        line.get("level", "INFO"),
                        source,
                        line.get("message", ""),
                        json.dumps(line.get("metadata", {})),
                    ),
                )
                count += 1

            conn.commit()
            return count

    def get_logs(
        self,
        stream_id: str,
        since: Optional[str] = None,
        level: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 1000,
    ) -> List[LogLine]:
        """Query logs with filters."""
        with self._get_connection() as conn:
            query = "SELECT * FROM logs WHERE stream_id = ?"
            params = [stream_id]

            if since:
                query += " AND timestamp >= datetime(?)"
                params.append(since)

            if level:
                query += " AND level = ?"
                params.append(level)

            if search:
                # Use FTS for full-text search
                query = """
                    SELECT logs.* FROM logs
                    JOIN logs_fts ON logs.rowid = logs_fts.rowid
                    WHERE stream_id = ? AND logs_fts MATCH ?
                """
                params = [stream_id, search]

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                LogLine(
                    id=row["id"],
                    stream_id=row["stream_id"],
                    timestamp=ensure_aware(row["timestamp"]),
                    level=row["level"],
                    source=row["source"],
                    message=row["message"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                )
                for row in rows
            ]

    def get_streams(self) -> List[Dict[str, Any]]:
        """Get list of all log streams."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT
                    stream_id,
                    COUNT(*) as line_count,
                    MIN(timestamp) as first_log,
                    MAX(timestamp) as last_log
                FROM logs
                GROUP BY stream_id
                ORDER BY last_log DESC
            """
            )

            return [dict(row) for row in cursor.fetchall()]

    def add_screenshot(
        self,
        screenshot_id: str,
        strand_id: str,
        turn_number: Optional[int],
        label: str,
        file_path: str,
        file_size: int,
        metadata: Dict[str, Any],
    ) -> bool:
        """Add screenshot metadata to database."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO screenshots (screenshot_id, strand_id, turn_number, label, file_path, file_size, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    screenshot_id,
                    strand_id,
                    turn_number,
                    label,
                    file_path,
                    file_size,
                    json.dumps(metadata),
                ),
            )
            conn.commit()
            return True

    def get_screenshots(
        self,
        strand_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query screenshots with filters."""
        with self._get_connection() as conn:
            query = "SELECT * FROM screenshots WHERE 1=1"
            params = []

            if strand_id:
                query += " AND strand_id = ?"
                params.append(strand_id)

            if since:
                query += " AND timestamp >= datetime(?)"
                params.append(since)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    def get_screenshot(self, screenshot_id: str) -> Optional[Dict[str, Any]]:
        """Get specific screenshot by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM screenshots WHERE screenshot_id = ?", (screenshot_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    # Sack Operations

    def add_yield(
        self,
        sack_id: str,
        chain_id: str,
        task_id: str,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        outcome: Optional[str] = None,
        artifacts: Optional[List[str]] = None,
        notes: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        tokens_used: Optional[int] = None,
        shard_path: Optional[str] = None,
        tender_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Add a yield to the sack for a chain.

        Args:
            sack_id: Unique yield ID (e.g., 'yield-20251206-abc')
            chain_id: Chain this yield belongs to
            task_id: Which task produced this yield
            agent_id: Agent that produced the yield
            status: Yield status (complete/partial/blocked)
            outcome: What was accomplished
            artifacts: List of SKEIN artifact IDs (tender-xyz, finding-abc)
            notes: Context for next agent
            duration_seconds: How long the task took
            tokens_used: Token consumption
            shard_path: Path to shard worktree if used
            tender_id: Tender folio ID if work was tendered
            metadata: Additional metadata

        Returns:
            True on success
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sacks (
                    sack_id, chain_id, task_id, agent_id,
                    status, outcome, artifacts, notes,
                    duration_seconds, tokens_used, shard_path, tender_id,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    sack_id,
                    chain_id,
                    task_id,
                    agent_id,
                    status,
                    outcome,
                    json.dumps(artifacts) if artifacts else None,
                    notes,
                    duration_seconds,
                    tokens_used,
                    shard_path,
                    tender_id,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            conn.commit()
            return True

    def get_chain_yields(self, chain_id: str) -> List[Dict[str, Any]]:
        """
        Get all yields in a chain, ordered by timestamp.

        Args:
            chain_id: The chain to query

        Returns:
            List of yield dicts in execution order
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp", (chain_id,)
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                # Parse JSON fields
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_yield(self, sack_id: str) -> Optional[Dict[str, Any]]:
        """Get specific yield by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM sacks WHERE sack_id = ?", (sack_id,))
            row = cursor.fetchone()
            if not row:
                return None

            yield_dict = dict(row)
            if yield_dict.get("artifacts"):
                yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
            if yield_dict.get("metadata"):
                yield_dict["metadata"] = json.loads(yield_dict["metadata"])
            return yield_dict

    def get_yields_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get yields by status (e.g., find all blocked work)."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE status = ? ORDER BY timestamp DESC",
                (status,),
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_agent_yields(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all yields by a specific agent."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE agent_id = ? ORDER BY timestamp DESC",
                (agent_id,),
            )
            rows = cursor.fetchall()

            results = []
            for row in rows:
                yield_dict = dict(row)
                if yield_dict.get("artifacts"):
                    yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
                if yield_dict.get("metadata"):
                    yield_dict["metadata"] = json.loads(yield_dict["metadata"])
                results.append(yield_dict)

            return results

    def get_previous_yield(
        self, chain_id: str, before_task_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get the most recent yield in a chain before a specific task.

        Used for injecting previous yield context into downstream tasks.

        Args:
            chain_id: The chain to query
            before_task_id: Get yields before this task

        Returns:
            The previous yield dict, or None if this is the first task
        """
        with self._get_connection() as conn:
            # Get all yields in chain ordered by timestamp
            cursor = conn.execute(
                "SELECT * FROM sacks WHERE chain_id = ? ORDER BY timestamp", (chain_id,)
            )
            rows = cursor.fetchall()

            # Find the yield just before the specified task
            previous = None
            for row in rows:
                if row["task_id"] == before_task_id:
                    break
                previous = row

            if not previous:
                return None

            yield_dict = dict(previous)
            if yield_dict.get("artifacts"):
                yield_dict["artifacts"] = json.loads(yield_dict["artifacts"])
            if yield_dict.get("metadata"):
                yield_dict["metadata"] = json.loads(yield_dict["metadata"])
            return yield_dict

    # Thread Operations

    def save_thread(self, thread: Thread) -> bool:
        """Save a thread to the database."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO threads
                (thread_id, from_id, to_id, type, content, weaver, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    thread.thread_id,
                    thread.from_id,
                    thread.to_id,
                    thread.type,
                    thread.content,
                    thread.weaver,
                    thread.created_at.isoformat()
                    if isinstance(thread.created_at, datetime)
                    else str(thread.created_at),
                ),
            )
            conn.commit()
        return True

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
    ) -> List[Thread]:
        """Get threads with optional filters using indexed queries."""
        with self._get_connection() as conn:
            query = "SELECT * FROM threads WHERE 1=1"
            params = []

            if from_id:
                query += " AND from_id = ?"
                params.append(from_id)
            if to_id:
                query += " AND to_id = ?"
                params.append(to_id)
            if type:
                query += " AND type = ?"
                params.append(type)
            if weaver:
                query += " AND weaver = ?"
                params.append(weaver)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

            return [
                Thread(
                    thread_id=row["thread_id"],
                    from_id=row["from_id"],
                    to_id=row["to_id"],
                    type=row["type"],
                    content=row["content"],
                    weaver=row["weaver"],
                    created_at=ensure_aware(row["created_at"]),
                )
                for row in rows
            ]

    def get_latest_statuses(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent status for each folio in a single query.

        Returns dict mapping folio_id -> status content.
        """
        with self._get_connection() as conn:
            query = """
                SELECT t.to_id AS folio_id, t.content
                FROM threads t
                INNER JOIN (
                    SELECT to_id, MAX(created_at) AS max_created
                    FROM threads WHERE type = 'status'
                    {where_clause}
                    GROUP BY to_id
                ) latest ON t.to_id = latest.to_id
                        AND t.created_at = latest.max_created
                        AND t.type = 'status'
            """
            params = []
            if folio_ids is not None:
                placeholders = ",".join("?" for _ in folio_ids)
                where_clause = f"AND to_id IN ({placeholders})"
                params = list(folio_ids)
            else:
                where_clause = ""

            query = query.format(where_clause=where_clause)
            cursor = conn.execute(query, params)
            return {row["folio_id"]: row["content"] for row in cursor.fetchall()}

    def get_latest_assignments(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent assignment for each folio in a single query.

        Returns dict mapping folio_id -> assigned_to (the to_id of the assignment thread).
        """
        with self._get_connection() as conn:
            query = """
                SELECT t.from_id AS folio_id, t.to_id AS assigned_to
                FROM threads t
                INNER JOIN (
                    SELECT from_id, MAX(created_at) AS max_created
                    FROM threads WHERE type = 'assignment'
                    {where_clause}
                    GROUP BY from_id
                ) latest ON t.from_id = latest.from_id
                        AND t.created_at = latest.max_created
                        AND t.type = 'assignment'
            """
            params = []
            if folio_ids is not None:
                placeholders = ",".join("?" for _ in folio_ids)
                where_clause = f"AND from_id IN ({placeholders})"
                params = list(folio_ids)
            else:
                where_clause = ""

            query = query.format(where_clause=where_clause)
            cursor = conn.execute(query, params)
            return {row["folio_id"]: row["assigned_to"] for row in cursor.fetchall()}

    # Folio Operations

    def save_folio(self, folio: Folio) -> bool:
        """Save or update a folio in SQLite."""
        with self._get_connection() as conn:
            created_at = (
                folio.created_at.isoformat()
                if isinstance(folio.created_at, datetime)
                else str(folio.created_at)
            )
            acknowledged_at = None
            if folio.acknowledged_at:
                acknowledged_at = (
                    folio.acknowledged_at.isoformat()
                    if isinstance(folio.acknowledged_at, datetime)
                    else str(folio.acknowledged_at)
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO folios
                (folio_id, type, site_id, created_at, created_by, title, content,
                 status, assigned_to, target_agent, omlet, archived, metadata,
                 acknowledged_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    folio.folio_id,
                    folio.type,
                    folio.site_id,
                    created_at,
                    folio.created_by,
                    folio.title,
                    folio.content,
                    folio.status or "open",
                    folio.assigned_to,
                    folio.target_agent,
                    folio.omlet,
                    1 if folio.archived else 0,
                    json.dumps(folio.metadata) if folio.metadata else "{}",
                    acknowledged_at,
                    folio.content_hash,
                ),
            )

            # FTS index updated automatically via triggers

            conn.commit()
        return True

    def get_folio(self, folio_id: str) -> Optional[Folio]:
        """Get a specific folio by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM folios WHERE folio_id = ?", (folio_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_folio(row)

    def get_folios(
        self,
        site_id: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        assigned_to: Optional[str] = None,
        created_by: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> List[Folio]:
        """Get folios with optional filters."""
        with self._get_connection() as conn:
            query = "SELECT * FROM folios WHERE 1=1"
            params = []

            if site_id:
                query += " AND site_id = ?"
                params.append(site_id)
            if type:
                query += " AND type = ?"
                params.append(type)
            if status:
                query += " AND status = ?"
                params.append(status)
            if assigned_to:
                query += " AND assigned_to = ?"
                params.append(assigned_to)
            if created_by:
                query += " AND created_by = ?"
                params.append(created_by)
            if archived is not None:
                query += " AND archived = ?"
                params.append(1 if archived else 0)

            query += " ORDER BY created_at DESC"

            cursor = conn.execute(query, params)
            return [self._row_to_folio(row) for row in cursor.fetchall()]

    def move_folio(self, folio_id: str, dest_site_id: str) -> Optional[Folio]:
        """Move a folio to a different site."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM folios WHERE folio_id = ?", (folio_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            conn.execute(
                "UPDATE folios SET site_id = ? WHERE folio_id = ?",
                (dest_site_id, folio_id),
            )
            conn.commit()

            # Return updated folio
            cursor = conn.execute(
                "SELECT * FROM folios WHERE folio_id = ?", (folio_id,)
            )
            return self._row_to_folio(cursor.fetchone())

    def search_folios(self, query: str, limit: int = 50) -> List[Folio]:
        """Full-text search across folios using FTS5."""
        with self._get_connection() as conn:
            # FTS5 query - escape special characters for safety
            fts_query = query.replace('"', '""')
            cursor = conn.execute(
                """
                SELECT folios.* FROM folios
                JOIN folios_fts ON folios.rowid = folios_fts.rowid
                WHERE folios_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """,
                (f'"{fts_query}"', limit),
            )
            return [self._row_to_folio(row) for row in cursor.fetchall()]

    def get_folio_count(self, site_id: Optional[str] = None) -> int:
        """Get count of folios, optionally by site."""
        with self._get_connection() as conn:
            if site_id:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM folios WHERE site_id = ?", (site_id,)
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM folios")
            return cursor.fetchone()[0]

    def get_folio_stats(self, site_id: Optional[str] = None) -> Dict[str, Any]:
        """Get folio statistics (type/status breakdowns), optionally by site."""
        with self._get_connection() as conn:
            base = "SELECT {} FROM folios"
            where = ""
            params = []
            if site_id:
                where = " WHERE site_id = ?"
                params = [site_id]

            # By type
            cursor = conn.execute(
                base.format("type, COUNT(*) as cnt") + where + " GROUP BY type",
                params,
            )
            by_type = {row["type"]: row["cnt"] for row in cursor.fetchall()}

            # By status
            cursor = conn.execute(
                base.format("status, COUNT(*) as cnt") + where + " GROUP BY status",
                params,
            )
            by_status = {row["status"]: row["cnt"] for row in cursor.fetchall()}

            # Total
            cursor = conn.execute(base.format("COUNT(*) as cnt") + where, params)
            total = cursor.fetchone()["cnt"]

            return {"total": total, "by_type": by_type, "by_status": by_status}

    def _row_to_folio(self, row: sqlite3.Row) -> Folio:
        """Convert a SQLite row to a Folio model."""
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        return Folio(
            folio_id=row["folio_id"],
            type=row["type"],
            site_id=row["site_id"],
            created_at=ensure_aware(row["created_at"]),
            created_by=row["created_by"],
            title=row["title"],
            content=row["content"],
            status=row["status"] or "open",
            assigned_to=row["assigned_to"],
            target_agent=row["target_agent"],
            omlet=row["omlet"],
            archived=bool(row["archived"]),
            metadata=metadata,
            acknowledged_at=ensure_aware(row["acknowledged_at"]),
            content_hash=row["content_hash"],
        )

    def migrate_folios_from_json(self, sites_dir: Path) -> int:
        """
        Migrate folio JSON files from all sites into SQLite.
        Returns count of migrated folios. Idempotent.
        """
        if not sites_dir.exists():
            return 0

        # Check if we already have data (idempotent)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM folios")
            existing_count = cursor.fetchone()[0]
            if existing_count > 0:
                logger.info(
                    f"Folios table already has {existing_count} rows, skipping migration"
                )
                return 0

        # Collect all folio JSON files
        folio_files = []
        for site_dir in sites_dir.iterdir():
            if site_dir.is_dir():
                folios_dir = site_dir / "folios"
                if folios_dir.exists():
                    folio_files.extend(folios_dir.glob("*.json"))

        if not folio_files:
            return 0

        logger.info(f"Migrating {len(folio_files)} folios from JSON to SQLite")

        count = 0
        errors = 0
        with self._get_connection() as conn:
            for folio_file in folio_files:
                try:
                    with open(folio_file) as f:
                        data = json.load(f)

                    # Handle missing fields gracefully
                    folio_id = data.get("folio_id", folio_file.stem)
                    metadata = data.get("metadata", {})

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO folios
                        (folio_id, type, site_id, created_at, created_by, title, content,
                         status, assigned_to, target_agent, omlet, archived, metadata,
                         acknowledged_at, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            folio_id,
                            data.get("type", "issue"),
                            data.get("site_id", folio_file.parent.parent.name),
                            data.get(
                                "created_at", datetime.now(timezone.utc).isoformat()
                            ),
                            data.get("created_by", "unknown"),
                            data.get("title", ""),
                            data.get("content", ""),
                            data.get("status", "open"),
                            data.get("assigned_to"),
                            data.get("target_agent"),
                            data.get("omlet"),
                            1 if data.get("archived", False) else 0,
                            json.dumps(metadata) if metadata else "{}",
                            data.get("acknowledged_at"),
                            data.get("content_hash"),
                        ),
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to migrate {folio_file.name}: {e}")
                    errors += 1

            # FTS index populated automatically via triggers on INSERT

            conn.commit()

        logger.info(f"Migrated {count} folios from JSON to SQLite ({errors} errors)")

        # Backup sites/ folio dirs by renaming
        for site_dir in sites_dir.iterdir():
            if site_dir.is_dir():
                folios_dir = site_dir / "folios"
                migrated_dir = site_dir / "folios_migrated"
                if folios_dir.exists() and not migrated_dir.exists():
                    folios_dir.rename(migrated_dir)
                    logger.info(f"Backed up {folios_dir} -> {migrated_dir}")

        return count

    def migrate_threads_from_json(self, threads_dir: Path) -> int:
        """Migrate thread JSON files into SQLite. Returns count of migrated threads."""
        if not threads_dir.exists():
            return 0

        json_files = list(threads_dir.glob("*.json"))
        if not json_files:
            return 0

        # Check if we already have data (idempotent)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM threads")
            existing_count = cursor.fetchone()[0]
            if existing_count > 0:
                logger.info(
                    f"Threads table already has {existing_count} rows, skipping migration"
                )
                return 0

        count = 0
        with self._get_connection() as conn:
            for thread_file in json_files:
                try:
                    with open(thread_file) as f:
                        data = json.load(f)

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO threads
                        (thread_id, from_id, to_id, type, content, weaver, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            data["thread_id"],
                            data["from_id"],
                            data["to_id"],
                            data["type"],
                            data.get("content"),
                            data.get("weaver"),
                            data["created_at"],
                        ),
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to migrate {thread_file.name}: {e}")

            conn.commit()

        logger.info(f"Migrated {count} threads from JSON to SQLite")

        # Rename old dir as backup
        migrated_dir = threads_dir.parent / "threads_migrated"
        if not migrated_dir.exists():
            threads_dir.rename(migrated_dir)
            logger.info(f"Renamed {threads_dir} to {migrated_dir}")

        return count


# JSON Storage for Structured Artifacts


class JSONStore:
    """Storage for roster (JSON), sites (JSON), folios/threads (SQLite)."""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir
        self.roster_dir = base_dir / "roster"
        self.sites_dir = base_dir / "sites"

        # Ensure directories exist
        self.roster_dir.mkdir(exist_ok=True)
        self.sites_dir.mkdir(exist_ok=True)

        # SQLite-backed storage for threads and folios
        db_path = base_dir / "skein.db"
        self._log_db = LogDatabase(db_path)

        # Auto-migrate folios from JSON to SQLite if needed
        self._log_db.migrate_folios_from_json(self.sites_dir)

    # Roster Operations

    def save_agent(self, agent: AgentInfo) -> bool:
        """Save agent registration."""
        agents_file = self.roster_dir / "agents.json"
        agents = self._load_json(agents_file, [])

        # Update or append
        existing_idx = next(
            (i for i, a in enumerate(agents) if a["agent_id"] == agent.agent_id), None
        )
        agent_dict = agent.model_dump(mode="json")

        if existing_idx is not None:
            agents[existing_idx] = agent_dict
        else:
            agents.append(agent_dict)

        self._save_json(agents_file, agents)
        return True

    def get_agents(self, status: Optional[str] = None) -> List[AgentInfo]:
        """Get registered agents, optionally filtered by status."""
        agents_file = self.roster_dir / "agents.json"
        agents_data = self._load_json(agents_file, [])
        agents = [AgentInfo(**self._normalize_datetime_fields(a)) for a in agents_data]

        if status is not None:
            agents = [a for a in agents if a.status == status]

        return agents

    def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get specific agent."""
        agents = self.get_agents()
        return next((a for a in agents if a.agent_id == agent_id), None)

    # Site Operations

    def save_site(self, site: Site) -> bool:
        """Save site metadata."""
        site_dir = self.sites_dir / site.site_id
        site_dir.mkdir(exist_ok=True)

        metadata_file = site_dir / "metadata.json"
        self._save_json(metadata_file, site.model_dump(mode="json"))

        # Ensure folios directory exists
        (site_dir / "folios").mkdir(exist_ok=True)
        return True

    def get_sites(self) -> List[Site]:
        """Get all sites."""
        sites = []
        for site_dir in self.sites_dir.iterdir():
            if site_dir.is_dir():
                metadata_file = site_dir / "metadata.json"
                if metadata_file.exists():
                    site_data = self._normalize_datetime_fields(
                        self._load_json(metadata_file)
                    )
                    sites.append(Site(**site_data))
        return sites

    def get_site(self, site_id: str) -> Optional[Site]:
        """Get specific site."""
        metadata_file = self.sites_dir / site_id / "metadata.json"
        if metadata_file.exists():
            return Site(
                **self._normalize_datetime_fields(self._load_json(metadata_file))
            )
        return None

    def update_site(
        self,
        site_id: str,
        status: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Site]:
        """Update site status and/or metadata."""
        site = self.get_site(site_id)
        if not site:
            return None

        if status is not None:
            site.status = status

        if metadata is not None:
            site.metadata.update(metadata)

        self.save_site(site)
        return site

    # Folio Operations (SQLite-backed)

    def save_folio(self, folio: Folio) -> bool:
        """Save folio to SQLite."""
        # Compute content hash if not present
        if not folio.content_hash and KNURL_AVAILABLE:
            folio.content_hash = compute_folio_hash(folio)

        return self._log_db.save_folio(folio)

    def get_folios(self, site_id: Optional[str] = None) -> List[Folio]:
        """Get folios, optionally filtered by site."""
        return self._log_db.get_folios(site_id=site_id)

    def get_folio(self, folio_id: str) -> Optional[Folio]:
        """Get specific folio by ID."""
        folio = self._log_db.get_folio(folio_id)
        if folio and not folio.content_hash and KNURL_AVAILABLE:
            folio.content_hash = compute_folio_hash(folio)
            self._log_db.save_folio(folio)
        return folio

    def move_folio(self, folio_id: str, dest_site_id: str) -> Optional[Folio]:
        """
        Move a folio to a different site.

        Returns the updated folio on success, None if folio not found.
        Raises ValueError if destination site doesn't exist.
        """
        # Verify destination site exists
        dest_site_dir = self.sites_dir / dest_site_id
        if not dest_site_dir.exists():
            raise ValueError(f"Destination site '{dest_site_id}' does not exist")

        return self._log_db.move_folio(folio_id, dest_site_id)

    def search_folios(self, query: str, limit: int = 50) -> List[Folio]:
        """Full-text search across folios."""
        return self._log_db.search_folios(query, limit=limit)

    # Thread Operations

    def save_thread(self, thread: Thread) -> bool:
        """Save thread to SQLite."""
        return self._log_db.save_thread(thread)

    def get_threads(
        self,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        type: Optional[str] = None,
        weaver: Optional[str] = None,
    ) -> List[Thread]:
        """Get threads with optional filters via SQLite."""
        return self._log_db.get_threads(
            from_id=from_id, to_id=to_id, type=type, weaver=weaver
        )

    def get_latest_statuses(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent status for each folio in a single query."""
        return self._log_db.get_latest_statuses(folio_ids)

    def get_latest_assignments(
        self, folio_ids: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """Get the most recent assignment for each folio in a single query."""
        return self._log_db.get_latest_assignments(folio_ids)

    # Helper methods

    def _normalize_datetime_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize datetime fields to be timezone-aware.

        Pydantic datetime parsing is inconsistent - some datetimes are parsed as
        timezone-aware, others as naive. This causes comparison errors.

        Convert all datetime strings to timezone-aware (UTC) format.
        """
        datetime_fields = ["created_at", "registered_at", "acknowledged_at", "read_at"]

        for field in datetime_fields:
            if field in data and data[field]:
                dt_str = data[field]
                # If it's already a datetime object, skip
                if isinstance(dt_str, datetime):
                    continue

                # Parse the datetime string
                try:
                    # Try parsing with timezone first
                    if dt_str.endswith("Z"):
                        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    elif "+" in dt_str or dt_str.count(":") > 2:
                        dt = datetime.fromisoformat(dt_str)
                    else:
                        # Naive datetime - assume UTC
                        dt = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)

                    # Convert to ISO format with timezone
                    data[field] = dt.isoformat()
                except (ValueError, AttributeError):
                    # If parsing fails, leave as-is
                    pass

        return data

    def _load_json(self, file_path: Path, default=None):
        """Load JSON file."""
        if not file_path.exists():
            return default if default is not None else {}

        with open(file_path, "r") as f:
            return json.load(f)

    def _save_json(self, file_path: Path, data):
        """Save JSON file."""
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2, default=str)


# Legacy module-level instances removed - use Depends(get_project_log_db) and Depends(get_project_store) in routes.py
