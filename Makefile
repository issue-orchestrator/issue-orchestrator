.PHONY: help venv worktree-setup install upgrade-deps typecheck lint-arch lint-complexity sync-deps test test-unit test-unit-cov test-unit-cov-html test-integration test-e2e test-e2e-one test-e2e-live test-real-claude-dev test-real-claude-review test-real-gh-labels test-real-gh test-real-gh-plus-e2e test-real-gh-plus-e2e-subprocess test-web test-web-headed playwright-install validate validate-quick validate-full _validate-impl _validate-full-impl clean demo issues-validate issues-fix issues-fix-dry-run issues-create

# GNU make detection - required for parallel validation with grouped output
# On macOS: brew install make (provides gmake)
# On Linux: GNU make is the default
GMAKE := $(shell command -v gmake 2>/dev/null || command -v make)
GMAKE_VERSION := $(shell $(GMAKE) --version 2>/dev/null | head -1)

# Default target
help:
	@echo "Available targets:"
	@echo "  venv                Create/recreate .venv with Python 3.14+ and install all deps"
	@echo "  worktree-setup      Full worktree setup: venv + vscode extensions + playwright"
	@echo "  install             Install dev dependencies (assumes venv exists)"
	@echo "  upgrade-deps        Update uv.lock after changing pyproject.toml"
	@echo "  typecheck           Run pyright type checking"
	@echo "  lint-arch           Run import-linter + AST guardrails"
	@echo "  lint-complexity     Check cyclomatic complexity (C901) and branch count (PLR0912)"
	@echo "  test-unit           Run unit tests"
	@echo "  test-unit-cov       Run unit tests with coverage report"
	@echo "  test-unit-cov-html  Run unit tests with HTML coverage (open htmlcov/index.html)"
	@echo "  test-integration    Run integration tests"
	@echo "  test-e2e            Run e2e tests (stops on first failure, use NOFAST=1 to run all)"
	@echo "  test-e2e-one        Run single e2e test (TEST=test_name)"
	@echo "  test-e2e-live       Run e2e tests with REAL PR creation (no dry run!)"
	@echo "  test-real-claude-dev    Test dev agent: Claude execution -> PR created"
	@echo "  test-real-claude-review Test full pipeline: dev agent -> review agent -> approved"
	@echo "  test-real-gh-labels     Verify label write paths against real GitHub"
	@echo "  test-real-gh            Run full real-GitHub suite (dev + review + labels)"
	@echo "  test-real-gh-plus-e2e   Run real-GitHub suite plus full e2e tests"
	@echo "  test-real-gh-plus-e2e-subprocess   Same as above but using subprocess backend"
	@echo "  test-web            Run Playwright web UI tests (headless)"
	@echo "  test-web-headed     Run Playwright web UI tests (headed, for debugging)"
	@echo "  test-vscode         Run VS Code extension tests (local only, skipped in CI)"
	@echo "  install-vscode-extensions      Install VS Code extension dev dependencies"
	@echo "  playwright-install  Install Playwright browser binaries"
	@echo "  test                Run all tests"
	@echo "  validate            Parallel validation (~40s): typecheck + lint-arch + unit + integration + web-ui"
	@echo "  validate-quick      Quick validation (typecheck + unit tests only)"
	@echo "  validate-full       Full parallel validation: validate + e2e tests"
	@echo "  demo                Run demo showing orchestrator features"
	@echo "  issues-validate     Check issue naming conventions"
	@echo "  issues-fix          Apply issue name fixes"
	@echo "  issues-fix-dry-run  Preview issue name fixes (no changes)"
	@echo "  issues-create       Create issue (use ARGS='--agent x --milestone n --title y')"
	@echo "  clean               Remove build artifacts"
	@echo ""
	@echo "Using: $(GMAKE_VERSION)"

# System Python for venv creation - prefer 3.14, fall back to 3.13, 3.12, 3.11
SYSTEM_PYTHON := $(shell command -v python3.14 2>/dev/null || command -v python3.13 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.11 2>/dev/null || echo python3)

# Timing log for worktree setup analysis (central location for cumulative stats)
SETUP_LOG ?= $(HOME)/.issue-orchestrator/worktree-setup.log

# Shared playwright browser cache - avoids 250MB re-downloads across worktrees
export PLAYWRIGHT_BROWSERS_PATH ?= $(HOME)/.cache/ms-playwright

# uv command - prefer PATH, fall back to default install location
UV := $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)

