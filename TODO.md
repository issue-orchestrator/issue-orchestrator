# TODO - 4057 Production-Parity E2E Recovery

## Current Focus
- Get the focused live e2e flow for issue 4057 to complete end-to-end reliably:
  - coder agent completion
  - validation gate
  - reviewer cycle(s)
  - publish actions (push + PR finalization)
  - durable artifacts for post-mortem

## What Works
- Focused e2e target is isolated and running against real agents:
  - `tests/e2e/test_issue_4057_production_flow.py::test_4057_production_real_agents_publish_gate_and_diagnostics`
- Provider invocation uses `claude -p` with streamed JSON output.
- Run-level provider logs are mirrored for review-exchange sessions.
- Runtime dirty files are centrally whitelisted (shared policy module) to prevent false dirty-worktree blocking.
- GitHub label removal is idempotent for 404 in adapter path used during PR finalization.
- The coding timeout budget in the 4057 e2e config was increased to reduce premature timeout during long validation.

## What Does Not Work Reliably Yet
- Timeout race still exists in some runs:
  - agent starts `coding-done`
  - validation (`make validate-quick`) runs long
  - orchestrator marks session timed out before completion file appears
- Continuous monitoring discipline has been inconsistent; this caused avoidable idle gaps.
- Post-mortem preservation is still incomplete system-wide (some artifacts can be cleaned or become hard to correlate after timeout paths).

## Active Work In Progress
- Added controller-side no-completion diagnostic persistence to run artifacts when no completion record is found at decision time.
- Verifying diagnostics are always available for timeout/no-completion decisions and include path/existence metadata.
- Continuing focused 4057 live iterations until a full successful cycle is reached.

## How I Run Tests
- Focused live e2e (real agents):
  - `pytest -v tests/e2e/test_issue_4057_production_flow.py::test_4057_production_real_agents_publish_gate_and_diagnostics`
- Unit tests for touched logic:
  - `pytest -v tests/unit/test_session_controller.py`
  - `pytest -v tests/unit/test_provider_runner.py`
  - `pytest -v tests/unit/test_terminal_subprocess.py`
- Agent-side validation gate inside sessions:
  - `make validate-quick`

## What I Validate Each Run
- Session lifecycle correctness:
  - session launch, active state, completion handling, timeout handling
- `coding-done`/`reviewer-done` path behavior:
  - completion record creation timing
  - validation gate execution and artifact outputs
- Review/publish pipeline:
  - reviewer completion records
  - push + PR creation/finalization actions
  - label transitions and idempotent removal behavior
- Artifact durability:
  - run manifest, provider-runner logs, ui-session log linkage
  - completion and validation artifacts
  - no-completion diagnostics on timeout/no-record paths

## Next Steps
- Validate the new no-completion diagnostic behavior with targeted unit tests and focused e2e reruns.
- Execute focused 4057 runs repeatedly until successful publish path is stable.
- Confirm expected artifacts exist per stage (coding, validation, review, publish, diagnostics).
- If timeout race persists, implement a bounded grace/pending-validation handoff strategy in session outcome logic.
