# Undtraced Run Playbook

This doc captures the lessons learned while chasing the long-running e2e/test issue runs (most recently issue 4057) that keep stalling in the live orchestration pipeline. Think of it as the "undocumented trace" of what we need to monitor, what files we produce, and how we stay aligned when the run is still alive and the worktree is shared between coder and reviewer.

## 1. Purpose

- Provide an actionable checklist for keeping the symptoms of these runs visible when nothing produces `ui-session.log`.
- Keep everyone on the same page for how `via-local-loop` is supposed to behave (single worktree, shared run directory, reviewer finishing only after coder tests complete).
- Capture the specific artifacts, log paths, and fallbacks that let us reconstruct an entire run for post-mortem debugging.

## 2. Observability & artifacts per run

1. `run_dir/` (one per issue/batch). Every live run writes its own `ui-session.log`, `claude.jsonl`, and `session-latest.json` that we want to persist even after compaction or cleanup. If the orchestrator deletes these files before we finish reviewing a failure, copy them into `run_dir/artifacts/` so we have traceability.
2. `ui-session.log` – the terminal transcript; not terribly user friendly but still the easiest way to see what happened. When it is empty (e.g., because we are streaming directly to Claude with `-p`), fall back to the stream that ManifestAccessor is about to ship so `/api/log/local` can still replay the JSONL.
3. `claude.jsonl` – the Claude JSONL transcript. Store this anywhere the reviewer can fetch it manually when a `agent_done` parse error happens.
4. `session-latest.json` – the orchestrator’s state snapshot. Keep it up to date for every run; if it is missing we no longer know whether `agent_done` succeeded or timed out.
5. `run_dir/coder/` and `run_dir/reviewer/` (proposed structure) – we run both logical personas inside the same git worktree but separate their log directories so no one steps on the other.

## 3. `via-local-loop` flow enforcement

- **Single worktree** – coder and reviewer should operate inside the same git worktree; the reviewer should not create detached heads. If the orchestrator still creates a new worktree per persona, treat that as a bug and drive it back to `via-local-loop` semantics (same git branch, shared run state).
- **Coder responsibilities** – run `make validate`/`make validate-quick` from the shared worktree, ensure tests pass, and emit `coder_done` (eventually `reviewer_done`) so the orchestrator knows the work is complete.
- **Reviewer responsibilities** – do not re-run the entire test suite; only run selective validation if a change was requested. The reviewer is mostly reading the artifact logs and calling `agent-done approved/changes_requested`. Introduce a `reviewer_done` signal in the future so we can differentiate the two completion scopes without rerunning synthesis steps.

## 4. Logging and streaming tips

- Claude must stream its JSONL so we can recover incremental results. Always launch Claude with `-p` so `claude` streams stdout; that stream is what we capture in `claude.jsonl` and feed into `/api/log/local` whenever `ui-session.log` is empty.
- Keep the fallback path in `ManifestAccessor` in sync: if `ui-session.log` lacks output, the API should transparently switch to the live JSONL stream rather than dropping the request.
- Capture the stdout/JSONL stream in a dedicated file under `run_dir/artifacts/` so we can replay the reviewer session even if the orchestrator prunes `ui-session.log`.

## 5. Artifact registry (for dirty-check exemption)

Whenever the orchestrator or the e2e tooling creates helper files (logs, register files, temp CSVs, run markers), register them ahead of time so `make worktree-setup`/`make validate` do not treat them as dirty. The list we currently know about:

- `ui-session.log`
- `claude.jsonl`
- `session-latest.json`
- Any `run_dir/*/artifacts` snapshots we create manually for debugging (zip or JSONL dumps)
- CLI caches produced by the live run (e.g., `run_dir/*/cache/*.json`)

Add newly discovered helpers to the registry immediately; this prevents future `make validate`/`git status` noise.

## 6. Handoff summary template (the "undtraced doc" payload)

When a run is still warm or a PR is in flight, publish a summary that follows the format we keep in this doc’s handoff section. Each summary should include:

1. **Progress so far** – mention the issue number (e.g., 4057), whether a run is active, what artifacts were generated, and any failures observed.
2. **Tests executed** – record commands (`make validate`, `make validate-quick`, or pytest invocation) and their outcomes.
3. **Push/PR status** – note whether a PR was created, what branch it targeted, and the current reviewer verdict if available.
4. **Context** – remind readers that this is part of the via-local-loop/e2e orbit and that `agent_done`/`reviewer_done` signals govern completion.
5. **Next steps** – suggest retries (e.g., rerun live e2e once `reviewer_done` is implemented), additional debugging areas (ui logs, JSONL stream), or configuration clarifications (special label filtering or issue-number filtering).

This section doubles as the eventual final handoff note you will keep inside the repo (the doc you just added) plus the chat summary we send when the run completes.
