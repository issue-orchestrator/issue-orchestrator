"""Shared setup-wizard helpers for CLI and Control Center entrypoints."""

from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from urllib.parse import urlparse

import yaml

if TYPE_CHECKING:
    from ..infra.config import Config


@dataclass
class PlannedWrite:
    """A file write that would be performed."""

    path: Path
    content: str
    action: str
    kind: str | None = None
    agent: str | None = None

    def size_display(self) -> str:
        """Return a human-readable size."""
        size = len(self.content.encode("utf-8"))
        if size < 1024:
            return f"{size} B"
        return f"{size / 1024:.1f} KB"


class FileCollector:
    """Collect planned writes and label mutations for dry-run workflows."""

    def __init__(self) -> None:
        self.writes: list[PlannedWrite] = []
        self.labels: list[tuple[str, str, str]] = []

    def add_write(
        self,
        path: Path,
        content: str,
        action: str = "create",
        *,
        kind: str | None = None,
        agent: str | None = None,
    ) -> None:
        """Record a planned file write."""
        self.writes.append(
            PlannedWrite(
                path=path,
                content=content,
                action=action,
                kind=kind,
                agent=agent,
            )
        )

    def add_label(self, name: str, color: str, description: str) -> None:
        """Record a planned GitHub label creation."""
        self.labels.append((name, color, description))


def get_repository_host(repo: str):
    """Get a RepositoryHost for the given repo."""
    from ..execution.providers import create_repository_host

    return create_repository_host(repo=repo)


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout_s: int = 10,
) -> tuple[bool, str]:
    """Run a git command and return ``(success, output)``."""
    from ..execution.git_tools import run_git as run_git_impl

    return run_git_impl(args, cwd=cwd, timeout_s=timeout_s)


def detect_repo(cwd: Path | None = None) -> str | None:
    """Detect ``owner/repo`` from the current git origin remote."""
    ok, output = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if not ok:
        return None

    url = output.strip()
    return _repo_path_from_github_remote(url)


def _repo_path_from_github_remote(url: str) -> str | None:
    """Extract ``owner/repo`` from GitHub remote URL formats."""
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1].removesuffix(".git")

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        if parsed.hostname != "github.com":
            return None
        repo_path = parsed.path.lstrip("/")
        return repo_path.removesuffix(".git") or None

    if "github.com" not in url:
        return None
    repo_path = "/".join(url.split("/")[-2:])
    return repo_path.removesuffix(".git") or None


def fetch_github_labels(repo: str) -> list[str]:
    """Fetch label names from GitHub for the given repository."""
    try:
        labels = get_repository_host(repo).list_labels()
    except Exception:
        return []

    names: list[str] = []
    for label in labels:
        if not isinstance(label, dict):
            continue
        name = label.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def find_existing_config(
    start_path: Path | None = None,
) -> tuple[Path | None, dict | None]:
    """Find an existing orchestrator config starting at ``start_path``."""
    from ..infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME

    if start_path is None:
        start_path = Path.cwd()

    candidates = [
        f"{CONFIG_DIR}/{DEFAULT_CONFIG_NAME}",
        f"{CONFIG_DIR}/*.yaml",
    ]

    current = start_path
    while current != current.parent:
        for candidate in candidates:
            if "*" in candidate:
                matches = list(current.glob(candidate))
                if not matches:
                    continue
                config_path = matches[0]
            else:
                config_path = current / candidate
                if not config_path.exists():
                    continue
            try:
                with open(config_path) as handle:
                    return config_path, yaml.safe_load(handle)
            except yaml.YAMLError:
                continue
        current = current.parent

    return None, None


def find_existing_default_config(
    start_path: Path | None = None,
) -> tuple[Path | None, dict | None]:
    """Find the legacy default config used by the Control Center setup API."""
    from ..infra.config import find_config_file

    config_path = find_config_file(start_path)
    if config_path is None:
        return None, None

    try:
        with open(config_path) as handle:
            return config_path, yaml.safe_load(handle)
    except Exception:
        return config_path, None


