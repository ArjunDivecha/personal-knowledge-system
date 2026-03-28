PYTHON := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/distillation/venv/bin/python
WORKER_DIR := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/cloudflare-mcp/mcp-server
FIXTURE_BUNDLE := /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/fixtures/sample_memory_fixture.json

.PHONY: worker-typecheck worker-test verify-memory-full dream-live-canary check-overnight-dream test-python-checker seed-staging-dry-run staging-smoke-dry-run staging-smoke deploy-staging worker-secrets-staging

worker-typecheck:
	cd "$(WORKER_DIR)" && npm run type-check

worker-test:
	cd "$(WORKER_DIR)" && npm run test:worker

verify-memory-full:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/verify_memory_consistency.py --full --strict

dream-live-canary:
	cd "$(WORKER_DIR)" && npm run test:dream-live -- --count 3

check-overnight-dream:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/check_overnight_dream_run.py

test-python-checker:
	"$(PYTHON)" -m unittest discover -s /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/tests/python -p 'test_*.py'

seed-staging-dry-run:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/seed_staging_env.py --bundle "$(FIXTURE_BUNDLE)" --dry-run

staging-smoke-dry-run:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/run_e2e_staging.py --base-url "$$STAGING_WORKER_BASE_URL" --bundle "$(FIXTURE_BUNDLE)" --dry-run

staging-smoke:
	"$(PYTHON)" /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/scripts/run_e2e_staging.py --base-url "$$STAGING_WORKER_BASE_URL" --bundle "$(FIXTURE_BUNDLE)"

deploy-staging:
	cd "$(WORKER_DIR)" && npm run deploy:staging

worker-secrets-staging:
	cd "$(WORKER_DIR)" && npm run secrets:staging
