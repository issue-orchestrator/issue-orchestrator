# agent-runner

Provider-agnostic AI agent execution for issue-orchestrator.

## Overview

agent-runner provides a simple, single-shot execution model for AI coding agents. It handles:

- Subprocess invocation with proper isolation
- Timeout management
- Output capture to files
- Environment variable filtering (security)
- Provider-specific command building (Claude, Codex)

## Installation

```bash
pip install -e packages/agent_runner
```

## Usage

```python
from pathlib import Path
from agent_runner import AgentRunner, RunSpec
from agent_runner.providers import ClaudeCodeProvider

# Build command using a provider
provider = ClaudeCodeProvider()
command = provider.build_command(
    prompt="Fix the bug in auth.py",
    model="sonnet",
)

# Run the agent
runner = AgentRunner()
result = runner.run(RunSpec(
    command=command,
    working_dir=Path("/path/to/repo"),
    timeout_seconds=300,
    output_dir=Path("/path/to/output"),
))

# Check result
if result.succeeded:
    print("Agent completed successfully")
    print(f"Output: {result.stdout}")
elif result.timed_out:
    print("Agent timed out")
else:
    print(f"Agent failed with exit code {result.exit_code}")
    print(f"Errors: {result.stderr}")
```

## Providers

### Claude Code

```python
from agent_runner.providers import ClaudeCodeProvider

provider = ClaudeCodeProvider()
cmd = provider.build_command(
    prompt="Fix the bug",
    model="sonnet",  # or "haiku", "opus", full model ID
    permission_mode="bypassPermissions",  # default
    system_prompt="Additional instructions",  # optional
)
```

### Codex

```python
from agent_runner.providers import CodexProvider

provider = CodexProvider()
cmd = provider.build_command(
    prompt="Fix the bug",
    model="gpt-5-codex",
    approval_mode="full-auto",  # or "yolo", "default"
    sandbox="workspace-write",  # optional
)
```

## Environment Filtering

agent-runner automatically scrubs sensitive environment variables:

- `GH_TOKEN`, `GITHUB_TOKEN`
- `AWS_SECRET_ACCESS_KEY`, `AWS_ACCESS_KEY_ID`
- `SSH_AUTH_SOCK`
- And more...

You can customize filtering:

```python
from agent_runner import RunSpec

spec = RunSpec(
    command=["agent", "run"],
    working_dir=Path("/repo"),
    timeout_seconds=300,
    output_dir=Path("/output"),
    env_scrub=["CUSTOM_SECRET"],  # Additional vars to remove
    env_overrides={"MY_VAR": "value"},  # Vars to set
    env_passthrough=["PATH", "HOME"],  # Only pass these (allowlist mode)
)
```

## Design Philosophy

agent-runner is intentionally minimal. It does NOT:

- Retry on failure (orchestrator's responsibility)
- Run validation (orchestrator's responsibility)
- Parse completion files (orchestrator's responsibility)
- Manage terminal sessions (orchestrator's responsibility)

This keeps the boundary clean: agent-runner executes, orchestrator coordinates.
