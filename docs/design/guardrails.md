# Guardrails & Safety Model

Issue-Orchestrator is designed to assist humans, not replace trust boundaries. Agents are powerful but constrained by explicit guardrails at multiple layers.

## What the system guarantees

- **Agents cannot publish code directly.** All publishing is gated by a mandatory validation step (`make validate`) and enforced by the orchestrator and CI.
- **Humans always merge.** Branch protection is assumed; agents may create draft PRs but never merge.
- **Architecture boundaries are enforced.** Control, domain, and ports layers cannot perform side effects (subprocesses, HTTP calls). Violations fail fast.
- **Validation is the single source of truth.** The same validation gate runs locally, in CI, and in orchestrated workflows.

## How guardrails are enforced

Guardrails are layered so that no single bypass defeats the system:

1. **AI agent hooks** block unsafe tool calls before they execute (Claude Code `PreToolUse`, Cursor `beforeShellExecution`, etc.)
2. **Git hooks** run tests and linters before push is allowed. Bypassable with `--no-verify`, but covered by the next layer.
3. **Orchestrator policy** enforces validation regardless of local hooks. An agent session cannot advance without a passing validation record.
4. **CI** re-runs the canonical validation gate in a clean environment. This is the ultimate backstop for code quality.
5. **Static guardrails** (import-linter + custom AST checks) prevent architectural drift at every layer.

For a target repo, the intended install path is `issue-orchestrator setup-guardrails`: it installs the repo-local pre-push gate plus the configured AI-agent hook wiring in one step.

For the full hook architecture and inventory, see [Hook Enforcement Architecture](../architecture/hooks.md).
For the validation gate design, see [Validation System](../architecture/validation.md).

## What the system does not claim

- **Local process isolation**: Agent execution on macOS is not sandboxed. Agents can run arbitrary shell commands on the host. The guardrails prevent unvalidated code from being merged, not arbitrary local execution.
- **Absolute-path execution** (e.g. `/usr/bin/*`) cannot be fully prevented locally.
- For strong process isolation, container or CI-based execution is a future option.

This is an intentional trade-off. The system guarantees that agents cannot bypass validation or merge unreviewed code, but it does not claim to sandbox the agent's local environment.
