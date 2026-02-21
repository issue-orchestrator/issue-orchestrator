ma# TODO FOR NEXT AGENT

## Mission
Stop relying on shortcut tests. Build and validate a production-realistic, orchestrator-driven test flow that catches the same failures the user sees in live Issue #4057 runs, before the user finds them.
Iterate until results are as good as reasonably possible in this cycle (do not stop at first partial success).

## User Requirements (Non-Negotiable)
1. Realism first. No shortcuts for speed.
2. Use real coding and real review agents.
3. Run through the actual orchestrator process lifecycle (E2E-style), not ad-hoc direct helper calls.
4. Exercise the full publish gate path (`make validate`), same as real runs.
5. Verify expected artifacts and UI diagnostics availability at each stage.
6. Keep iterating until this test path catches the regressions seen in live 4057 runs.
7. Diagnostic artifacts must be accessible during the run, and `ui-session.log` must be viewable in near-real-time (~2s lag) **with continuously updated content**. Opening an empty/static log view is not sufficient. It is unacceptable to wait until agent completion to see or refresh `ui-session.log` content.

## Current Context / Why This Exists
- The user has repeatedly seen live #4057 failures not caught by prior tests.
- Main parity gap identified: prior "real" integration test (`tests/integration/test_real_coding_review_cycle.py`) is not truly orchestrator-driven. It manually launches subprocess session and directly calls review loop.
- E2E harness exists (`tests/e2e/*`) but currently defaults to script-based test agents (`complete-immediately.sh`, `review-decider.sh`) and weaker validation (`make typecheck`), which is not equivalent to live #4057 conditions.

## Known Findings So Far
- In a real run artifact set (`.../sessions/20260221-031820Z__coding-1`), publish gate failed with `make validate` (exit 2).
- `validation-output.log` showed failing integration tests unrelated to issue logic, including:
  - `tests/integration/test_codex_execution.py::TestCodexAgentDoneInvocation::test_agent_done_invocable_from_codex`
  - `tests/integration/test_subprocess_terminal.py::test_subprocess_session_writes_completion_and_log`
- Root causes identified and patched in this worktree:
  - missing git `origin` remote in Codex integration fixture
  - stale expected log filename (`session.log` vs canonical `ui-session.log`)
- Also added diagnostics action support for validation output in session diagnostics dialog:
  - `Open Validation Record`
  - `Open Validation Output`

## Mandatory Work Plan
1. Create/upgrade an E2E-style real-agent test that starts and runs the actual orchestrator process and mirrors live #4057 flow.
2. Ensure test configuration uses real coding/review agents (not script stubs).
3. Ensure publish gate command in that path is `make validate` (or exact production equivalent for this repo).
4. Use a realistic #4057-style prompt/input.
5. Assert phase-by-phase outputs:
   - coding session started
   - coding ui-session artifact present, non-empty, and continuously viewable while the session is running (near-real-time, ~2s lag) with live-updating content
   - review started and review artifacts present
   - validation record present
   - validation output artifact present/openable
   - timeline actions/diagnostics include relevant entries and openable artifact paths during execution
6. Fail hard on blocked-failed / publish-gate failure and preserve artifacts for audit.
7. Iterate until stable and deterministic enough to catch previously observed regressions.
8. Continue iterating and tightening assertions until the result quality is as high as can be achieved within scope.

## Acceptance Criteria
- A single command/test target reproduces production-like orchestrator flow for 4057.
- That test fails when publish gate or artifact wiring regresses, and passes when healthy.
- Failures are diagnosable from generated artifacts without forensic digging.
- No simulation-only success path accepted as substitute.

## Guardrails
- Do not hide bugs with fallback behavior. Prefer fail-fast and explicit diagnostics.
- Do not dilute validation scope to make tests pass.
- Keep changes architecture-aligned (ports/adapters, DI via bootstrap, event-driven UI/test sync).

## Working Location Lock (Mandatory)
- Continue all work in this exact worktree:
  - `/Users/brucegordon/dev/issue-orchestrator-wt-ui-recovery-pre4057`
- Current branch:
  - `ui-recovery-pre4057-base`
- Do **not** change worktree or branch without explicit written user consent.

## Reporting Requirement
When handing back to the user, explicitly state:
1. what production-like path is now covered,
2. what exact failure classes it now catches,
3. evidence (test names + pass/fail output summary),
4. any remaining gap (if any) with concrete next step.