def find_prompt_candidates(start_path: Path | None = None) -> list[Path]:
    """Find likely prompt markdown files in a repository."""
    if start_path is None:
        start_path = Path.cwd()

    candidates: list[Path] = []
    high_priority_patterns = [
        ".prompts/**/*.md",
        "**/prompts/*.md",
        ".issue-orchestrator/prompts/**/*.md",
        "**/*orchestrator*.md",
        "**/*-agent*.md",
        "**/*_agent*.md",
    ]

    for pattern in high_priority_patterns:
        for path in start_path.glob(pattern):
            if not path.is_file() or path in candidates:
                continue
            if any(part.startswith(".") or part == "node_modules" for part in path.parts):
                continue
            candidates.append(path)

    if candidates:
        return sorted(set(candidates))

    docs_patterns = [
        "docs/**/*ai*.md",
        "docs/**/*agent*.md",
        "docs/**/*claude*.md",
    ]
    for pattern in docs_patterns:
        for path in start_path.glob(pattern):
            if path.is_file() and path not in candidates:
                candidates.append(path)

    return sorted(set(candidates))


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that disables anchors and aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


CONFIG_HEADER = """\
# Issue Orchestrator Configuration
#
# Template variables for initial_prompt and command:
#   {issue_number}    - GitHub issue number
#   {issue_title}     - Issue title
#   {prompt}          - Path to prompt file
#   {worktree}        - Path to worktree
#   {model}           - Model name from agent config
#   {permission_mode} - Claude permission mode
#   {pr_number}       - PR number (review/rework sessions only)
#
# See: https://github.com/anthropics/issue-orchestrator

"""


