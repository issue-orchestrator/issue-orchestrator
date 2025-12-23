.PHONY: help install typecheck test test-unit test-integration test-e2e validate validate-before-push clean demo issues-validate issues-fix issues-fix-dry-run issues-create

# Default target
help:
	@echo "Available targets:"
	@echo "  install             Install dev dependencies"
	@echo "  typecheck           Run pyright type checking"
	@echo "  test-unit           Run unit tests"
	@echo "  test-integration    Run integration tests"
	@echo "  test-e2e            Run e2e tests"
	@echo "  test                Run all tests"
	@echo "  validate            Quick validation (typecheck + unit tests)"
	@echo "  validate-before-push Full validation (typecheck + all tests) - publish gate"
	@echo "  demo                Run demo showing orchestrator features"
	@echo "  issues-validate     Check issue naming conventions"
	@echo "  issues-fix          Apply issue name fixes"
	@echo "  issues-fix-dry-run  Preview issue name fixes (no changes)"
	@echo "  issues-create       Create issue (use ARGS='--agent x --milestone n --title y')"
	@echo "  clean               Remove build artifacts"

install:
	pip install -e ".[dev]"

typecheck:
	pyright src/

test-unit:
	pytest tests/unit -x -q --tb=short

test-integration:
	pytest tests/integration -x -q --tb=short

test-e2e:
	pytest tests/e2e -x -q --tb=short

test:
	pytest tests/ -x -q --tb=short

# Quick validation for agent_gate (~45s)
validate: typecheck test-unit

# Full validation for pre-push (~2-3 min) - THE publish gate
validate-before-push: typecheck
	pytest tests/unit tests/integration tests/e2e -x -q --tb=short

# Demo - show orchestrator features with mock data
demo:
	issue-orchestrator demo

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
