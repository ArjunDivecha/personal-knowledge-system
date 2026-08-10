# Files

- [Ingestion and distillation workflow](ingestion-and-distillation.md) - How the system ingests GitHub, Gmail, Twitter/X, and agent-session sources, normalizes them, and distills structured memory entries.
- [Nightly orchestration workflow](nightly-orchestration.md) - Nightly orchestrator state machine (staging/legacy after the source-first cutover): run identity, locking, ledgering, stage persistence, resume, report generation, and launchd supervision.
- [Source-first rebuild workflow](source-first-rebuild.md) - Two-hour staged build, retrieval gate, and atomic promotion for unified file and recent-session evidence.
