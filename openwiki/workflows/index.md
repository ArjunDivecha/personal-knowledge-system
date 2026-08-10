# Files

- [Ingestion and distillation workflow](ingestion-and-distillation.md) - How the system ingests GitHub, Gmail, Twitter/X, and agent-session sources, normalizes them, and distills structured memory entries.
- [Nightly orchestration workflow](nightly-orchestration.md) - Nightly orchestrator state machine (staging/legacy after the source-first cutover): run identity, locking, ledgering, stage persistence, resume, report generation, and launchd supervision.
- [Source-first rebuild workflow](source-first-rebuild.md) - Nightly build, verify, and atomic-promote lifecycle for source-first memory generations, driven by scripts/source_first_rebuild.py and the GitHub Actions Source-First Memory Rebuild workflow on a self-hosted macOS runner.
