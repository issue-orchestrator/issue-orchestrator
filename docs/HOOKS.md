# Hooks & Guardrails

Hooks exist to enforce invariants, not to decide state.

Examples:
- Blocking `--no-verify`
- Enforcing validation before publish
- Preventing agent credential leakage

Hooks may observe or block actions, but never advance state.
