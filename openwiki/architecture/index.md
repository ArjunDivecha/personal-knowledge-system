# Files

- [Cloudflare MCP and Dream control plane](mcp-and-dream.md) - Cloudflare Worker MCP server. In production it serves the source-first read path; in staging it owns the legacy Dream lifecycle, salience scoring, judge queue, scheduled async Dream, semantic candidate authority, and lossless merge gates. Covers the SOURCE_FIRST_MODE cutover and which subsystems are retired from automatic operation.
- [Architecture overview](overview.md) - High-level architecture of the personal knowledge system: ingestion, distillation, Redis/Vector storage, Cloudflare MCP retrieval, and Dream governance.
