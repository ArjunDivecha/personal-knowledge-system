REPO_ROOT := $(shell pwd)
PYTHON := $(REPO_ROOT)/distillation/venv/bin/python
WORKER_DIR := $(REPO_ROOT)/cloudflare-mcp/mcp-server
FIXTURE_BUNDLE := $(REPO_ROOT)/tests/fixtures/sample_memory_fixture.json
PHASE9_PROBES := $(REPO_ROOT)/tests/fixtures/phase9_staging_outcome_probes.json
RETRIEVAL_BASELINE := $(REPO_ROOT)/tests/baselines/retrieval_baseline.json
STAGING_WORKER_BASE_URL ?= $${STAGING_WORKER_BASE_URL}
CHECK_OVERNIGHT_DREAM_ARGS ?=
ENSURE_OVERNIGHT_DREAM_ARGS ?=

.PHONY: worker-typecheck worker-test verify-memory-full dream-live-canary ensure-overnight-dream check-overnight-dream sleep-report check-overnight-streak test-python-checker retrieval-regression-gate seed-staging-dry-run staging-smoke-dry-run staging-smoke deploy-staging worker-secrets-staging audit-memory-quality verify-memory-quality

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

ensure-overnight-dream:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/ensure_overnight_dream_run.py" $(ENSURE_OVERNIGHT_DREAM_ARGS)

check-overnight-dream:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/check_overnight_dream_run.py" $(CHECK_OVERNIGHT_DREAM_ARGS)

sleep-report: ensure-overnight-dream
	"$(PYTHON)" "$(REPO_ROOT)/scripts/check_overnight_dream_run.py" --allow-on-demand $(CHECK_OVERNIGHT_DREAM_ARGS)

check-overnight-streak:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/check_validation_streak.py" --gate check_overnight_dream --required-days 7

test-python-checker:
	"$(PYTHON)" -m unittest discover -s "$(REPO_ROOT)/tests/python" -p 'test_*.py'

retrieval-regression-gate:
	@test -n "$(FRESH_REPORT)" || (echo "Usage: make retrieval-regression-gate FRESH_REPORT=/path/to/fresh.json"; exit 2)
	@test -f "$(RETRIEVAL_BASELINE)" || (echo "Missing committed retrieval baseline: $(RETRIEVAL_BASELINE)"; exit 2)
	"$(PYTHON)" "$(REPO_ROOT)/scripts/run_eval.py" --compare "$(RETRIEVAL_BASELINE)" "$(FRESH_REPORT)" --fail-on-regression

seed-staging-dry-run:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/seed_staging_env.py" --bundle "$(FIXTURE_BUNDLE)" --phase9-probes "$(PHASE9_PROBES)" --dry-run

staging-smoke-dry-run:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/run_e2e_staging.py" --base-url "$(STAGING_WORKER_BASE_URL)" --bundle "$(FIXTURE_BUNDLE)" --phase9-probes "$(PHASE9_PROBES)" --dry-run

staging-smoke:
	"$(PYTHON)" "$(REPO_ROOT)/scripts/run_e2e_staging.py" --base-url "$(STAGING_WORKER_BASE_URL)" --bundle "$(FIXTURE_BUNDLE)" --phase9-probes "$(PHASE9_PROBES)"

deploy-staging:
	cd "$(WORKER_DIR)" && npm run deploy:staging

worker-secrets-staging:
	cd "$(WORKER_DIR)" && npm run secrets:staging