# Auto-install uv if not present (one-time per machine)
ensure-uv:
	@if [ ! -x "$(UV)" ]; then \
		echo "Installing uv for fast package management..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi

venv: ensure-uv
	@mkdir -p $$(dirname $(SETUP_LOG))
	@if [ -d .venv ]; then \
		echo "Removing existing .venv..."; \
		rm -rf .venv; \
	fi
	@echo "Creating venv with $(SYSTEM_PYTHON) and installing dependencies..."
	@t0=$$(date +%s); \
	$(UV) venv .venv --python $(SYSTEM_PYTHON); \
	t1=$$(date +%s); \
	$(UV) sync --frozen --all-extras; \
	t2=$$(date +%s); \
	touch .venv/.deps-synced; \
	echo "venv pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) uv_venv=$$((t1-t0))s uv_sync=$$((t2-t1))s total=$$((t2-t0))s" >> $(SETUP_LOG)
	@echo ""
	@echo "Done! Activate with: source .venv/bin/activate"

# Legacy pip-based venv for systems without uv
venv-pip:
	@mkdir -p $$(dirname $(SETUP_LOG))
	@if [ -d .venv ]; then \
		echo "Removing existing .venv..."; \
		rm -rf .venv; \
	fi
	@echo "Creating venv with $(SYSTEM_PYTHON) (pip fallback)..."
	@t0=$$(date +%s); \
	$(SYSTEM_PYTHON) -m venv .venv; \
	t1=$$(date +%s); \
	echo "Installing agent-runner package first..."; \
	.venv/bin/pip install -e "packages/agent_runner"; \
	t2=$$(date +%s); \
	echo "Installing main package with dev dependencies..."; \
	.venv/bin/pip install -e ".[dev]"; \
	t3=$$(date +%s); \
	touch .venv/.deps-synced; \
	echo "venv-pip pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) venv_create=$$((t1-t0))s pip_agent_runner=$$((t2-t1))s pip_dev_deps=$$((t3-t2))s total=$$((t3-t0))s" >> $(SETUP_LOG)
	@echo ""
	@echo "Done! Activate with: source .venv/bin/activate"

# Full worktree setup - use this when setting up a new git worktree
worktree-setup: venv
	@echo ""
	@t0=$$(date +%s); \
	echo "Installing VS Code extension dependencies..."; \
	(cd packages/vscode && npm install --silent); \
	t1=$$(date +%s); \
	echo "Installing Playwright browsers..."; \
	.venv/bin/playwright install chromium --with-deps 2>/dev/null || .venv/bin/playwright install chromium; \
	t2=$$(date +%s); \
	echo "worktree-setup pid=$$$$ ts=$$(date -Iseconds) pwd=$$(pwd) npm_vscode=$$((t1-t0))s playwright=$$((t2-t1))s total=$$((t2-t0))s" >> $(SETUP_LOG)
	@echo ""
	@echo "Worktree setup complete! Activate with: source .venv/bin/activate"

install: ensure-uv
	$(UV) sync --frozen --all-extras
	@touch .venv/.deps-synced

# Update dependencies after changing pyproject.toml
# Usage: make upgrade-deps           - re-resolve after pyproject.toml changes
#        make upgrade-deps UPGRADE=1 - upgrade all deps to latest versions
upgrade-deps: ensure-uv
ifdef UPGRADE
	@echo "Upgrading all dependencies to latest versions..."
	$(UV) lock --upgrade
else
	@echo "Updating uv.lock..."
	$(UV) lock
endif
	@echo "Syncing dependencies..."
	$(UV) sync --frozen --all-extras
	@touch .venv/.deps-synced
	@echo ""
	@echo "Done! Commit uv.lock with your changes."

PYRIGHT ?= .venv/bin/pyright --pythonpath .venv/bin/python
PYTEST ?= .venv/bin/pytest

# Two-pass typecheck: strict for core (domain/ports/control), standard for rest
# --warnings ensures 0 warnings required (exit code 1 if warnings reported)
typecheck:
	@echo "Running pyright (standard mode, excluding core)..."
	$(PYRIGHT) --project pyrightconfig.json --warnings
	@echo "Running pyright (strict mode, core only)..."
	$(PYRIGHT) --project pyrightconfig.strict.json --warnings

LINT_IMPORTS ?= .venv/bin/lint-imports
RUFF ?= .venv/bin/ruff

