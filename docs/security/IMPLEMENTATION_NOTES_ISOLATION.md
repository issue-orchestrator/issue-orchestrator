# Implementation Notes: Default vs Hardened

This document captures the intended mechanical behavior without prescribing one exact OS mechanism.

---

## Standard mode launch contract

The agent session launcher must:
1) start with a clean env (`env -i` equivalent)
2) explicitly set a minimal PATH
3) set an isolated HOME per worktree:
   - e.g. `<worktree>/.issue-orchestrator/agent_home`
4) explicitly remove/blank sensitive env vars:
   - `GITHUB_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`, `GIT_ASKPASS`, etc.
5) set fast-fail git auth behavior:
   - `GIT_TERMINAL_PROMPT=0`
   - `GIT_ASKPASS=/usr/bin/false`
6) run sandbox verification once per worktree/session startup

Agents are allowed to run local git commands (including `git commit`). Agents should not have credentials required for `git push` or GitHub APIs.

---

## Hardened mode approaches (macOS)

Hardened mode needs a mechanism to launch agent processes as a different OS user.

Two viable approaches:
1) **LaunchDaemon runner** (sudo once; no sudo at runtime)
2) **Tightly scoped sudoers** for a single allowlisted runner command (may still use `sudo` but without prompting)

Either approach must:
- deny arbitrary shell execution
- validate inputs (worktree path/session identifiers)
- run agents with a minimal environment
- keep the orchestrator’s GitHub credentials out of the agent environment

---

## Validation and caching interplay

To avoid redundant validation:
- validation results are cached per worktree and HEAD SHA:
  - `.issue-orchestrator/validation/<HEAD_SHA>.json`
- `agent_done` runs validation for immediate feedback and writes the cache
- orchestrator consults the cache; reruns only if missing/mismatched/stale
- pre-push hook should consult the same cache and only re-run when needed

This preserves guardrails without normalizing bypass flags like `--no-verify`.

---

## Documentation claims (be precise)

Safe claims:
- Standard mode: reduces accidental credential inheritance; verified at worktree creation
- Hardened mode: prevents GitHub authentication by construction via OS-level isolation

Avoid claiming Standard mode is an adversarial sandbox.
