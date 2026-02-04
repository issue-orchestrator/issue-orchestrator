# Project Validation Script

This directory contains project-specific validation logic.

## Contract

The validation script is invoked by the orchestrator with JSON context on stdin.
It must:
- Read stdin (JSON)
- Return exit code `0` on success
- Return non-zero on failure
- Write any artifacts to the run directory in the context

## Current Script

`run.sh` reads stdin (context) and runs `make validate` for this repo.

## Sample Context (JSON)

```json
{
  "schema_version": 1,
  "mode": "agent_gate",
  "agent_label": "agent:reviewer",
  "repo_root": "/path/to/repo",
  "run_dir": "/path/to/run/dir",
  "config": { "...": "..." }
}
```

You can parse the context with Python or `jq` if you want to route output
into the run directory or apply mode-specific behavior.