lint-arch:
	$(LINT_IMPORTS)
	$(PYTHON) tools/check_arch_guardrails.py src
	scripts/check_agents_md.sh

# Ruff guardrails - blocks on violations (C901 complexity, PLR0912 branches, SLF001 private access)
lint-complexity:
	@echo "Checking code complexity (C901) and branch count (PLR0912)..."
	$(RUFF) check src packages/agent_runner/src --output-format=concise

# Parallel test execution with pytest-xdist (-n auto uses all CPU cores)
# Use PARALLEL=0 to disable: make test-unit PARALLEL=0
PARALLEL ?= auto

# Python interpreter for dependency checks
PYTHON ?= .venv/bin/python

# Marker file for tracking when deps were last synced
DEPS_MARKER ?= .venv/.deps-synced

# Auto-sync dependencies if pyproject.toml or uv.lock is newer than last sync
# This prevents cryptic errors like "unrecognized arguments: -n" when pytest-xdist is missing
sync-deps:
	@if [ ! -f $(DEPS_MARKER) ] || [ pyproject.toml -nt $(DEPS_MARKER) ] || [ uv.lock -nt $(DEPS_MARKER) ]; then \
		echo ""; \
		echo "================================================================"; \
		echo "[sync-deps] Dependencies changed since last install"; \
		echo "[sync-deps] Auto-syncing dependencies on your behalf..."; \
		echo "================================================================"; \
		$(UV) sync --frozen --all-extras && touch $(DEPS_MARKER) && \
		echo "[sync-deps] Done. Continuing with original command..."; \
		echo ""; \
	fi

test-unit: sync-deps
ifeq ($(PARALLEL),0)
	$(PYTEST) tests/unit packages/agent_runner/tests -x -q --tb=short
else
	$(PYTEST) tests/unit packages/agent_runner/tests -x -q --tb=short -n $(PARALLEL)
endif

test-unit-cov:
	$(PYTEST) tests/unit packages/agent_runner/tests --cov=src/issue_orchestrator --cov=packages/agent_runner/src --cov-report=term-missing -x -q --tb=short

test-unit-cov-html:
	$(PYTEST) tests/unit packages/agent_runner/tests --cov=src/issue_orchestrator --cov=packages/agent_runner/src --cov-report=html -x -q --tb=short
	@echo "Coverage report: open htmlcov/index.html"

test-integration: sync-deps
ifeq ($(PARALLEL),0)
	$(PYTEST) tests/integration -x -q --tb=short
else
	$(PYTEST) tests/integration -x -q --tb=short -n $(PARALLEL)
endif

# Integration tests excluding those that require external infrastructure (GitHub token, etc.)
# Used in pre-push validation where full infra may not be available
test-integration-no-infra: sync-deps
ifeq ($(PARALLEL),0)
	$(PYTEST) tests/integration -x -q --tb=short -m "not requires_infra"
else
	$(PYTEST) tests/integration -x -q --tb=short -m "not requires_infra" -n $(PARALLEL)
endif

# Full integration tests including infrastructure-dependent ones (run in CI)
test-integration-full: sync-deps
ifeq ($(PARALLEL),0)
	$(PYTEST) tests/integration -x -q --tb=short
else
	$(PYTEST) tests/integration -x -q --tb=short -n $(PARALLEL)
endif

# E2E tests stop on first failure by default. Use NOFAST=1 to run all tests.
# Usage: make test-e2e        (stops on first failure)
#        make test-e2e NOFAST=1  (runs all tests even if some fail)
test-e2e:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short
else
	$(PYTEST) tests/e2e -v -s --tb=short -x
endif

# Real Claude tests - layered for incremental debugging
# test-real-claude-dev: dev agent only (faster, good for basic sanity)
# test-real-claude-review: dev + review agent (full happy path)

test-real-claude-dev:
	@echo "Testing agent-done invocation from Claude..."
	$(PYTEST) tests/integration/test_claude_execution.py::TestAgentDoneInvocation -v -s --tb=short -x
	@echo "Testing real Claude execution in tmux mode..."
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_terminal_adapter.py::TestTerminalAdapterExecution -v -s --tb=short -x
	@echo "✓ Dev agent tests passed!"

test-real-claude-review:
	@echo "Testing full pipeline: dev agent -> review agent..."
	@echo "Note: This test creates REAL PRs (not dry-run)"
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_review_agent.py::TestReviewAgentExecution -v -s --tb=short -x
	@echo "✓ Review agent tests passed!"

