"""
SKEIN address resolution.

Parses colon-based addresses like "speakbot:brief-20251226-xyz" into
a project reference and folio ID. Bare IDs (no colon) resolve to the
current project first, then cascade across all projects.

Address layers (only 1-3 implemented now):
  1. Bare     — brief-20251226-n1br
  2. Site     — (skipped, sites are more like tags)
  3. Project  — speakbot:brief-20251226-n1br
  4. Peer     — @patrick:brief-20251226-n1br (future)
  5. URL      — https://x.com:brief-20251226-n1br (future)
  6. Hash     — knurl content hash (future)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedAddress:
    """Result of parsing a SKEIN address."""

    folio_id: str
    project: Optional[str] = None

    @property
    def is_qualified(self) -> bool:
        """True if address includes an explicit project."""
        return self.project is not None


def parse(address: str) -> ParsedAddress:
    """
    Parse a SKEIN address string.

    "brief-20251226-xyz"          → ParsedAddress(folio_id="brief-20251226-xyz")
    "speakbot:brief-20251226-xyz" → ParsedAddress(project="speakbot", folio_id="brief-20251226-xyz")
    """
    if ":" not in address:
        return ParsedAddress(folio_id=address)

    project, _, folio_id = address.partition(":")

    # Future: @peer and https:// prefixes — for now, only bare project names
    if project.startswith("@") or project.startswith("http"):
        # Not a project-scoped address, treat the whole thing as a bare ID
        return ParsedAddress(folio_id=address)

    if not project or not folio_id:
        # Malformed — treat as bare
        return ParsedAddress(folio_id=address)

    return ParsedAddress(project=project, folio_id=folio_id)
