# Repo-Specific Artifacts

This directory contains artifacts specific to issue-orchestrator's use of itself.
These are NOT framework examples - they are our working configuration.

## What belongs here

- `prompts/` - Prompts for agents working on this repo
- `scripts/` - Repo-maintainer scripts (not framework/user-facing)
- `hooks/` - Repo-maintainer hook extensions run by local guardrails
- `config/` - Repo-maintainer guardrail/profiling configuration
- `Makefile` - Repo-maintainer targets (for internal profiling/workflows)

## What does NOT belong here

- Framework examples (those go in `examples/`)
- Framework config schema (that's in `src/issue_orchestrator/`)
- User-facing documentation (that's in `docs/`)

## Prompts

The prompts in `prompts/` are task-focused. They do NOT need to include `coding-done`/`reviewer-done`
documentation - that is automatically injected by the framework into every agent's
system prompt.

If you're looking for prompt templates to copy for your own repo, see `examples/prompts/`.
