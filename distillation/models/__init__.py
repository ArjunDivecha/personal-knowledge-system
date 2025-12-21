"""
Type definitions for the knowledge distillation pipeline.

Contains dataclasses for:
- KnowledgeEntry, ProjectEntry (output types)
- NormalizedConversation (intermediate format)
- ThinIndex (compressed index for retrieval)
"""

from .entries import (
    Evidence,
    Insight,
    Capability,
    OpenQuestion,
    RepoLink,
    Position,
    Evolution,
    KnowledgeMetadata,
    KnowledgeEntry,
    Decision,
    ProjectMetadata,
    ProjectEntry,
)
from .normalized import (
    CodeBlock,
    NormalizedMessage,
    ParseMetadata,
    NormalizedConversation,
)
from .thin_index import (
    ThinIndexTopic,
    ThinIndexProject,
    ThinIndexEvolution,
    ThinIndex,
)

__all__ = [
    # Entry types
    "Evidence",
    "Insight",
    "Capability",
    "OpenQuestion",
    "RepoLink",
    "Position",
    "Evolution",
    "KnowledgeMetadata",
    "KnowledgeEntry",
    "Decision",
    "ProjectMetadata",
    "ProjectEntry",
    # Normalized conversation types
    "CodeBlock",
    "NormalizedMessage",
    "ParseMetadata",
    "NormalizedConversation",
    # Index types
    "ThinIndexTopic",
    "ThinIndexProject",
    "ThinIndexEvolution",
    "ThinIndex",
]

