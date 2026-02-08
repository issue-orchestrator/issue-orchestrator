.PHONY: validate-profile

# Repo-maintainer profiling target (not framework/user-facing).
# Usage:
#   make -f repo-specific/Makefile validate-profile
#   make -f repo-specific/Makefile validate-profile DRY_RUN=1
validate-profile:
	@args=""; \
	if [ -x ".venv/bin/python" ]; then py=".venv/bin/python"; \
	elif [ -x "../.venv/bin/python" ]; then py="../.venv/bin/python"; \
	else echo "Missing .venv/bin/python. Run: make venv-fast"; exit 1; fi; \
	if [ "$(DRY_RUN)" = "1" ]; then args="$$args --dry-run"; fi; \
	if [ -n "$(TARGETS)" ]; then args="$$args --targets $(TARGETS)"; fi; \
	if [ -n "$(OUTPUT)" ]; then args="$$args --output $(OUTPUT)"; fi; \
	VALIDATE_JOBS=$${VALIDATE_JOBS:-$$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 5)} \
	GMAKE="$${GMAKE:-$$(command -v gmake 2>/dev/null || command -v make)}" \
	$$py repo-specific/scripts/validate_profile.py $$args