test-real-gh-labels:
	@echo "Testing label write verification against real GitHub..."
	E2E_DRY_RUN_PUSH=false $(PYTEST) tests/e2e/test_label_write_verification.py::TestLabelWriteVerification -v -s --tb=short -x
	@echo "✓ Label write verification passed!"

test-real-gh: test-real-claude-dev test-real-claude-review test-real-gh-labels
	@echo "✓ Real GitHub suite passed!"

test-real-gh-plus-e2e: test-real-gh test-e2e
	@echo "✓ Real GitHub + e2e suite passed!"

test-real-gh-plus-e2e-subprocess:
	@echo "✓ Running real GitHub + e2e suite with subprocess backend"
	E2E_TERMINAL_ADAPTER=subprocess $(GMAKE) test-real-gh
	E2E_TERMINAL_ADAPTER=subprocess $(GMAKE) test-e2e
	@echo "✓ Real GitHub + e2e subprocess suite passed!"

# Run a single e2e test by name. Usage: make test-e2e-one TEST=test_code_review_produces_review_comment
# E2E tests stop on first failure by default
test-e2e-one:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short -k "$(TEST)"
else
	$(PYTEST) tests/e2e -v -s --tb=short -x -k "$(TEST)"
endif

# Run e2e tests with REAL PR creation on GitHub (no dry run)
# WARNING: This creates actual PRs and branches on the target repo!
# Use TEST= to run a specific test, e.g.: make test-e2e-live TEST=test_code_review
test-e2e-live:
	@echo "⚠️  Running e2e tests with REAL PR creation (no dry run)!"
	@echo "   This will create actual PRs and branches on GitHub."
	@echo ""
ifdef TEST
	E2E_DRY_RUN_PUSH= $(PYTEST) tests/e2e -v -s --tb=short -x -k "$(TEST)"
else
	E2E_DRY_RUN_PUSH= $(PYTEST) tests/e2e -v -s --tb=short -x
endif

test:
	$(PYTEST) tests/ -x -q --tb=short

# Playwright browser tests for web UI
test-web:
	$(PYTEST) tests/e2e_web -v --tb=short

test-web-headed:
	$(PYTEST) tests/e2e_web -v --tb=short --headed

# VS Code extension tests (local only). Skipped in GitHub Actions.
test-vscode:
ifneq ($(GITHUB_ACTIONS),)
	@echo "Skipping test-vscode in GitHub Actions"
else
	@if [ ! -d "packages/vscode/node_modules" ]; then \
		echo "Missing packages/vscode/node_modules. Run: make install-vscode-extensions"; \
		exit 1; \
	fi
	cd packages/vscode && npm test
endif

install-vscode-extensions:
	cd packages/vscode && npm install

playwright-install:
	playwright install chromium

# Quick validation for agent_gate (~45s)
validate-quick: typecheck test-unit

# Standard validation - runs through Python wrapper for output capture
# Output is saved to ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR or .issue-orchestrator/diagnostics/
# On failure, prints path to output file so agents can find failure details
validate:
	@$(PYTHON) -m issue_orchestrator.entrypoints.cli_tools.validate_runner --command "$(GMAKE) validate-raw"

# Raw validation - direct execution without output capture wrapper
# Use this as a fallback if the Python wrapper fails
VALIDATE_JOBS ?= $(shell sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 5)
validate-raw:
	@$(GMAKE) -j$(VALIDATE_JOBS) --output-sync=target _validate-impl

# Internal target for parallel execution
_validate-impl: typecheck lint-arch lint-complexity test-unit test-integration test-web test-vscode
	@echo "✓ All validations passed!"

# Full validation including e2e tests
validate-full:
	@$(GMAKE) -j$(VALIDATE_JOBS) --output-sync=target _validate-full-impl

# Internal target for parallel execution
_validate-full-impl: typecheck lint-arch lint-complexity test-unit test-integration test-web test-e2e
	@echo "✓ All validations passed (including e2e)!"

# Demo - show orchestrator features with mock data
demo:
	.venv/bin/issue-orchestrator demo

# Issue management
PYTHON ?= .venv/bin/python

issues-validate:
	$(PYTHON) scripts/issues.py validate $(ARGS)

issues-fix:
	$(PYTHON) scripts/issues.py fix --apply $(ARGS)

issues-fix-dry-run:
	$(PYTHON) scripts/issues.py fix $(ARGS)

issues-create:
	$(PYTHON) scripts/issues.py create $(ARGS)
