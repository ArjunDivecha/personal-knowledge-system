REPO_ROOT := $(shell pwd)
PYTHON := $(REPO_ROOT)/distillation/venv/bin/python
WORKER_DIR := $(REPO_ROOT)/cloudflare-mcp/mcp-server
FIXTURE_BUNDLE := $(REPO_ROOT)/tests/fixtures/sample_memory_fixture.json
CHECK_OVERNIGHT_DREAM_ARGS ?=

.PHONY: worker-typecheck worker-test verify-memory-full dream-live-canary check-overnight-dream sleep-report check-overnight-streak test-python-checker seed-staging-dry-run staging-smoke-dry-run staging-smoke deploy-staging worker-secrets-staging audit-memory-quality verify-memory-quality

worker-typecheck:
	cd "$(WORKER_DIR)" && npm run type-check

worker-test:
	cd "$(WORKER_DIR)" && npm run test:worker

verify-memory-full:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/verify_memory_consistency.py" --full --strict

audit-memory-quality:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/audit_memory_quality.py" $(AUDIT_MEMORY_QUALITY_ARGS)

verify-memory-quality:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/audit_memory_quality.py" --write-gate $(AUDIT_MEMORY_QUALITY_ARGS)

dream-live-canary:
	cd "$(WORKER_DIR)" && npm run test:dream-live -- --count 3

check-overnight-dream:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/check_overnight_dream_run.py" $(CHECK_OVERNIGHT_DREAM_ARGS)

sleep-report: check-overnight-dream

check-overnight-streak:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/check_validation_streak.py" --gate check_overnight_dream --required-days 7

test-python-checker:
	"$(PYTHON)" -m unittest discover -s "$(REPO_ROOT)/tests/python" -p 'test_*.py'

seed-staging-dry-run:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/seed_staging_env.py" --bundle "$(FIXTURE_BUNDLE)" --dry-run

staging-smoke-dry-run:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/run_e2e_staging.py" --base-url "$$STAGING_WORKER_BASE_URL" --bundle "$(FIXTURE_BUNDLE)" --dry-run

staging-smoke:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/run_e2e_staging.py" --base-url "$$STAGING_WORKER_BASE_URL" --bundle "$(FIXTURE_BUNDLE)"

deploy-staging:
	cd "$(WORKER_DIR)" && npm run deploy:staging

worker-secrets-staging:
	cd "$(WORKER_DIR)" && npm run secrets:staging
