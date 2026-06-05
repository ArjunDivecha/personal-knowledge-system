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
from .phase7 import (
    AUTHORITY_RANK,
    PHASE7_SCHEMA_VERSION,
    Phase7CompiledClaim,
    Phase7MigrationPreview,
    Phase7Observation,
    Phase7SupersessionEdge,
    compiled_claims_from_observations,
    highest_source_authority,
    normalize_claim_text,
    observations_from_legacy_entry,
    preview_phase7_migration,
    provisional_claim_from_observation,
    retrieval_projection_from_claims,
    stable_phase7_id,
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
    # Phase 7A schema
    "AUTHORITY_RANK",
    "PHASE7_SCHEMA_VERSION",
    "Phase7CompiledClaim",
    "Phase7MigrationPreview",
    "Phase7Observation",
    "Phase7SupersessionEdge",
    "compiled_claims_from_observations",
    "highest_source_authority",
    "normalize_claim_text",
    "observations_from_legacy_entry",
    "preview_phase7_migration",
    "provisional_claim_from_observation",
    "retrieval_projection_from_claims",
    "stable_phase7_id",
]
