#!/usr/bin/env python3
"""
Migration: Normalize naive datetime strings in SKEIN SQLite databases to UTC-aware.

Finds all datetime columns and appends +00:00 to naive datetime strings.
Idempotent - safe to run multiple times.

Usage:
    python -m skein.migrations.normalize_datetimes /path/to/.skein/data/skein.db
    python -m skein.migrations.normalize_datetimes --dry-run /path/to/.skein/data/skein.db
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

# Columns known to contain datetime values
DATETIME_COLUMNS = {
    "created_at",
    "registered_at",
    "updated_at",
    "acknowledged_at",
    "read_at",
    "timestamp",
}

# Pattern: ISO datetime without timezone info (e.g. "2025-12-01T10:30:00" or "2025-12-01 10:30:00.123456")
# Must NOT already have +XX:XX or Z suffix
NAIVE_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$")


def find_datetime_columns(conn):
    """Return {table_name: [col_names]} for columns that look like datetime fields."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]

    result = {}
    for table in tables:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cursor.fetchall()]
        dt_cols = [c for c in cols if c in DATETIME_COLUMNS]
        if dt_cols:
            result[table] = dt_cols
    return result


def normalize_db(db_path: Path, dry_run: bool = False) -> dict[str, int]:
    """Normalize naive datetimes to UTC-aware. Returns {table.column: count_updated}."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    table_cols = find_datetime_columns(conn)
    stats = {}

    for table, columns in table_cols.items():
        # Find the primary key for this table
        cursor = conn.execute(f"PRAGMA table_info({table})")
        pk_cols = [row[1] for row in cursor.fetchall() if row[5] > 0]
        if not pk_cols:
            # Fall back to rowid
            pk_cols = ["rowid"]

        for col in columns:
            # Find rows with naive datetimes (no +, no Z at end)
            query = f"SELECT rowid, [{col}] FROM [{table}] WHERE [{col}] IS NOT NULL"
            cursor = conn.execute(query)
            rows = cursor.fetchall()

            count = 0
            for row in rows:
                val = row[1]  # col value by index (Row doesn't expose rowid by name)
                rowid = row[0]
                if val and isinstance(val, str) and NAIVE_DATETIME_RE.match(val):
                    new_val = val + "+00:00"
                    if not dry_run:
                        conn.execute(
                            f"UPDATE [{table}] SET [{col}] = ? WHERE rowid = ?",
                            (new_val, rowid),
                        )
                    count += 1

            if count > 0:
                stats[f"{table}.{col}"] = count

    if not dry_run:
        conn.commit()
    conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Normalize naive datetime strings in SKEIN SQLite DB to UTC-aware"
    )
    parser.add_argument("db_path", type=Path, help="Path to skein.db file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without modifying the database",
    )
    args = parser.parse_args()

    if not args.db_path.exists():
        print(f"Error: {args.db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    mode = "DRY RUN" if args.dry_run else "MIGRATING"
    print(f"[{mode}] {args.db_path}")

    stats = normalize_db(args.db_path, dry_run=args.dry_run)

    if stats:
        total = 0
        for key, count in sorted(stats.items()):
            print(f"  {key}: {count} rows {'would be ' if args.dry_run else ''}updated")
            total += count
        print(f"  Total: {total} rows {'would be ' if args.dry_run else ''}updated")
    else:
        print("  No naive datetimes found - database is already normalized")


if __name__ == "__main__":
    main()
