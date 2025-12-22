# Implementation Task List (explicit, agent-friendly)

This is the authoritative “what to do” checklist for implementing:
- single-command validation with suite+SHA caching
- sandbox verification and env scrubbing
- integration points (agent_done, publish gate, pre-push)
- tests

Each task has acceptance criteria.

---

## 0. Pre-flight
- [ ] Identify current modules/paths where:
  - worktrees are created
  - agent sessions are launched (tmux/iTerm/web)
  - agent_done is implemented
  - publish actions occur (push/PR/comment/labels)
  - existing validation/hook logic lives
Acceptance:
- [ ] Notes added to `docs/internal/planning/00-preflight-notes.md` with file paths.

## 1. Config: YAML keys for validation + isolation
- [ ] Add config schema keys:
  - `validation.publish_gate.cmd` + `timeout_seconds`
  - `validation.agent_gate.cmd` + `timeout_seconds` (optional)
  - `validation_policy.publish_requires`
  - `validation_policy.agent_runs` (optional)
  - `isolation.mode: standard|hardened` (standard default)
Acceptance:
- [ ] Config parsing rejects missing publish gate cmd when publish gating is enabled.
- [ ] Clear error messages for bad keys.

## 2. Validation record format + storage layout
- [ ] Define record location:
  - `.issue-orchestrator/validation/<suite>/<HEAD_SHA>.json`
- [ ] Define record schema (minimum):
  - schema_version, suite, head_sha, passed, exit_code, command, timestamps
Acceptance:
- [ ] Record writer and reader implemented.
- [ ] Record contains exact HEAD SHA and suite name.

## 3. Validation runner: run exactly one command per suite
- [ ] Implement `run_validation(cmd, cwd, timeout) -> outputs`
  - captures stdout/stderr
  - returns exit code and timing
- [ ] Implement `ValidationRunner.run(suite, worktree) -> ValidationRecord`
Acceptance:
- [ ] On failure, stdout/stderr captured and referenced in record.
- [ ] Timeout is enforced; record indicates timeout.

## 4. Validation cache lookup (suite + SHA)
- [ ] Implement `ValidationCache.lookup(suite, sha, worktree) -> Optional[ValidationRecord]`
- [ ] Implement `ValidationCache.is_acceptable(record, suite, sha, version) -> bool`
Acceptance:
- [ ] Cache hit requires exact suite + SHA.
- [ ] Cache miss triggers a run.

## 5. Integrate publish gate in orchestrator control plane
- [ ] Before push/PR creation, require publish suite:
  - lookup cache; if hit+pass -> proceed
  - else run publish validation once and write record
Acceptance:
- [ ] Publishing does not occur if publish gate fails.
- [ ] Cache hit prevents rerun for same SHA.

## 6. Integrate agent_done validation (optional)
- [ ] If configured, `agent_done` runs `agent_gate` and writes record
- [ ] `agent_done` provides fast feedback (and fails “done” if validation fails)
Acceptance:
- [ ] With agent_gate enabled, agent cannot complete “done” on failure.

## 7. Env scrubbing + isolated HOME for agent sessions (Standard mode)
- [ ] Implement `build_agent_env(worktree) -> env dict`
  - isolated HOME: `<worktree>/.issue-orchestrator/agent_home`
  - unset GH_TOKEN/GITHUB_TOKEN/SSH_AUTH_SOCK
  - set `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/usr/bin/false`
- [ ] Ensure all agent launchers use this env
Acceptance:
- [ ] Agent sessions do not inherit forbidden env vars.
- [ ] Agent sessions use isolated HOME.

## 8. Sandbox verification (per worktree/session)
- [ ] Implement `verify_agent_sandbox(worktree, mode) -> VerificationResult`
  - `gh auth status` fails
  - `git push --dry-run` fails fast
  - env invariants
  - mode-specific check
- [ ] Run verification at worktree creation or before agent session start
Acceptance:
- [ ] If verification fails, agent session is refused with clear message.

## 9. Pre-push hook consults cache first
- [ ] If cache hit for publish suite+SHA: exit 0
- [ ] Else run publish validation and write record
Acceptance:
- [ ] No redundant reruns when cache exists.

## 10. Docs + README wiring (public)
- [ ] Ensure public usage docs link to design docs without mixing audiences.
