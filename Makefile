.PHONY: help install typecheck lint-arch test test-unit test-unit-cov test-unit-cov-html test-integration test-e2e test-e2e-one test-web test-web-headed playwright-install validate validate-quick validate-full clean demo issues-validate issues-fix issues-fix-dry-run issues-create

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
	@echo "  validate            Standard validation (typecheck + lint-arch + unit + integration + web-ui)"
	@echo "  validate-quick      Quick validation (typecheck + unit tests only)"
	@echo "  validate-full       Full validation (parallel: pyright + lint-arch + unit + integration + e2e + web-ui)"
	@echo "  demo                Run demo showing orchestrator features"
	@echo "  issues-validate     Check issue naming conventions"
	@echo "  issues-fix          Apply issue name fixes"
	@echo "  issues-fix-dry-run  Preview issue name fixes (no changes)"
	@echo "  issues-create       Create issue (use ARGS='--agent x --milestone n --title y')"
	@echo "  clean               Remove build artifacts"

install:
	pip install -e ".[dev]"

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

# Standard validation - used by CI and pre-push hook
validate: typecheck lint-arch
	$(PYTEST) tests/unit tests/integration tests/e2e_web -x -q --tb=short

# Full validation - runs pyright, lint-arch, unit, integration, and e2e all in parallel
# Logs to .validate/*.log, fails if any component fails
validate-full:
	@mkdir -p .validate
	@echo "Starting parallel validation (pyright + lint-arch + unit + integration + e2e + web-ui)..."
	@( \
		$(PYRIGHT) src/ > .validate/pyright.log 2>&1 && echo "✓ pyright passed" || (echo "✗ pyright FAILED (see .validate/pyright.log)" && exit 1) \
	) & pid1=$$!; \
	( \
		$(LINT_IMPORTS) > .validate/lint-arch.log 2>&1 && $(PYTHON) tools/check_arch_guardrails.py src >> .validate/lint-arch.log 2>&1 && echo "✓ lint-arch passed" || (echo "✗ lint-arch FAILED (see .validate/lint-arch.log)" && exit 1) \
	) & pid2=$$!; \
	( \
		$(PYTEST) tests/unit -x -q --tb=short > .validate/unit.log 2>&1 && echo "✓ unit tests passed" || (echo "✗ unit tests FAILED (see .validate/unit.log)" && exit 1) \
	) & pid3=$$!; \
	( \
		$(PYTEST) tests/integration -x -q --tb=short > .validate/integration.log 2>&1 && echo "✓ integration tests passed" || (echo "✗ integration tests FAILED (see .validate/integration.log)" && exit 1) \
	) & pid4=$$!; \
	( \
		$(PYTEST) tests/e2e -v -s --tb=short -x > .validate/e2e.log 2>&1 && echo "✓ e2e tests passed" || (echo "✗ e2e tests FAILED (see .validate/e2e.log)" && exit 1) \
	) & pid5=$$!; \
	( \
		$(PYTEST) tests/e2e_web -v --tb=short > .validate/web.log 2>&1 && echo "✓ web-ui tests passed" || (echo "✗ web-ui tests FAILED (see .validate/web.log)" && exit 1) \
	) & pid6=$$!; \
	wait $$pid1 && wait $$pid2 && wait $$pid3 && wait $$pid4 && wait $$pid5 && wait $$pid6 || (echo "Validation failed - check .validate/*.log" && exit 1)
	@echo "All validations passed!"

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
