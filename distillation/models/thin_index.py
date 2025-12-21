"""
=============================================================================
THIN INDEX TYPE DEFINITIONS
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Defines the thin index structure - a compressed summary of all entries
that fits within ~3000 tokens for fast context injection.

The thin index enables Claude to quickly see what topics and projects
exist without loading full entries.

INPUT FILES:
- None (type definitions only)

OUTPUT FILES:
- None (type definitions only)

USAGE:
    from distillation.types import ThinIndex, ThinIndexTopic
=============================================================================
"""

from dataclasses import dataclass, field
from typing import Literal, Optional


# -----------------------------------------------------------------------------
# THIN INDEX TOPIC - Compressed knowledge entry summary
# -----------------------------------------------------------------------------
@dataclass
class ThinIndexTopic:
    """
    A compressed summary of a knowledge entry for the thin index.
    Max ~80 chars for current_view_summary to stay within token budget.
    """
    id: str                       # ke_uuid - for retrieval resolution
    domain: str                   # Topic area
    current_view_summary: str     # Max 80 chars
    state: Literal["active", "contested", "stale"]
    confidence: Literal["high", "medium", "low"]
    last_updated: str             # ISO8601
    top_repo: Optional[str] = None  # Most relevant repo
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "domain": self.domain,
            "current_view_summary": self.current_view_summary,
            "state": self.state,
            "confidence": self.confidence,
            "last_updated": self.last_updated,
            "top_repo": self.top_repo,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ThinIndexTopic":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            domain=data["domain"],
            current_view_summary=data["current_view_summary"],
            state=data["state"],
            confidence=data["confidence"],
            last_updated=data["last_updated"],
            top_repo=data.get("top_repo"),
        )


# -----------------------------------------------------------------------------
# THIN INDEX PROJECT - Compressed project entry summary
# -----------------------------------------------------------------------------
@dataclass
class ThinIndexProject:
    """
    A compressed summary of a project entry for the thin index.
    Max ~80 chars for goal_summary to stay within token budget.
    """
    id: str                       # pe_uuid - for retrieval resolution
    name: str                     # Project name
    status: Literal["active", "paused", "completed", "abandoned"]
    goal_summary: str             # Max 80 chars
    current_phase: str
    blocked_on: Optional[str] = None
    last_touched: str = ""        # ISO8601
    primary_repo: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "goal_summary": self.goal_summary,
            "current_phase": self.current_phase,
            "blocked_on": self.blocked_on,
            "last_touched": self.last_touched,
            "primary_repo": self.primary_repo,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ThinIndexProject":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            status=data["status"],
            goal_summary=data["goal_summary"],
            current_phase=data["current_phase"],
            blocked_on=data.get("blocked_on"),
            last_touched=data.get("last_touched", ""),
            primary_repo=data.get("primary_repo"),
        )


# -----------------------------------------------------------------------------
# THIN INDEX EVOLUTION - Recent changes summary
# -----------------------------------------------------------------------------
@dataclass
class ThinIndexEvolution:
    """
    A compressed summary of a recent evolution (change in thinking).
    Max ~60 chars for delta_summary.
    """
    entry_id: str                 # ke_uuid or pe_uuid
    entry_type: Literal["knowledge", "project"]
    domain_or_name: str           # Topic domain or project name
    delta_summary: str            # Max 60 chars - what changed
    date: str                     # ISO8601
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "entry_id": self.entry_id,
            "entry_type": self.entry_type,
            "domain_or_name": self.domain_or_name,
            "delta_summary": self.delta_summary,
            "date": self.date,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ThinIndexEvolution":
        """Create from dictionary."""
        return cls(
            entry_id=data["entry_id"],
            entry_type=data["entry_type"],
            domain_or_name=data["domain_or_name"],
            delta_summary=data["delta_summary"],
            date=data["date"],
        )


# -----------------------------------------------------------------------------
# THIN INDEX - The complete compressed index
# -----------------------------------------------------------------------------
@dataclass
class ThinIndex:
    """
    The complete thin index - a compressed view of all knowledge.
    
    Constraint: Must serialize to <3000 tokens.
    Token count is measured using cl100k_base tokenizer.
    """
    generated_at: str             # ISO8601 - when this index was created
    token_count: int = 0          # Actual token count after generation
    topics: list[ThinIndexTopic] = field(default_factory=list)
    projects: list[ThinIndexProject] = field(default_factory=list)
    recent_evolutions: list[ThinIndexEvolution] = field(default_factory=list)
    contested_count: int = 0      # Number of entries in contested state
    
    @property
    def topic_count(self) -> int:
        """Number of topics in the index."""
        return len(self.topics)
    
    @property
    def project_count(self) -> int:
        """Number of projects in the index."""
        return len(self.projects)
    
    @property
    def active_project_count(self) -> int:
        """Number of active projects."""
        return sum(1 for p in self.projects if p.status == "active")
    
    def get_summary(self) -> str:
        """Get a one-line summary of the index."""
        return f"{self.topic_count} topics, {self.project_count} projects ({self.active_project_count} active)"
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "generated_at": self.generated_at,
            "token_count": self.token_count,
            "topics": [t.to_dict() for t in self.topics],
            "projects": [p.to_dict() for p in self.projects],
            "recent_evolutions": [e.to_dict() for e in self.recent_evolutions],
            "contested_count": self.contested_count,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ThinIndex":
        """Create from dictionary (JSON deserialization)."""
        topics = [ThinIndexTopic.from_dict(t) for t in data.get("topics", [])]
        projects = [ThinIndexProject.from_dict(p) for p in data.get("projects", [])]
        evolutions = [ThinIndexEvolution.from_dict(e) for e in data.get("recent_evolutions", [])]
        
        return cls(
            generated_at=data["generated_at"],
            token_count=data.get("token_count", 0),
            topics=topics,
            projects=projects,
            recent_evolutions=evolutions,
            contested_count=data.get("contested_count", 0),
        )
    
    def __repr__(self) -> str:
        return f"ThinIndex({self.get_summary()}, {self.token_count} tokens)"

