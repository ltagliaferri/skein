"""Shared definition and inspection of the collaborator bootstrap pack."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Optional

RAW_FILES = (
    "sigstore-pinned.txt",
    "interskein-pinned.txt",
    "interskein-primer.txt",
)
FILES = frozenset(name for raw in RAW_FILES for name in (raw, f"{raw}.sigstore.json"))


def pack_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "bootstrap"


def inventory(data_dir: str | Path) -> dict[str, Optional[str]]:
    """Return each allowed artifact's SHA256, or ``None`` when unreadable."""
    base = pack_dir(data_dir)
    hashes: dict[str, Optional[str]] = {}
    for name in FILES:
        try:
            payload = (base / name).read_bytes()
        except OSError:
            hashes[name] = None
        else:
            hashes[name] = sha256(payload).hexdigest()
    return hashes


def is_complete(items: dict[str, Optional[str]]) -> bool:
    return set(items) == FILES and all(items.values())