def render_config_yaml(
    config: Mapping[str, Any],
    *,
    include_header: bool = True,
) -> str:
    """Render config to YAML with stable formatting."""
    buffer = io.StringIO()
    yaml.dump(
        dict(config),
        buffer,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    content = buffer.getvalue()
    return CONFIG_HEADER + content if include_header else content


def write_config(
    config: Mapping[str, Any],
    path: Path,
    file_collector: FileCollector | None = None,
    *,
    include_header: bool = True,
) -> None:
    """Write a config file or add it to a dry-run collector."""
    content = render_config_yaml(config, include_header=include_header)

    if file_collector is not None:
        action = "overwrite" if path.exists() else "create"
        file_collector.add_write(path, content, action, kind="config")
        return

    with open(path, "w") as handle:
        handle.write(content)


def build_starter_prompt_text(agent_short: str) -> str:
    """Build the canonical work-agent prompt text."""
    return f"""# {agent_short.title()} Agent Prompt

You are working on issue #{{issue_number}}: {{issue_title}}

## Your Role
You are the {agent_short} agent responsible for implementing changes in this area.

## Working Directory
Your worktree is at: {{worktree}}

## Core Principle

**You report intent; the orchestrator executes.**

You do NOT:
- Push code (`git push` is blocked by hooks)
- Create PRs
- Post GitHub comments
- Mutate labels

The orchestrator handles all GitHub operations after you complete your work.

## Instructions
1. Read the issue carefully and understand the requirements
2. Implement the necessary changes
3. Write tests if applicable
4. Run existing tests to ensure nothing is broken
5. Commit your changes locally
6. Use `coding-done` to signal completion (see below)

## Completion (MANDATORY)

You MUST use `coding-done` to complete. This runs quick validation, then the orchestrator pushes your code and creates the PR.

### When work is complete:
```bash
coding-done completed \\
  --implementation "Brief description of what you implemented" \\
  --problems "Any issues encountered, or 'None'"
```

### If blocked (cannot proceed):
```bash
coding-done blocked \\
  --reason "Why you cannot proceed" \\
  --attempted "What you tried"
```

### If you need human input:
```bash
coding-done needs_human \\
  --question "Specific question for the human"
```

Run `coding-done --help or reviewer-done --help` for all options.

**What happens after `coding-done`:**
1. Quick validation runs (tests, linting) - if it fails, fix and retry
2. Orchestrator pushes your branch
3. Orchestrator creates PR and posts comment
4. Session completes
"""


def build_code_review_prompt_text(
    code_review_label: str,
    code_reviewed_label: str,
) -> str:
    """Build the canonical code-review prompt text."""
    return f"""# Code Review Agent

You are a code reviewer. Your job is to review PRs created by work agents, checking code quality, test coverage, and adherence to best practices.

## Your Task

You are reviewing PR #{{pr_number}} for issue #{{issue_number}}: {{issue_title}}

The PR has the `{code_review_label}` label and needs your review.

## Core Principle

**You report intent; the orchestrator executes.**

You do NOT:
- Call `gh pr review` or `gh pr edit`
- Post GitHub comments directly
- Mutate labels

You analyze the code and report your verdict via `reviewer-done`. The orchestrator handles all GitHub operations.

## Review Process

### 1. Fetch PR Details (read-only)

```bash
gh pr view {{pr_number}} --json title,body,additions,deletions,changedFiles,commits
gh pr diff {{pr_number}}
```

### 2. Review Checklist

Check each area and note any issues:

- [ ] **Code Quality**: Clean, readable, follows project conventions
- [ ] **Logic**: Implementation is correct and handles edge cases
- [ ] **Tests**: Adequate test coverage for changes
- [ ] **Security**: No obvious vulnerabilities introduced
- [ ] **Performance**: No obvious performance issues
- [ ] **Documentation**: Comments where needed, README updates if applicable

### 3. Run Tests

```bash
# Run the project's test suite
# Adjust command based on project type
npm test  # or pytest, cargo test, etc.
```

## Completion (MANDATORY)

Use `reviewer-done` to report your verdict. The orchestrator will post your review and update labels.

### If the PR looks good:

```bash
reviewer-done approved \\
  --summary "Brief summary of what you reviewed and why it's good" \\
  --risk low
```

### If changes are needed:

```bash
reviewer-done changes_requested \\
  --issues "Specific issues that need fixing (be detailed)" \\
  --risk medium
```

**What happens after `reviewer-done`:**
1. Orchestrator posts your review comment on the PR
2. Orchestrator updates labels (`{code_review_label}` → `{code_reviewed_label}` or triggers rework)
3. If changes requested, work agent is re-queued to fix issues

## Review Principles

1. **Be constructive** - Explain why something should change, not just that it should
2. **Be specific** - Point to exact lines/files in your `--issues` or `--summary`
3. **Prioritize** - Distinguish blocking issues from nice-to-haves
4. **Be consistent** - Apply the same standards across all PRs
5. **Trust but verify** - Check that tests actually test the changes
"""


def build_triage_review_prompt_text(
    review_label: str,
    reviewed_label: str,
) -> str:
    """Build the canonical triage-review prompt text."""
    return f"""# Triage Review Agent

You are a triage/technical advisor **auditing** work done by AI agents.

**Important:** You do NOT approve PRs - that's for humans. Your job is to:
- Identify patterns across PRs (good and bad)
- Flag concerns for human review
- Suggest process improvements

## Core Principle

**You report intent; the orchestrator executes.**

You do NOT:
- Call `gh pr comment` or `gh pr edit`
- Call `gh issue create`
- Post GitHub comments directly
- Mutate labels

You analyze PRs and report findings via `reviewer-done`. The orchestrator handles all GitHub operations.

## Review Process

### 1. Find PRs to Audit (read-only)

```bash
gh pr list --label "{review_label}" --json number,title,body,url,headRefName
```

**If no PRs found:** Complete with "No PRs to review".

### 2. For Each PR, Investigate (read-only)

```bash
# Get PR details
gh pr view <number> --json title,body,additions,deletions,files

# See the code changes
gh pr diff <number>

# Check linked issue for context
gh issue view <linked_issue_number> --comments
```

Evaluate:
- **Code quality**: Clean, maintainable implementation?
- **Completeness**: Fully addresses the issue?
- **Testing**: Tests present? Edge cases covered?
- **Patterns**: Recurring issues across PRs?

### 3. Document Your Findings

As you review, build a mental report:

**For each PR:**
- PR number and title
- What you checked
- Status: No concerns / Minor concerns / Significant concerns
- Specific feedback

**Patterns observed:**
- Recurring issues across PRs
- Common mistakes
- Good practices to encourage

**Process improvements:**
- Suggestions for agent prompts
- Workflow improvements

## Completion (MANDATORY)

Use `reviewer-done` to report your findings. The orchestrator will post your report and update labels.

```bash
reviewer-done approved \\
  --summary "Audited N PRs. Summary: X no concerns, Y flagged. Patterns: [key patterns]. Recommendations: [suggestions]" \\
  --risk low
```

**If no PRs to review:**
```bash
reviewer-done approved \\
  --summary "No PRs with '{review_label}' label found. Nothing to audit." \\
  --risk low
```

**What happens after `reviewer-done`:**
1. Orchestrator posts your triage report as a comment
2. Orchestrator updates PR labels (`{review_label}` → `{reviewed_label}`)
3. Session completes

## Audit Principles

- **Be constructive** - agents are learning from your feedback
- **Focus on patterns** - individual issues matter less than systemic ones
- **Note what's good** - reinforcement helps improve agent behavior
- **Suggest prompt improvements** - if agents keep making the same mistake, the prompt needs work
- **Document everything** - always log what you checked, even if nothing was found
- **Flag, don't approve** - your job is to surface concerns, humans make final decisions
- **Don't block for style** - focus on correctness and maintainability
"""


def create_starter_prompt(
    agent_name: str,
    path: Path,
    file_collector: FileCollector | None = None,
) -> None:
    """Create a starter prompt file for a work agent."""
    content = build_starter_prompt_text(agent_name.split(":")[-1])
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_code_review_prompt(
    path: Path,
    code_review_label: str,
    code_reviewed_label: str,
    file_collector: FileCollector | None = None,
    *,
    agent_name: str | None = None,
) -> None:
    """Create a code-review prompt file."""
    content = build_code_review_prompt_text(code_review_label, code_reviewed_label)
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_triage_review_prompt(
    path: Path,
    review_label: str,
    reviewed_label: str,
    file_collector: FileCollector | None = None,
    *,
    agent_name: str | None = None,
) -> None:
    """Create a triage-review prompt file."""
    content = build_triage_review_prompt_text(review_label, reviewed_label)
    if file_collector is not None:
        file_collector.add_write(path, content, "create", kind="prompt", agent=agent_name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_missing_setup_prompts(
    config: Mapping[str, Any],
    repo_root: Path,
    file_collector: FileCollector | None = None,
) -> list[Path]:
    """Create or collect any missing prompt files referenced by the config."""
    review_config = config.get("review", {}) or {}
    code_review_agent = review_config.get("default")
    code_review_label = review_config.get("code_review_label", "needs-code-review")
    code_reviewed_label = review_config.get("code_reviewed_label", "code-reviewed")
    triage_review_agent = review_config.get("triage_review_agent")
    triage_reviewed_label = review_config.get("triage_reviewed_label", "triage-reviewed")

    created_paths: list[Path] = []
    for agent_name, agent_config in (config.get("agents", {}) or {}).items():
        if not isinstance(agent_config, Mapping):
            continue
        prompt_rel = agent_config.get("prompt", "")
        if not isinstance(prompt_rel, str) or not prompt_rel:
            continue

        prompt_path = Path(prompt_rel)
        if not prompt_path.is_absolute():
            prompt_path = repo_root / prompt_path
        if prompt_path.exists():
            continue

        is_code_review_agent = (
            agent_name == code_review_agent or agent_name.lower() == "agent:reviewer"
        )
        is_triage_review_agent = (
            agent_name == triage_review_agent or "triage" in agent_name.lower()
        )

        if is_code_review_agent:
            create_code_review_prompt(
                prompt_path,
                code_review_label,
                code_reviewed_label,
                file_collector=file_collector,
                agent_name=agent_name,
            )
        elif is_triage_review_agent:
            create_triage_review_prompt(
                prompt_path,
                code_reviewed_label,
                triage_reviewed_label,
                file_collector=file_collector,
                agent_name=agent_name,
            )
        else:
            create_starter_prompt(agent_name, prompt_path, file_collector=file_collector)
        created_paths.append(prompt_path)

    return created_paths


def plan_setup_labels(
    config: Mapping[str, Any],
    *,
    include_priority_labels: bool = True,
    include_review_labels_without_default: bool = False,
) -> list[tuple[str, str, str]]:
    """Return the label set for a setup-wizard config."""
    labels_config = config.get("labels", {}) or {}
    review_config = config.get("review", {}) or {}
    return _plan_setup_labels(
        labels_config=labels_config,
        review_config=review_config,
        agent_names=(config.get("agents", {}) or {}).keys(),
        include_priority_labels=include_priority_labels,
        include_review_labels_without_default=include_review_labels_without_default,
    )


def _plan_setup_labels(
    *,
    labels_config: Mapping[str, Any],
    review_config: Mapping[str, Any],
    agent_names: Iterable[str],
    include_priority_labels: bool = True,
    include_review_labels_without_default: bool = False,
) -> list[tuple[str, str, str]]:
    """Build setup labels for CLI and Control Center surfaces."""
    label_prefix = labels_config.get("prefix", "")

    def prefixed(label: str) -> str:
        return f"{label_prefix}:{label}" if label_prefix else label

    agent_labels = [
        (agent_name, "1D76DB", f"Issues for {agent_name.split(':')[-1]} agent")
        for agent_name in agent_names
    ]
    priority_labels = [
        ("priority:high", "D93F0B", "Urgent - do first"),
        ("priority:medium", "FBCA04", "Normal priority"),
        ("priority:low", "0E8A16", "Nice to have"),
    ]
    status_labels = [
        (
            prefixed(labels_config.get("in_progress", "in-progress")),
            "5319E7",
            "Agent is working on this",
        ),
        (
            prefixed(labels_config.get("blocked", "blocked")),
            "B60205",
            "Agent is blocked",
        ),
        (
            prefixed(labels_config.get("needs_human", "needs-human")),
            "FBCA04",
            "Agent needs human input",
        ),
    ]

    all_labels = agent_labels + status_labels
    if include_priority_labels:
        all_labels.extend(priority_labels)

    code_review_agent = review_config.get("default")
    review_enabled = bool(review_config.get("enabled"))
    if code_review_agent or (include_review_labels_without_default and review_enabled):
        all_labels.extend(
            [
                (
                    review_config.get("code_review_label", "needs-code-review"),
                    "7057FF",
                    "PR needs code review",
                ),
                (
                    review_config.get("code_reviewed_label", "code-reviewed"),
                    "0E8A16",
                    "PR has been code reviewed",
                ),
            ]
        )

    triage_review_agent = review_config.get("triage_review_agent")
    if triage_review_agent:
        all_labels.append(
            (
                review_config.get("triage_reviewed_label", "triage-reviewed"),
                "1D76DB",
                "PR has been triage reviewed",
            )
        )

    return all_labels


def load_config_for_repo(repo_root: Path | None) -> "Config | None":
    """Load the default config for a repo when one exists."""
    from ..infra.config import Config, DEFAULT_CONFIG_NAME, get_config_path, list_configs

    if repo_root is None:
        return None
    available = list_configs(repo_root)
    if not available:
        return None
    config_name = DEFAULT_CONFIG_NAME if DEFAULT_CONFIG_NAME in available else available[0]
    config_path = get_config_path(repo_root, config_name)
    try:
        return Config.load(config_path)
    except Exception:
        return None


def _probe_cli_version(executable: str, *, fallback: str) -> str:
    """Return ``<cli> --version`` output when available."""
    from ..execution.command_runner import LocalCommandRunner

    result = LocalCommandRunner().run([executable, "--version"], timeout_seconds=5)
    if result.returncode == 0:
        detail = result.stdout.strip() or result.stderr.strip()
        if detail:
            return detail
    return fallback


def _provider_cli_display(provider_name: str, executable: str) -> str:
    if executable == provider_name:
        return provider_name
    return f"{provider_name} via {executable}"


def _provider_cli_detail(detail: str, provider_name: str, executable: str) -> str:
    if executable == provider_name:
        return detail
    return f"{detail} (executable: {executable})"


def build_any_ai_provider_check() -> dict[str, Any]:
    """Check whether at least one registered AI provider CLI is available."""
    from issue_orchestrator.agent_runner import get_provider, list_providers

    providers = list_providers()
    available: list[str] = []
    for provider_name in providers:
        provider = get_provider(provider_name)
        executable = getattr(provider, "executable", provider_name)
        if provider.is_available():
            available.append(_provider_cli_display(provider_name, executable))

    if available:
        return {
            "ok": True,
            "detail": "Available: " + ", ".join(available),
        }
    return {
        "ok": False,
        "detail": "No AI provider CLIs found. Install one of: " + ", ".join(providers),
    }


def _build_provider_agent_check(
    *,
    label: str,
    provider_name: str,
    seen_executables: set[str],
) -> dict[str, Any] | None:
    from issue_orchestrator.agent_runner import get_provider

    from ..infra.provider_cli_diagnostics import provider_cli_missing_detail

    try:
        provider = get_provider(provider_name)
    except ValueError:
        return {
            "name": f"{provider_name} CLI",
            "ok": False,
            "detail": f"Unknown provider configured for {label}: {provider_name}",
        }

    executable = getattr(provider, "executable", provider_name)
    if executable in seen_executables:
        return None
    seen_executables.add(executable)
    if not provider.is_available():
        return {
            "name": f"{provider_name} CLI",
            "ok": False,
            "detail": provider_cli_missing_detail(provider_name, executable),
        }
    path = shutil.which(executable) or executable
    detail = provider.check_version() or path
    return {
        "name": f"{provider_name} CLI",
        "ok": True,
        "detail": _provider_cli_detail(detail, provider_name, executable),
    }


def build_agent_checks(config: "Config | None") -> list[dict[str, Any]]:
    """Check agent CLI availability for the supplied config."""
    if config is None:
        return [{
            "name": "Agent CLI",
            "ok": True,
            "detail": "Config not detected yet",
        }]

    checks: list[dict[str, Any]] = []
    seen_executables: set[str] = set()
    for label, agent_config in config.agents.items():
        provider_name = getattr(agent_config, "provider", None)
        default_agent = getattr(config, "default_agent", None)
        if provider_name is None and default_agent is not None:
            provider_name = getattr(default_agent, "provider", None)
        if provider_name:
            check = _build_provider_agent_check(
                label=label,
                provider_name=provider_name,
                seen_executables=seen_executables,
            )
            if check:
                checks.append(check)
            continue

        command = getattr(agent_config, "command", None) or ""
        executable = command.strip().split()[0] if command.strip() else ""
        if not executable:
            checks.append({
                "name": f"{label} CLI",
                "ok": False,
                "detail": "No command configured",
            })
            continue
        exec_name = executable.rsplit("/", 1)[-1]
        if exec_name in seen_executables:
            continue
        seen_executables.add(exec_name)
        path = shutil.which(exec_name)
        if not path:
            checks.append({
                "name": f"{exec_name} CLI",
                "ok": False,
                "detail": "Not found on PATH",
            })
            continue
        checks.append({
            "name": f"{exec_name} CLI",
            "ok": True,
            "detail": _probe_cli_version(path, fallback=path),
        })
    return checks


def build_github_auth_check(config: "Config | None") -> dict[str, Any]:
    """Validate GitHub auth using repo-specific config when available."""
    from ..execution.providers import validate_github_token

    auth_kwargs = config.github_auth_kwargs() if config else {}
    token_result = validate_github_token(
        **auth_kwargs,
        repo=getattr(config, "repo", None) if config else None,
        api_url=getattr(config, "github_api_url", "https://api.github.com")
        if config
        else "https://api.github.com",
    )
    if token_result.valid:
        detail = f"Authenticated as {token_result.username}"
        if config and getattr(config, "repo", None):
            detail += f" with access to {config.repo}"
        return {"ok": True, "detail": detail}
    return {"ok": False, "detail": token_result.error or "Unknown error"}


__all__ = [
    "CONFIG_HEADER",
    "FileCollector",
    "PlannedWrite",
    "build_agent_checks",
    "build_any_ai_provider_check",
    "build_code_review_prompt_text",
    "build_github_auth_check",
    "build_starter_prompt_text",
    "build_triage_review_prompt_text",
    "create_code_review_prompt",
    "create_starter_prompt",
    "create_triage_review_prompt",
    "detect_repo",
    "fetch_github_labels",
    "find_existing_config",
    "find_existing_default_config",
    "find_prompt_candidates",
    "get_repository_host",
    "load_config_for_repo",
    "plan_setup_labels",
    "render_config_yaml",
    "run_git",
    "write_config",
    "write_missing_setup_prompts",
]
