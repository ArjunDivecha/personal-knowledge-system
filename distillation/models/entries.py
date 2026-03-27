"""
=============================================================================
KNOWLEDGE ENTRY TYPE DEFINITIONS
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Defines the core data structures for knowledge and project entries.
These match the schemas defined in prd-distillation-v1.1.md Section 3.

INPUT FILES:
- None (type definitions only)

OUTPUT FILES:
- None (type definitions only)

USAGE:
    from distillation.types import KnowledgeEntry, ProjectEntry
=============================================================================
"""

from dataclasses import dataclass, field
from typing import Literal, Optional
from datetime import datetime

MEMORY_SCHEMA_VERSION = 2


def _coerce_int(value: object, default: int = 0) -> int:
    """Best-effort integer coercion for metadata loaded from Redis JSON."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_int(value: object) -> Optional[int]:
    """Best-effort optional integer coercion."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: object) -> Optional[float]:
    """Best-effort optional float coercion."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_knowledge_metadata_dict(metadata: Optional[dict]) -> dict:
    """Apply Phase 1 schema defaults to a knowledge metadata block."""
    meta = dict(metadata or {})
    updated_at = meta.get("updated_at") or meta.get("created_at") or ""
    created_at = meta.get("created_at") or updated_at

    normalized = dict(meta)
    normalized["created_at"] = created_at
    normalized["updated_at"] = updated_at
    normalized["source_conversations"] = list(meta.get("source_conversations") or [])
    normalized["source_messages"] = list(meta.get("source_messages") or [])
    normalized["access_count"] = _coerce_int(meta.get("access_count"), 0)
    normalized["last_accessed"] = meta.get("last_accessed")
    normalized["schema_version"] = _coerce_int(meta.get("schema_version"), MEMORY_SCHEMA_VERSION)
    normalized["classification_status"] = meta.get("classification_status") or "pending"
    normalized["context_type"] = meta.get("context_type")
    normalized["mention_count"] = _coerce_optional_int(meta.get("mention_count"))
    normalized["first_seen"] = meta.get("first_seen")
    normalized["last_seen"] = meta.get("last_seen")
    normalized["auto_inferred"] = meta.get("auto_inferred") if isinstance(meta.get("auto_inferred"), bool) else None
    normalized["source_weights"] = dict(meta.get("source_weights")) if isinstance(meta.get("source_weights"), dict) else {}
    normalized["injection_tier"] = _coerce_optional_int(meta.get("injection_tier"))
    normalized["salience_score"] = _coerce_optional_float(meta.get("salience_score"))
    normalized["last_consolidated"] = meta.get("last_consolidated")
    normalized["consolidation_notes"] = list(meta.get("consolidation_notes") or [])
    normalized["archived"] = bool(meta.get("archived", False))
    return normalized


def normalize_project_metadata_dict(metadata: Optional[dict]) -> dict:
    """Apply Phase 1 schema defaults to a project metadata block."""
    meta = dict(metadata or {})
    updated_at = meta.get("updated_at") or meta.get("last_touched") or meta.get("created_at") or ""
    created_at = meta.get("created_at") or updated_at
    last_touched = meta.get("last_touched") or updated_at

    normalized = dict(meta)
    normalized["created_at"] = created_at
    normalized["updated_at"] = updated_at
    normalized["source_conversations"] = list(meta.get("source_conversations") or [])
    normalized["source_messages"] = list(meta.get("source_messages") or [])
    normalized["last_touched"] = last_touched
    normalized["access_count"] = _coerce_int(meta.get("access_count"), 0)
    normalized["last_accessed"] = meta.get("last_accessed")
    normalized["schema_version"] = _coerce_int(meta.get("schema_version"), MEMORY_SCHEMA_VERSION)
    normalized["classification_status"] = meta.get("classification_status") or "pending"
    normalized["context_type"] = meta.get("context_type")
    normalized["mention_count"] = _coerce_optional_int(meta.get("mention_count"))
    normalized["first_seen"] = meta.get("first_seen")
    normalized["last_seen"] = meta.get("last_seen")
    normalized["auto_inferred"] = meta.get("auto_inferred") if isinstance(meta.get("auto_inferred"), bool) else None
    normalized["source_weights"] = dict(meta.get("source_weights")) if isinstance(meta.get("source_weights"), dict) else {}
    normalized["injection_tier"] = _coerce_optional_int(meta.get("injection_tier"))
    normalized["salience_score"] = _coerce_optional_float(meta.get("salience_score"))
    normalized["last_consolidated"] = meta.get("last_consolidated")
    normalized["consolidation_notes"] = list(meta.get("consolidation_notes") or [])
    normalized["archived"] = bool(meta.get("archived", False))
    return normalized


# -----------------------------------------------------------------------------
# EVIDENCE - Links insights back to source messages
# -----------------------------------------------------------------------------
@dataclass
class Evidence:
    """
    Provenance tracking - links an extracted insight to the original messages.
    Every insight MUST have evidence to be valid.
    """
    conversation_id: str          # Original conversation UUID
    message_ids: list[str]        # List of message UUIDs that support this
    snippet: str                  # Max 200 chars - key quote from the message


# -----------------------------------------------------------------------------
# KNOWLEDGE ENTRY COMPONENTS
# -----------------------------------------------------------------------------
@dataclass
class Insight:
    """A specific learning or conclusion with evidence."""
    insight: str                  # The extracted knowledge
    evidence: Evidence            # Where this came from


@dataclass
class Capability:
    """A practical skill the user demonstrated."""
    capability: str               # What the user knows how to do
    evidence: Evidence            # Where this was demonstrated


@dataclass
class OpenQuestion:
    """An unresolved question from the conversation."""
    question: str                 # The question
    context: Optional[str] = None # Additional context
    evidence: Optional[Evidence] = None


@dataclass
class RepoLink:
    """Link to a GitHub repository."""
    repo: str                     # owner/repo format
    path: Optional[str] = None    # Specific folder/file path
    link_type: Literal["explicit", "semantic"] = "explicit"
    confidence: float = 1.0       # 0.0 to 1.0
    evidence: Optional[str] = None  # Why this link exists


@dataclass
class Position:
    """
    A specific view/position on a topic at a point in time.
    Used for tracking evolution and contested states.
    """
    view: str                     # The position/view
    confidence: Literal["high", "medium", "low"]
    as_of: str                    # ISO8601 timestamp
    evidence: Evidence            # What supports this view


@dataclass
class Evolution:
    """
    Record of how thinking changed over time.
    Preserved even when entries are compressed.
    """
    delta: str                    # What changed
    trigger: str                  # Why it changed (conversation topic)
    from_view: str                # Previous position
    to_view: str                  # New position
    date: str                     # ISO8601 timestamp
    evidence: Evidence            # Messages that triggered the change


@dataclass
class KnowledgeMetadata:
    """Metadata tracking for knowledge entries."""
    created_at: str               # ISO8601
    updated_at: str               # ISO8601
    source_conversations: list[str]  # All conversation IDs that contributed
    source_messages: list[str]    # All message IDs referenced
    access_count: int = 0         # How many times retrieved
    last_accessed: Optional[str] = None  # ISO8601
    schema_version: int = MEMORY_SCHEMA_VERSION
    classification_status: str = "pending"
    context_type: Optional[str] = None
    mention_count: Optional[int] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    auto_inferred: Optional[bool] = None
    source_weights: dict[str, float] = field(default_factory=dict)
    injection_tier: Optional[int] = None
    salience_score: Optional[float] = None
    last_consolidated: Optional[str] = None
    consolidation_notes: list[str] = field(default_factory=list)
    archived: bool = False


# -----------------------------------------------------------------------------
# KNOWLEDGE ENTRY - Main knowledge record
# -----------------------------------------------------------------------------
@dataclass
class KnowledgeEntry:
    """
    A structured knowledge entry extracted from conversations.
    
    States:
    - active: Current, reliable knowledge
    - contested: Has conflicting positions that need resolution
    - stale: Old, may be outdated
    - deprecated: No longer relevant
    
    Detail levels:
    - full: Complete entry with all evidence
    - compressed: Summarized, full content archived
    """
    # Identity
    id: str                       # ke_uuid format
    domain: str                   # Specific topic (e.g., "MLX layer selection")
    type: Literal["knowledge"] = "knowledge"
    
    # Classification
    subdomain: Optional[str] = None
    
    # State
    state: Literal["active", "contested", "stale", "deprecated"] = "active"
    detail_level: Literal["full", "compressed"] = "full"
    
    # Current position (for fast retrieval)
    current_view: str = ""        # 1-3 sentences
    confidence: Literal["high", "medium", "low"] = "medium"
    
    # All positions (for contested states or history)
    positions: list[Position] = field(default_factory=list)
    
    # Structured knowledge with provenance
    key_insights: list[Insight] = field(default_factory=list)
    knows_how_to: list[Capability] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)
    
    # Linkages
    related_repos: list[RepoLink] = field(default_factory=list)
    related_knowledge: list[dict] = field(default_factory=list)  # {knowledge_id, relationship}
    
    # Evolution tracking
    evolution: list[Evolution] = field(default_factory=list)
    
    # Metadata
    metadata: Optional[KnowledgeMetadata] = None
    
    # Archive reference (when compressed)
    full_content_ref: Optional[str] = None  # Path to archived full content
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type,
            "domain": self.domain,
            "subdomain": self.subdomain,
            "state": self.state,
            "detail_level": self.detail_level,
            "current_view": self.current_view,
            "confidence": self.confidence,
            "positions": [
                {
                    "view": p.view,
                    "confidence": p.confidence,
                    "as_of": p.as_of,
                    "evidence": {
                        "conversation_id": p.evidence.conversation_id,
                        "message_ids": p.evidence.message_ids,
                        "snippet": p.evidence.snippet,
                    }
                } for p in self.positions
            ],
            "key_insights": [
                {
                    "insight": i.insight,
                    "evidence": {
                        "conversation_id": i.evidence.conversation_id,
                        "message_ids": i.evidence.message_ids,
                        "snippet": i.evidence.snippet,
                    }
                } for i in self.key_insights
            ],
            "knows_how_to": [
                {
                    "capability": c.capability,
                    "evidence": {
                        "conversation_id": c.evidence.conversation_id,
                        "message_ids": c.evidence.message_ids,
                        "snippet": c.evidence.snippet,
                    }
                } for c in self.knows_how_to
            ],
            "open_questions": [
                {
                    "question": q.question,
                    "context": q.context,
                    "evidence": {
                        "conversation_id": q.evidence.conversation_id,
                        "message_ids": q.evidence.message_ids,
                        "snippet": q.evidence.snippet,
                    } if q.evidence else None
                } for q in self.open_questions
            ],
            "related_repos": [
                {
                    "repo": r.repo,
                    "path": r.path,
                    "link_type": r.link_type,
                    "confidence": r.confidence,
                    "evidence": r.evidence,
                } for r in self.related_repos
            ],
            "related_knowledge": self.related_knowledge,
            "evolution": [
                {
                    "delta": e.delta,
                    "trigger": e.trigger,
                    "from_view": e.from_view,
                    "to_view": e.to_view,
                    "date": e.date,
                    "evidence": {
                        "conversation_id": e.evidence.conversation_id,
                        "message_ids": e.evidence.message_ids,
                        "snippet": e.evidence.snippet,
                    }
                } for e in self.evolution
            ],
            "metadata": normalize_knowledge_metadata_dict({
                "created_at": self.metadata.created_at,
                "updated_at": self.metadata.updated_at,
                "source_conversations": self.metadata.source_conversations,
                "source_messages": self.metadata.source_messages,
                "access_count": self.metadata.access_count,
                "last_accessed": self.metadata.last_accessed,
                "schema_version": self.metadata.schema_version,
                "classification_status": self.metadata.classification_status,
                "context_type": self.metadata.context_type,
                "mention_count": self.metadata.mention_count,
                "first_seen": self.metadata.first_seen,
                "last_seen": self.metadata.last_seen,
                "auto_inferred": self.metadata.auto_inferred,
                "source_weights": self.metadata.source_weights,
                "injection_tier": self.metadata.injection_tier,
                "salience_score": self.metadata.salience_score,
                "last_consolidated": self.metadata.last_consolidated,
                "consolidation_notes": self.metadata.consolidation_notes,
                "archived": self.metadata.archived,
            }) if self.metadata else None,
            "full_content_ref": self.full_content_ref,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeEntry":
        """Create from dictionary (JSON deserialization)."""
        # Parse positions
        positions = []
        for p in data.get("positions", []):
            ev = p.get("evidence", {})
            positions.append(Position(
                view=p["view"],
                confidence=p["confidence"],
                as_of=p["as_of"],
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                )
            ))
        
        # Parse insights
        key_insights = []
        for i in data.get("key_insights", []):
            ev = i.get("evidence", {})
            key_insights.append(Insight(
                insight=i["insight"],
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                )
            ))
        
        # Parse capabilities
        knows_how_to = []
        for c in data.get("knows_how_to", []):
            ev = c.get("evidence", {})
            knows_how_to.append(Capability(
                capability=c["capability"],
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                )
            ))
        
        # Parse questions
        open_questions = []
        for q in data.get("open_questions", []):
            ev = q.get("evidence")
            open_questions.append(OpenQuestion(
                question=q["question"],
                context=q.get("context"),
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                ) if ev else None
            ))
        
        # Parse repos
        related_repos = []
        for r in data.get("related_repos", []):
            related_repos.append(RepoLink(
                repo=r["repo"],
                path=r.get("path"),
                link_type=r.get("link_type", "explicit"),
                confidence=r.get("confidence", 1.0),
                evidence=r.get("evidence"),
            ))
        
        # Parse evolution
        evolution = []
        for e in data.get("evolution", []):
            ev = e.get("evidence", {})
            evolution.append(Evolution(
                delta=e["delta"],
                trigger=e["trigger"],
                from_view=e["from_view"],
                to_view=e["to_view"],
                date=e["date"],
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                )
            ))
        
        # Parse metadata
        meta_data = normalize_knowledge_metadata_dict(data.get("metadata"))
        metadata = KnowledgeMetadata(
            created_at=meta_data.get("created_at", ""),
            updated_at=meta_data.get("updated_at", ""),
            source_conversations=meta_data.get("source_conversations", []),
            source_messages=meta_data.get("source_messages", []),
            access_count=meta_data.get("access_count", 0),
            last_accessed=meta_data.get("last_accessed"),
            schema_version=meta_data.get("schema_version", MEMORY_SCHEMA_VERSION),
            classification_status=meta_data.get("classification_status", "pending"),
            context_type=meta_data.get("context_type"),
            mention_count=meta_data.get("mention_count"),
            first_seen=meta_data.get("first_seen"),
            last_seen=meta_data.get("last_seen"),
            auto_inferred=meta_data.get("auto_inferred"),
            source_weights=meta_data.get("source_weights", {}),
            injection_tier=meta_data.get("injection_tier"),
            salience_score=meta_data.get("salience_score"),
            last_consolidated=meta_data.get("last_consolidated"),
            consolidation_notes=meta_data.get("consolidation_notes", []),
            archived=meta_data.get("archived", False),
        ) if data.get("metadata") else None
        
        return cls(
            id=data["id"],
            type=data.get("type", "knowledge"),
            domain=data["domain"],
            subdomain=data.get("subdomain"),
            state=data.get("state", "active"),
            detail_level=data.get("detail_level", "full"),
            current_view=data.get("current_view", ""),
            confidence=data.get("confidence", "medium"),
            positions=positions,
            key_insights=key_insights,
            knows_how_to=knows_how_to,
            open_questions=open_questions,
            related_repos=related_repos,
            related_knowledge=data.get("related_knowledge", []),
            evolution=evolution,
            metadata=metadata,
            full_content_ref=data.get("full_content_ref"),
        )


# -----------------------------------------------------------------------------
# PROJECT ENTRY COMPONENTS
# -----------------------------------------------------------------------------
@dataclass
class Decision:
    """A decision made in the context of a project."""
    decision: str                 # What was decided
    rationale: Optional[str] = None  # Why (if stated)
    date: str = ""                # ISO8601
    evidence: Optional[Evidence] = None


@dataclass
class ProjectMetadata:
    """Metadata tracking for project entries."""
    created_at: str               # ISO8601
    updated_at: str               # ISO8601
    source_conversations: list[str]
    source_messages: list[str]
    last_touched: str             # Most recent activity
    access_count: int = 0
    last_accessed: Optional[str] = None
    schema_version: int = MEMORY_SCHEMA_VERSION
    classification_status: str = "pending"
    context_type: Optional[str] = None
    mention_count: Optional[int] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    auto_inferred: Optional[bool] = None
    source_weights: dict[str, float] = field(default_factory=dict)
    injection_tier: Optional[int] = None
    salience_score: Optional[float] = None
    last_consolidated: Optional[str] = None
    consolidation_notes: list[str] = field(default_factory=list)
    archived: bool = False


# -----------------------------------------------------------------------------
# PROJECT ENTRY - Active project record
# -----------------------------------------------------------------------------
@dataclass
class ProjectEntry:
    """
    A project entry tracking ongoing work.
    
    States:
    - active: Currently being worked on
    - paused: Temporarily stopped
    - completed: Successfully finished
    - abandoned: Stopped without completion
    """
    # Identity
    id: str                       # pe_uuid format
    type: Literal["project"] = "project"
    name: str = ""                # Project name
    
    # State
    status: Literal["active", "paused", "completed", "abandoned"] = "active"
    detail_level: Literal["full", "compressed"] = "full"
    
    # Current state
    goal: str = ""                # 1-2 sentences
    current_phase: str = ""       # e.g., "architecture", "implementation"
    blocked_on: Optional[str] = None
    
    # Decisions with provenance
    decisions_made: list[Decision] = field(default_factory=list)
    
    # Technical context
    tech_stack: list[str] = field(default_factory=list)
    
    # Linkages
    related_repos: list[RepoLink] = field(default_factory=list)
    related_knowledge: list[dict] = field(default_factory=list)
    
    # History
    phase_history: list[dict] = field(default_factory=list)
    
    # Metadata
    metadata: Optional[ProjectMetadata] = None
    
    # Archive reference
    full_content_ref: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "status": self.status,
            "detail_level": self.detail_level,
            "goal": self.goal,
            "current_phase": self.current_phase,
            "blocked_on": self.blocked_on,
            "decisions_made": [
                {
                    "decision": d.decision,
                    "rationale": d.rationale,
                    "date": d.date,
                    "evidence": {
                        "conversation_id": d.evidence.conversation_id,
                        "message_ids": d.evidence.message_ids,
                        "snippet": d.evidence.snippet,
                    } if d.evidence else None
                } for d in self.decisions_made
            ],
            "tech_stack": self.tech_stack,
            "related_repos": [
                {
                    "repo": r.repo,
                    "path": r.path,
                    "link_type": r.link_type,
                    "confidence": r.confidence,
                    "evidence": r.evidence,
                } for r in self.related_repos
            ],
            "related_knowledge": self.related_knowledge,
            "phase_history": self.phase_history,
            "metadata": normalize_project_metadata_dict({
                "created_at": self.metadata.created_at,
                "updated_at": self.metadata.updated_at,
                "source_conversations": self.metadata.source_conversations,
                "source_messages": self.metadata.source_messages,
                "last_touched": self.metadata.last_touched,
                "access_count": self.metadata.access_count,
                "last_accessed": self.metadata.last_accessed,
                "schema_version": self.metadata.schema_version,
                "classification_status": self.metadata.classification_status,
                "context_type": self.metadata.context_type,
                "mention_count": self.metadata.mention_count,
                "first_seen": self.metadata.first_seen,
                "last_seen": self.metadata.last_seen,
                "auto_inferred": self.metadata.auto_inferred,
                "source_weights": self.metadata.source_weights,
                "injection_tier": self.metadata.injection_tier,
                "salience_score": self.metadata.salience_score,
                "last_consolidated": self.metadata.last_consolidated,
                "consolidation_notes": self.metadata.consolidation_notes,
                "archived": self.metadata.archived,
            }) if self.metadata else None,
            "full_content_ref": self.full_content_ref,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProjectEntry":
        """Create from dictionary (JSON deserialization)."""
        # Parse decisions
        decisions_made = []
        for d in data.get("decisions_made", []):
            ev = d.get("evidence")
            decisions_made.append(Decision(
                decision=d["decision"],
                rationale=d.get("rationale"),
                date=d.get("date", ""),
                evidence=Evidence(
                    conversation_id=ev.get("conversation_id", ""),
                    message_ids=ev.get("message_ids", []),
                    snippet=ev.get("snippet", ""),
                ) if ev else None
            ))
        
        # Parse repos
        related_repos = []
        for r in data.get("related_repos", []):
            related_repos.append(RepoLink(
                repo=r["repo"],
                path=r.get("path"),
                link_type=r.get("link_type", "explicit"),
                confidence=r.get("confidence", 1.0),
                evidence=r.get("evidence"),
            ))
        
        # Parse metadata
        meta_data = normalize_project_metadata_dict(data.get("metadata"))
        metadata = ProjectMetadata(
            created_at=meta_data.get("created_at", ""),
            updated_at=meta_data.get("updated_at", ""),
            source_conversations=meta_data.get("source_conversations", []),
            source_messages=meta_data.get("source_messages", []),
            last_touched=meta_data.get("last_touched", ""),
            access_count=meta_data.get("access_count", 0),
            last_accessed=meta_data.get("last_accessed"),
            schema_version=meta_data.get("schema_version", MEMORY_SCHEMA_VERSION),
            classification_status=meta_data.get("classification_status", "pending"),
            context_type=meta_data.get("context_type"),
            mention_count=meta_data.get("mention_count"),
            first_seen=meta_data.get("first_seen"),
            last_seen=meta_data.get("last_seen"),
            auto_inferred=meta_data.get("auto_inferred"),
            source_weights=meta_data.get("source_weights", {}),
            injection_tier=meta_data.get("injection_tier"),
            salience_score=meta_data.get("salience_score"),
            last_consolidated=meta_data.get("last_consolidated"),
            consolidation_notes=meta_data.get("consolidation_notes", []),
            archived=meta_data.get("archived", False),
        ) if data.get("metadata") else None
        
        return cls(
            id=data["id"],
            type=data.get("type", "project"),
            name=data.get("name", ""),
            status=data.get("status", "active"),
            detail_level=data.get("detail_level", "full"),
            goal=data.get("goal", ""),
            current_phase=data.get("current_phase", ""),
            blocked_on=data.get("blocked_on"),
            decisions_made=decisions_made,
            tech_stack=data.get("tech_stack", []),
            related_repos=related_repos,
            related_knowledge=data.get("related_knowledge", []),
            phase_history=data.get("phase_history", []),
            metadata=metadata,
            full_content_ref=data.get("full_content_ref"),
        )
