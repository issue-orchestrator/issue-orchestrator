.PHONY: help install typecheck lint-arch test test-unit test-unit-cov test-unit-cov-html test-integration test-e2e test-e2e-one test-web test-web-headed playwright-install validate validate-quick validate-full _validate-impl _validate-full-impl clean demo issues-validate issues-fix issues-fix-dry-run issues-create

# GNU make detection - required for parallel validation with grouped output
# On macOS: brew install make (provides gmake)
# On Linux: GNU make is the default
GMAKE := $(shell command -v gmake 2>/dev/null || command -v make)
GMAKE_VERSION := $(shell $(GMAKE) --version 2>/dev/null | head -1)

# Default target
help:
	@echo "Available targets:"
	@echo "  install             Install dev dependencies"
	@echo "  typecheck           Run pyright type checking"
	@echo "  lint-arch           Run import-linter + AST guardrails"
	@echo "  test-unit           Run unit tests"
	@echo "  test-unit-cov       Run unit tests with coverage report"
	@echo "  test-unit-cov-html  Run unit tests with HTML coverage (open htmlcov/index.html)"
	@echo "  test-integration    Run integration tests"
	@echo "  test-e2e            Run e2e tests (stops on first failure, use NOFAST=1 to run all)"
	@echo "  test-e2e-one        Run single e2e test (TEST=test_name)"
	@echo "  test-web            Run Playwright web UI tests (headless)"
	@echo "  test-web-headed     Run Playwright web UI tests (headed, for debugging)"
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

install:
	pip install -e ".[dev]"
	@echo ""
	@echo "NOTE: On macOS, install GNU make for parallel validation:"
	@echo "  brew install make"

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

lint-arch:
	$(LINT_IMPORTS)
	$(PYTHON) tools/check_arch_guardrails.py src

test-unit:
	$(PYTEST) tests/unit -x -q --tb=short

test-unit-cov:
	$(PYTEST) tests/unit --cov=src/issue_orchestrator --cov-report=term-missing -x -q --tb=short

test-unit-cov-html:
	$(PYTEST) tests/unit --cov=src/issue_orchestrator --cov-report=html -x -q --tb=short
	@echo "Coverage report: open htmlcov/index.html"

test-integration:
	$(PYTEST) tests/integration -x -q --tb=short

# E2E tests stop on first failure by default. Use NOFAST=1 to run all tests.
# Usage: make test-e2e        (stops on first failure)
#        make test-e2e NOFAST=1  (runs all tests even if some fail)
test-e2e:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short
else
	$(PYTEST) tests/e2e -v -s --tb=short -x
endif

# Run a single e2e test by name. Usage: make test-e2e-one TEST=test_code_review_produces_review_comment
# E2E tests stop on first failure by default
test-e2e-one:
ifdef NOFAST
	$(PYTEST) tests/e2e -v -s --tb=short -k "$(TEST)"
else
	$(PYTEST) tests/e2e -v -s --tb=short -x -k "$(TEST)"
endif

test:
	$(PYTEST) tests/ -x -q --tb=short

# Playwright browser tests for web UI
test-web:
	$(PYTEST) tests/e2e_web -v --tb=short

test-web-headed:
	$(PYTEST) tests/e2e_web -v --tb=short --headed

playwright-install:
	playwright install chromium

# Quick validation for agent_gate (~45s)
validate-quick: typecheck test-unit

# Standard validation - runs 5 jobs in parallel (~40s vs ~90s sequential)
# Used by CI and pre-push hook
validate:
	@$(GMAKE) -j5 --output-sync=target _validate-impl

# Internal target for parallel execution
_validate-impl: typecheck lint-arch test-unit test-integration test-web
	@echo "✓ All validations passed!"

# Full validation including e2e tests - runs 6 jobs in parallel
validate-full:
	@$(GMAKE) -j6 --output-sync=target _validate-full-impl

# Internal target for parallel execution
_validate-full-impl: typecheck lint-arch test-unit test-integration test-web test-e2e
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
