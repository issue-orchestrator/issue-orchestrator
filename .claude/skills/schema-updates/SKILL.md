---
name: schema-updates
description: Update public UI contracts, settings schemas, and generated JSON schema artifacts. Use when changing view-model payloads, SSE event payload shapes, or configuration schema fields.
---

# Schema Updates

## Overview

Use this skill when you change any schema-backed payloads (UI view models, SSE payloads, or settings config). It explains where the canonical schema lives, how to regenerate JSON artifacts, and which tests enforce drift.

## Workflow

### 1. Public UI Contracts (View Models + SSE payloads)

**When to use:** Any change to dashboard view-model shape or SSE payload fields.

**Source of truth:**
- `src/issue_orchestrator/contracts/public.py`

**Generated artifacts:**
- `contracts/public/*.json` (one JSON schema per contract)

**Update steps:**
1. Edit the Pydantic contract(s) in `src/issue_orchestrator/contracts/public.py`.
2. Regenerate JSON schemas:
   ```bash
   python scripts/generate_public_contracts.py
   ```
3. Ensure `contracts/public/*.json` are updated.

**Drift test:**
- `tests/unit/test_public_contract_schemas.py`

Review artifact events expose artifact metadata, not blob content. Keep timeline event fields such as `review_decision_verdict`, `review_nit_policy`, and run-scoped review artifact descriptors in public contracts when dashboard or E2E projections consume them.

### 2. Settings Schema (Config + UI Settings Dialog)

**When to use:** Adding/changing config fields or settings dialog inputs.

**Source of truth:**
- `src/issue_orchestrator/infra/settings_schema.py`

**Generated artifacts:**
- `docs/user/configuration_reference.md` (auto-generated section)

**Update steps:**
1. Update `src/issue_orchestrator/infra/settings_schema.py` (and `infra/config.py` if needed).
2. Regenerate the config reference markdown:
   ```bash
   python -c "from issue_orchestrator.infra.settings_schema import generate_config_reference; print(generate_config_reference())" > /tmp/config_reference.md
   ```
3. Replace only the content between the `AUTO-GENERATED CONFIG REFERENCE` markers in `docs/user/configuration_reference.md`.

**Drift test:**
- `tests/unit/test_settings_schema.py::TestDriftDetection::test_config_reference_not_stale`

### 3. Completion Record Schema (coding-done / reviewer-done)

**When to use:** Changing the Completion Record JSON shape or validation rules.

**Source of truth:**
- `src/issue_orchestrator/domain/models.py` (`CompletionRecord`)
- `docs/architecture/ADR/0019-agent-done-completion-protocol.md`

**Update steps:**
1. Update `CompletionRecord` fields and validation logic in `domain/models.py`.
2. Update the ADR to match the new JSON shape.
3. Run the relevant completion tests.

## Guardrails

- Always update the **source of truth** first, then regenerate derived artifacts.
- Do not hand-edit files under `contracts/public/`.
- Keep schema changes in sync with any UI or test expectations.
