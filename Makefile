PYTHON := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/distillation/venv/bin/python
WORKER_DIR := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server
FIXTURE_BUNDLE := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/fixtures/sample_memory_fixture.json

.PHONY: worker-typecheck verify-memory-full dream-live-canary seed-staging-dry-run staging-smoke-dry-run

worker-typecheck:
	cd "$(WORKER_DIR)" && npm run type-check

verify-memory-full:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/verify_memory_consistency.py --full --strict

dream-live-canary:
	cd "$(WORKER_DIR)" && npm run test:dream-live -- --count 3

seed-staging-dry-run:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/seed_staging_env.py --bundle "$(FIXTURE_BUNDLE)" --dry-run

staging-smoke-dry-run:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/run_e2e_staging.py --base-url "$$STAGING_WORKER_BASE_URL" --bundle "$(FIXTURE_BUNDLE)" --dry-run
