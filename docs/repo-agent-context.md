# Repo Agent Context

This workflow attaches AI coding context to the repository itself so the nightly
GitHub ingestion job can treat it as repo evidence rather than as a separate
standalone chat log.

## What gets committed

The exporter writes redacted markdown artifacts under:

```text
.pks/agent-context/
```

Current surfaces:

- `claude_code`
- `codex_cli`
- `cursor`

Artifacts are session-scoped filenames such as:

```text
.pks/agent-context/claude-code-<session-id>.md
.pks/agent-context/codex-cli-<session-id>.md
.pks/agent-context/cursor-<session-id>.md
```

That avoids overwriting all history into a single file when multiple sessions
touch the same repo.

## Preferred install: global machine hook

From `knowledge-system/`, run:

```bash
./scripts/install_global_repo_agent_context_hook.sh
```

That installs:

```text
~/.githooks/pre-commit
~/.local/bin/pks-export-repo-context
~/.local/share/pks-repo-agent-context/export_repo_agent_context.py
```

and sets:

```bash
git config --global core.hooksPath ~/.githooks
```

After that, every Git commit on this machine will run the shared hook.

Behavior:

- skips cleanly for repos whose `origin` is not on GitHub
- skips cleanly for repos with `.pks-disable-hook`
- preserves an existing global pre-commit hook as `~/.githooks/pre-commit.previous`
- chains a repo-specific tracked hook at `<repo>/.githooks/pre-commit` if present

## Secondary install: one target repo

From `knowledge-system/`, run:

```bash
./scripts/install_repo_agent_context_hook.sh /path/to/target-repo
```

That installs `.git/hooks/pre-commit` only in that target repo. It is still
useful if you do not want the machine-wide hook.

## Nightly ingestion behavior

The GitHub ingestion pipeline now scans committed files under
`.pks/agent-context/`, distills durable repo-specific knowledge from those
artifacts, and stores the results with repo-scoped IDs so future runs update
the same topic instead of creating disconnected chat-shaped entries.

Processed artifact versions are deduplicated by Git blob SHA.

## Current limitations

- Windsurf and Antigravity adapters are not implemented yet.
- The per-repo installer depends on the `knowledge-system` checkout staying at
  the same absolute path. The global installer avoids that by copying the
  exporter into your home directory.
