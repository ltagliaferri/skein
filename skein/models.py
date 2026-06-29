"""
SKEIN data models using Pydantic.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


# Roster Models

AgentType = Literal["claude-code", "patbot", "horizon", "human", "system"]


class AgentRegistration(BaseModel):
    agent_id: str
    name: Optional[str] = None
    agent_type: Optional[AgentType] = None
    description: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = "active"  # "orienting" or "active"


class AgentInfo(BaseModel):
    agent_id: str
    name: Optional[str] = None
    agent_type: Optional[AgentType] = None
    description: Optional[str] = None
    registered_at: datetime
    capabilities: List[str]
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Site Models


class SiteCreate(BaseModel):
    site_id: str
    purpose: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SiteUpdate(BaseModel):
    status: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class Site(BaseModel):
    site_id: str
    created_at: datetime
    created_by: str
    purpose: str
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Folio Models

FolioType = Literal[
    "issue",
    "friction",
    "brief",
    "summary",
    "finding",
    "notion",
    "tender",
    "playbook",
    "mantle",
    "writ",
    "plan",
    "hypothesis",
]


class FolioCreate(BaseModel):
    type: FolioType
    site_id: str
    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    assigned_to: Optional[str] = None
    target_agent: Optional[str] = None  # For briefs
    omlet: Optional[str] = (
        None  # Reference to agent execution (strand_id/agent_id/turn-N)
    )


class Folio(BaseModel):
    folio_id: str
    type: FolioType
    site_id: str
    created_at: datetime
    created_by: str
    title: str
    content: str
    status: str = "open"
    assigned_to: Optional[str] = None
    target_agent: Optional[str] = None
    omlet: Optional[str] = (
        None  # Reference to agent execution (strand_id/agent_id/turn-N)
    )
    archived: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    acknowledged_at: Optional[datetime] = None
    content_hash: Optional[str] = None  # Content-addressable hash of immutable fields
    source_project: Optional[str] = (
        None  # Runtime-only: set when resolved from another project. Never persisted to the database.
    )


class VersionView(BaseModel):
    """A by-hash fetch result (§8): a version's five IMMUTABLE identity fields plus
    flags, and NO mutable control (status/assigned_to/etc.) — a hash addresses
    content, not a lineage's state. Distinct from Folio so a normal folio read is
    never polluted with these by-hash-only fields.

      - is_head: True if this version is currently some ref's head.
      - lineage_head: the current head hash of this version's lineage (so a consumer
        holding a superseded hash can find what is current). Itself when is_head.
    """

    content_hash: str
    type: FolioType
    title: str
    content: str
    created_at: datetime
    created_by: str
    is_head: bool
    lineage_head: Optional[str] = None


class FolioUpdate(BaseModel):
    """Model for updating a folio's mutable fields."""

    title: Optional[str] = None
    content: Optional[str] = None
    status: Optional[str] = None
    assigned_to: Optional[str] = None
    archived: Optional[bool] = None


# Thread Models

ThreadType = Literal[
    "message",
    "mention",
    "reference",
    "assignment",
    "succession",
    "reply",
    "tag",
    "status",
    # Phase 2 edit-as-commit edges. Endpoints are content HASHES, not slugs —
    # the first hash-keyed edges in the table. Endpoint-resolution surfaces
    # (orphan detection) must exclude these two types or they report every edit
    # edge as broken.
    "supersedes",  # the edit edge: from_id = new hash, to_id = old head hash
    "reverted",    # the revert marker: from_id = prior head hash, to_id = reused hash
]


class ThreadCreate(BaseModel):
    from_id: str  # Any resource ID (agent, folio, etc)
    to_id: str  # Any resource ID
    type: ThreadType
    content: Optional[str] = None
    weaver: Optional[str] = None  # Agent who created this connection


class Thread(BaseModel):
    thread_id: str
    from_id: str
    to_id: str
    type: ThreadType
    content: Optional[str] = None
    weaver: Optional[str] = None  # Agent who created this connection
    created_at: datetime


# Log Models


class LogEntry(BaseModel):
    stream_id: str
    level: str = "INFO"
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LogBatch(BaseModel):
    stream_id: str
    source: str
    lines: List[LogEntry]


class LogLine(BaseModel):
    id: int
    stream_id: str
    timestamp: datetime
    level: str
    source: str
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Screenshot Models


class ScreenshotCreate(BaseModel):
    screenshot_data: str  # base64 PNG
    strand_id: str
    turn_number: Optional[int] = None
    label: str = "auto"


class Screenshot(BaseModel):
    screenshot_id: str
    strand_id: str
    timestamp: datetime
    turn_number: Optional[int] = None
    label: str
    file_path: str
    file_size: int
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Yield/Sack Models (chain data passing)


class YieldCreate(BaseModel):
    """Data passed when an agent yields."""

    status: str  # complete, blocked, failed, etc.
    outcome: str = ""  # Summary of what was accomplished
    artifacts: List[str] = Field(default_factory=list)  # File paths, folio IDs, etc.
    notes: Optional[str] = None  # Additional context


class Yield(BaseModel):
    """A yield stored in the chain's sack."""

    sack_id: str
    chain_id: str
    task_id: str
    agent_id: Optional[str] = None
    timestamp: datetime
    status: str
    outcome: str = ""
    artifacts: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    duration_seconds: Optional[int] = None
    tokens_used: Optional[int] = None
    shard_path: Optional[str] = None
    tender_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
