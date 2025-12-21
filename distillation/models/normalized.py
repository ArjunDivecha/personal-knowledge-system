"""
=============================================================================
NORMALIZED CONVERSATION TYPE DEFINITIONS
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Defines the intermediate format for parsed conversations.
Both Claude and GPT exports are converted to this common format
before processing through the pipeline.

INPUT FILES:
- None (type definitions only)

OUTPUT FILES:
- None (type definitions only)

USAGE:
    from distillation.types import NormalizedConversation, NormalizedMessage
=============================================================================
"""

from dataclasses import dataclass, field
from typing import Literal, Optional


# -----------------------------------------------------------------------------
# CODE BLOCK - Extracted code from messages
# -----------------------------------------------------------------------------
@dataclass
class CodeBlock:
    """A code block extracted from a message."""
    language: Optional[str] = None  # Language hint (e.g., "python", "javascript")
    content: str = ""               # The code content


# -----------------------------------------------------------------------------
# NORMALIZED MESSAGE
# -----------------------------------------------------------------------------
@dataclass
class NormalizedMessage:
    """
    A single message in normalized format.
    Preserves message_id for provenance tracking.
    """
    message_id: str               # Original UUID - MUST be preserved
    role: Literal["user", "assistant"]
    created_at: str               # ISO8601 timestamp
    content: str                  # Full message text
    content_type: Literal["text", "code", "mixed"] = "text"
    code_blocks: list[CodeBlock] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "message_id": self.message_id,
            "role": self.role,
            "created_at": self.created_at,
            "content": self.content,
            "content_type": self.content_type,
            "code_blocks": [
                {"language": cb.language, "content": cb.content}
                for cb in self.code_blocks
            ],
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "NormalizedMessage":
        """Create from dictionary."""
        code_blocks = [
            CodeBlock(language=cb.get("language"), content=cb.get("content", ""))
            for cb in data.get("code_blocks", [])
        ]
        return cls(
            message_id=data["message_id"],
            role=data["role"],
            created_at=data["created_at"],
            content=data["content"],
            content_type=data.get("content_type", "text"),
            code_blocks=code_blocks,
        )


# -----------------------------------------------------------------------------
# PARSE METADATA - Tracking decisions made during parsing
# -----------------------------------------------------------------------------
@dataclass
class ParseMetadata:
    """
    Metadata about the parsing process.
    Used for debugging and audit trails.
    """
    total_nodes: int = 0          # Total messages in original tree
    branches_found: int = 0       # Number of branch points
    selected_path: list[str] = field(default_factory=list)  # Message IDs in order
    alternate_branches_kept: int = 0  # How many non-primary branches kept
    parser_version: str = "1.0.0"
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "total_nodes": self.total_nodes,
            "branches_found": self.branches_found,
            "selected_path": self.selected_path,
            "alternate_branches_kept": self.alternate_branches_kept,
            "parser_version": self.parser_version,
        }


# -----------------------------------------------------------------------------
# NORMALIZED CONVERSATION
# -----------------------------------------------------------------------------
@dataclass
class NormalizedConversation:
    """
    A conversation in normalized format, ready for processing.
    
    Both Claude and GPT exports are converted to this format.
    The messages are linearized (branches resolved to single path).
    """
    id: str                       # Original conversation UUID
    source: Literal["claude", "gpt"]
    title: str
    created_at: str               # ISO8601
    updated_at: str               # ISO8601
    messages: list[NormalizedMessage] = field(default_factory=list)
    parse_metadata: Optional[ParseMetadata] = None
    
    @property
    def message_count(self) -> int:
        """Number of messages in the conversation."""
        return len(self.messages)
    
    @property
    def user_message_count(self) -> int:
        """Number of user messages."""
        return sum(1 for m in self.messages if m.role == "user")
    
    @property
    def assistant_message_count(self) -> int:
        """Number of assistant messages."""
        return sum(1 for m in self.messages if m.role == "assistant")
    
    @property
    def has_code(self) -> bool:
        """Whether any message contains code blocks."""
        return any(m.code_blocks for m in self.messages)
    
    @property
    def total_content_length(self) -> int:
        """Total character count of all message content."""
        return sum(len(m.content) for m in self.messages)
    
    def get_message_by_id(self, message_id: str) -> Optional[NormalizedMessage]:
        """Find a message by its ID."""
        for msg in self.messages:
            if msg.message_id == message_id:
                return msg
        return None
    
    def validate_message_ids(self, message_ids: list[str]) -> list[str]:
        """
        Check which message IDs exist in this conversation.
        Returns list of invalid (not found) IDs.
        """
        valid_ids = {m.message_id for m in self.messages}
        return [mid for mid in message_ids if mid not in valid_ids]
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [m.to_dict() for m in self.messages],
            "parse_metadata": self.parse_metadata.to_dict() if self.parse_metadata else None,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "NormalizedConversation":
        """Create from dictionary (JSON deserialization)."""
        messages = [NormalizedMessage.from_dict(m) for m in data.get("messages", [])]
        
        pm_data = data.get("parse_metadata")
        parse_metadata = ParseMetadata(
            total_nodes=pm_data.get("total_nodes", 0),
            branches_found=pm_data.get("branches_found", 0),
            selected_path=pm_data.get("selected_path", []),
            alternate_branches_kept=pm_data.get("alternate_branches_kept", 0),
            parser_version=pm_data.get("parser_version", "1.0.0"),
        ) if pm_data else None
        
        return cls(
            id=data["id"],
            source=data["source"],
            title=data["title"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            messages=messages,
            parse_metadata=parse_metadata,
        )
    
    def __repr__(self) -> str:
        return f"NormalizedConversation(id={self.id!r}, source={self.source!r}, title={self.title!r}, messages={self.message_count})"

