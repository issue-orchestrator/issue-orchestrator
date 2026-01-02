"""Interactive setup wizard for issue-orchestrator."""

import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml


class Prompter(Protocol):
    """Protocol for user interaction - enables testing via dependency injection."""

    def print(self, message: str) -> None:
        """Print a message to the user."""
        ...

    def input(self, question: str, default: str = "") -> str:
        """Prompt for text input with optional default."""
        ...

    def yes_no(self, question: str, default: bool = True) -> bool:
        """Prompt for yes/no answer."""
        ...

    def choice(self, question: str, choices: list[str], allow_custom: bool = False) -> str:
        """Prompt user to choose from a list."""
        ...


class ConsolePrompter:
    """Real console-based prompter for interactive use."""

    def print(self, message: str) -> None:
        print(message)

    def input(self, question: str, default: str = "") -> str:
        if default:
            result = input(f"{question} [{default}]: ").strip()
            return result if result else default
        return input(f"{question}: ").strip()

    def yes_no(self, question: str, default: bool = True) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        result = input(f"{question} {suffix}: ").strip().lower()
        if not result:
            return default
        return result in ("y", "yes")

    def choice(self, question: str, choices: list[str], allow_custom: bool = False) -> str:
        print(f"\n{question}")
        for i, choice in enumerate(choices, 1):
            print(f"  {i}. {choice}")
        if allow_custom:
            print(f"  {len(choices) + 1}. Other (enter custom)")

        while True:
            result = input("Choice: ").strip()
            try:
                idx = int(result) - 1
                if 0 <= idx < len(choices):
                    return choices[idx]
                if allow_custom and idx == len(choices):
                    return input("Enter custom value: ").strip()
            except ValueError:
                pass
            print("Invalid choice, try again.")


# Default prompter for backwards compatibility
_default_prompter = ConsolePrompter()


@dataclass
class DetectedState:
    """What we found in an existing repo."""

    repo: str | None = None
    github_labels: list[str] = field(default_factory=list)
    agent_labels: list[str] = field(default_factory=list)
    existing_config: dict | None = None
    config_path: Path | None = None
    prompt_candidates: list[Path] = field(default_factory=list)


def _github_adapter(repo: str):
    """Get a GitHubAdapter for the given repo.

    All GitHub access in setup wizard is routed through the adapter for
    consistent auditing and rate-limit handling.
    """
    from .adapters.github import GitHubAdapter

    return GitHubAdapter(repo=repo)


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run git command, return (success, output)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, ""


def check_prerequisites() -> dict[str, bool]:
    """Check if required tools are installed."""
    checks = {}

    # git
    ok, _ = run_git(["--version"])
    checks["git"] = ok

    # GitHub token
    try:
        from .adapters.github.http_client import resolve_github_token

        resolve_github_token(configured_token=None, configured_env=None)
        checks["github_auth"] = True
    except Exception:
        checks["github_auth"] = False

    # claude CLI
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["claude"] = result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        checks["claude"] = False

    return checks


def detect_repo(cwd: Path | None = None) -> str | None:
    """Detect GitHub repo from git remote."""
    ok, output = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if not ok:
        return None

    # Parse GitHub URL formats
    url = output.strip()
    if "github.com" in url:
        # https://github.com/owner/repo.git or git@github.com:owner/repo.git
        if url.startswith("git@"):
            # git@github.com:owner/repo.git
            parts = url.split(":")[-1]
        else:
            # https://github.com/owner/repo.git
            parts = "/".join(url.split("/")[-2:])
        return parts.removesuffix(".git")
    return None


def fetch_github_labels(repo: str) -> list[str]:
    """Fetch all labels from GitHub repo."""
    try:
        labels = _github_adapter(repo).list_labels()
        names: list[str] = []
        for label in labels:
            if not isinstance(label, dict):
                continue
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
        return names
    except Exception:
        return []


def find_existing_config(start_path: Path | None = None) -> tuple[Path | None, dict | None]:
    """Find existing config file."""
    if start_path is None:
        start_path = Path.cwd()
    candidates = [
        ".issue-orchestrator.yaml",
        ".issue-orchestrator/config.yaml",
    ]

    current = start_path
    while current != current.parent:
        for candidate in candidates:
            config_path = current / candidate
            if config_path.exists():
                try:
                    with open(config_path) as f:
                        return config_path, yaml.safe_load(f)
                except yaml.YAMLError:
                    pass
        current = current.parent

    return None, None


def find_prompt_candidates(start_path: Path | None = None) -> list[Path]:
    """Find potential prompt files in the repo.

    Looks for markdown files that are likely agent prompts based on naming/location.
    """
    if start_path is None:
        start_path = Path.cwd()
    candidates = []

    # High-priority patterns (likely prompts)
    high_priority_patterns = [
        ".issue-orchestrator/**/*.md",
        "**/prompts/*.md",
        "**/*orchestrator*.md",
        "**/*-agent*.md",
        "**/*_agent*.md",
    ]

    # Check high-priority first
    for pattern in high_priority_patterns:
        for path in start_path.glob(pattern):
            if path.is_file() and path not in candidates:
                # Skip node_modules, .git, etc.
                if any(part.startswith(".") or part == "node_modules" for part in path.parts):
                    continue
                candidates.append(path)

    # If we found some, return those
    if candidates:
        return sorted(set(candidates))

    # Fallback: look in docs for AI-related files
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


def scan_existing_repo(path: Path | None = None) -> DetectedState:
    """Scan an existing repo and detect its state."""
    if path is None:
        path = Path.cwd()
    state = DetectedState()

    # Detect repo from the target path
    state.repo = detect_repo(cwd=path)

    # Find existing config
    state.config_path, state.existing_config = find_existing_config(path)

    # Fetch GitHub labels if we have a repo
    if state.repo:
        state.github_labels = fetch_github_labels(state.repo)
        state.agent_labels = [l for l in state.github_labels if l.startswith("agent:")]

    # Find prompt candidates
    state.prompt_candidates = find_prompt_candidates(path)

    return state


def wizard_new_project(prompter: Prompter) -> dict[str, Any]:
    """Walk through new project setup."""
    config: dict[str, Any] = {"agents": {}}

    prompter.print("\n" + "=" * 50)
    prompter.print("NEW PROJECT SETUP")
    prompter.print("=" * 50)

    # Repo
    detected_repo = detect_repo()
    if detected_repo:
        config["repo"] = prompter.input("GitHub repo", detected_repo)
    else:
        config["repo"] = prompter.input("GitHub repo (owner/name)")

    # Agents
    prompter.print("\n--- Agent Configuration ---")
    prompter.print("Agents are identified by GitHub labels (e.g., 'agent:backend').")
    prompter.print("Each agent needs a prompt file with instructions.\n")

    while True:
        agent_name = prompter.input("Agent label (e.g., 'agent:backend', or empty to finish)")
        if not agent_name:
            if not config["agents"]:
                prompter.print("You need at least one agent!")
                continue
            break

        if not agent_name.startswith("agent:"):
            if prompter.yes_no(f"Add 'agent:' prefix to make it 'agent:{agent_name}'?"):
                agent_name = f"agent:{agent_name}"

        prompt_path = prompter.input(
            f"Prompt file path for {agent_name}",
            f".issue-orchestrator/prompts/{agent_name.split(':')[-1]}.md",
        )

        timeout = prompter.input("Timeout in minutes", "45")

        # Ask about custom command
        prompter.print("\n  Agent command options:")
        prompter.print("    claude  - Use Claude Code CLI (default)")
        prompter.print("    custom  - Use a custom command/script")
        agent_type = prompter.choice("Agent type", ["claude", "custom"])

        custom_command = None
        permission_mode = "default"

        if agent_type == "custom":
            prompter.print("\n  Enter your custom command template. Available variables:")
            prompter.print("    {issue_number}, {issue_title}, {prompt}, {worktree}, {model}")
            custom_command = prompter.input("Custom command")
            model = "sonnet"  # Not relevant for custom, but keep a default
        else:
            model = prompter.choice("Model for this agent", ["sonnet", "opus", "haiku"])

            # Permission mode for Claude CLI
            prompter.print("\n  Permission mode controls how Claude handles tool permissions:")
            prompter.print("    default          - Prompt for each action (safest)")
            prompter.print("    acceptEdits      - Auto-accept file edits, prompt for others")
            prompter.print("    bypassPermissions - Skip all prompts (use for trusted automation)")
            permission_mode = prompter.choice(
                "Permission mode",
                ["default", "acceptEdits", "bypassPermissions"]
            )

            # Safety confirmation for bypassPermissions
            if permission_mode == "bypassPermissions":
                prompter.print("\n  ⚠️  WARNING: bypassPermissions allows the agent to:")
                prompter.print("     - Execute any shell commands without confirmation")
                prompter.print("     - Read/write any files without confirmation")
                prompter.print("     - Access network resources without confirmation")
                if not prompter.yes_no("Are you sure you want to bypass all permission prompts?", default=False):
                    permission_mode = "default"
                    prompter.print("  → Using 'default' mode instead")

        agent_config: dict[str, Any] = {
            "prompt": prompt_path,
            "model": model,
            "timeout_minutes": int(timeout),
        }

        if custom_command:
            agent_config["command"] = custom_command
        else:
            agent_config["permission_mode"] = permission_mode

        config["agents"][agent_name] = agent_config

        prompter.print(f"✓ Added {agent_name}\n")

    # Create agent labels on GitHub
    prompter.print("--- Agent Labels ---")
    if prompter.yes_no("Create agent labels on GitHub now?"):
        client = _github_adapter(str(config["repo"]))
        for agent_name in config["agents"].keys():
            try:
                client.create_label(
                    agent_name,
                    color="1D76DB",
                    description=f"Issues for {agent_name.split(':')[-1]} agent",
                    force=True,
                )
                prompter.print(f"  ✓ {agent_name}")
            except Exception:
                prompter.print(f"  ✗ {agent_name} (may already exist)")

    # Concurrency
    prompter.print("\n--- Concurrency Settings ---")
    max_sessions = prompter.input("Max concurrent agent sessions", "3")
    config["concurrency"] = {
        "max_concurrent_sessions": int(max_sessions),
    }

    # Milestone sorting
    prompter.print("\n--- Issue Prioritization ---")
    prompter.print("How should issues be sorted when multiple are available?\n")
    prompter.print("  due_date - By milestone due date (earliest first)")
    prompter.print("  number   - By milestone number (lowest first)")
    prompter.print("  pattern  - Extract number from milestone name (e.g., 'M13' → 13)")
    prompter.print("  name     - Alphabetically by milestone name\n")
    milestone_sort = prompter.input("Milestone sort strategy", "due_date")
    if milestone_sort not in ("due_date", "number", "pattern", "name"):
        prompter.print(f"  Invalid strategy '{milestone_sort}', using 'due_date'")
        milestone_sort = "due_date"
    config["milestone_sort"] = milestone_sort

    if milestone_sort == "pattern":
        prompter.print("\n  Enter a regex pattern with one capture group for the number.")
        prompter.print("  Examples:")
        prompter.print("    M(\\d+)       → matches 'M13' → 13")
        prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
        pattern = prompter.input("  Pattern", r"M(\d+)")
        config["milestone_sort_config"] = {"pattern": pattern}

    # Worktree location
    prompter.print("\n--- Worktree Location ---")
    prompter.print("Each issue gets its own git worktree for isolated work.")
    prompter.print("Examples:")
    prompter.print("  '../'           → sibling dirs (~/dev/myrepo-123)")
    prompter.print("  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)")
    worktree_base = prompter.input("Worktree base directory", "../")

    # Apply to all agents
    for agent_config in config["agents"].values():
        agent_config["worktree_base"] = worktree_base

    # If subdirectory, offer to add to .gitignore
    if not worktree_base.startswith(".."):
        worktree_dir = worktree_base.lstrip("./")
        gitignore_path = Path(".gitignore")
        needs_gitignore = True

        if gitignore_path.exists():
            gitignore_content = gitignore_path.read_text()
            if worktree_dir in gitignore_content:
                needs_gitignore = False
                prompter.print(f"  ✓ {worktree_dir} already in .gitignore")

        if needs_gitignore:
            if prompter.yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                with open(gitignore_path, "a") as f:
                    f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                prompter.print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # UI Mode
    prompter.print("\n--- UI Mode ---")
    prompter.print("How do you want to monitor agent sessions?\n")
    prompter.print("  web    - Browser dashboard at localhost (recommended)")
    prompter.print("           Best for most users. Visual overview of all agents.")
    prompter.print("  tmux   - Terminal multiplexer sessions")
    prompter.print("           For terminal power users. Requires tmux installed.")
    prompter.print("  iterm2 - Native iTerm2 tabs (macOS only)")
    prompter.print("           Each agent runs in its own iTerm2 tab.\n")
    ui_mode = prompter.input("UI mode", "web")
    if ui_mode not in ("web", "tmux", "iterm2"):
        prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
        ui_mode = "web"
    config["ui_mode"] = ui_mode
    if ui_mode == "web":
        port = prompter.input("Web dashboard port", "8080")
        config["web_port"] = int(port)

    # Labels - use defaults, can be customized in YAML later
    # Default labels: in-progress, blocked, needs-human
    prompter.print("\n--- Label Prefix (Optional) ---")
    prompter.print("Add a prefix to avoid conflicts with existing labels.")
    prompter.print("  Example: prefix 'bot' → 'bot:in-progress', 'bot:blocked', etc.\n")
    label_prefix = prompter.input("Label prefix (leave empty for none)", "")
    if label_prefix:
        config["labels"] = {"prefix": label_prefix}
        prompter.print(f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc.")

    # Two-stage review workflow (enabled by default)
    prompter.print("\n--- Review Workflow ---")
    prompter.print("Code review is RECOMMENDED to catch issues before merging:")
    prompter.print("  Stage 1: Per-PR code review (immediate, after each PR)")
    prompter.print("  Stage 2: Triage batch review (when N reviewed PRs accumulate)\n")

    # Stage 1: Per-PR Code Review (default enabled)
    if prompter.yes_no("Enable Stage 1: Per-PR code review?", default=True):
        prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
        code_review_agent = prompter.input("  Code review agent label", "agent:reviewer")
        code_review_label = prompter.input("  Label for PRs needing review", "needs-code-review")
        code_reviewed_label = prompter.input("  Label after review passes", "code-reviewed")

        config["code_review_agent"] = code_review_agent
        config["code_review_label"] = code_review_label
        config["code_reviewed_label"] = code_reviewed_label
        prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
        prompter.print(f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}")

        # Stage 2: Triage Batch Review (only if Stage 1 enabled)
        prompter.print("")
        if prompter.yes_no("Enable Stage 2: Triage batch review?", default=False):
            prompter.print("\n  --- Stage 2: Triage Batch Review ---")
            triage_review_agent = prompter.input("  triage review agent label", "agent:triage")
            triage_reviewed_label = prompter.input("  Label after triage review", "triage-reviewed")
            threshold = prompter.input("  Trigger after N code-reviewed PRs", "5")

            config["triage_review_agent"] = triage_review_agent
            config["triage_reviewed_label"] = triage_reviewed_label
            try:
                threshold_int = int(threshold)
                if threshold_int > 0:
                    config["triage_review_threshold"] = threshold_int
                    prompter.print(f"  ✓ triage review triggers after {threshold_int} PRs with '{code_reviewed_label}'")
            except ValueError:
                pass
            prompter.print(f"  ✓ Label flow: {code_reviewed_label} → {triage_reviewed_label}")

    return config


def wizard_existing_project(state: DetectedState, prompter: Prompter) -> tuple[dict[str, Any], Optional[Path]]:
    """Walk through existing project onboarding.

    Returns:
        Tuple of (config dict, existing_config_path if updating else None)
    """
    prompter.print("\n" + "=" * 50)
    prompter.print("EXISTING PROJECT ONBOARDING")
    prompter.print("=" * 50)

    # Show what we found
    prompter.print("\n--- Detected State ---")
    prompter.print(f"  Repo: {state.repo or 'Not detected'}")
    prompter.print(f"  Existing config: {state.config_path or 'None'}")
    prompter.print(f"  GitHub labels: {len(state.github_labels)} total")
    prompter.print(f"  Agent labels: {', '.join(state.agent_labels) or 'None'}")
    prompter.print(f"  Prompt candidates: {len(state.prompt_candidates)} files")

    # Start with existing config or fresh
    config: dict[str, Any]
    updating_existing_path: Optional[Path] = None
    if state.existing_config:
        prompter.print(f"\n✓ Found existing config at {state.config_path}")
        if prompter.yes_no("Update existing config?"):
            config = dict(state.existing_config)
            updating_existing_path = state.config_path
        else:
            config = {"agents": {}}
    else:
        config = {"agents": {}}

    # Ensure repo is set
    if "repo" not in config:
        config["repo"] = state.repo or prompter.input("GitHub repo (owner/name)")

    # Check for agent labels not in config
    configured_agents = set(config.get("agents", {}).keys())
    unconfigured_agents = [a for a in state.agent_labels if a not in configured_agents]

    if unconfigured_agents:
        prompter.print(f"\n--- Found {len(unconfigured_agents)} agent labels not in config ---")
        for agent_label in unconfigured_agents:
            prompter.print(f"\nAgent: {agent_label}")
            if prompter.yes_no(f"Add {agent_label} to config?"):
                # Suggest prompt files
                agent_short = agent_label.split(":")[-1]
                matching_prompts = [
                    p for p in state.prompt_candidates if agent_short.lower() in p.name.lower()
                ]

                if matching_prompts:
                    prompter.print("  Possible prompt files:")
                    for i, p in enumerate(matching_prompts[:5], 1):
                        prompter.print(f"    {i}. {p.relative_to(Path.cwd())}")
                    choice = prompter.input("Choose (number) or enter path", "1")
                    try:
                        idx = int(choice) - 1
                        prompt_path = str(matching_prompts[idx].relative_to(Path.cwd()))
                    except (ValueError, IndexError):
                        prompt_path = choice
                else:
                    prompt_path = prompter.input(
                        "Prompt file path",
                        f".issue-orchestrator/prompts/{agent_short}.md",
                    )

                timeout = prompter.input("Timeout (minutes)", "45")

                # Ask about custom command
                prompter.print("\n  Agent command options:")
                prompter.print("    claude  - Use Claude Code CLI (default)")
                prompter.print("    custom  - Use a custom command/script")
                agent_type = prompter.choice("Agent type", ["claude", "custom"])

                custom_command = None
                permission_mode = "default"

                if agent_type == "custom":
                    prompter.print("\n  Enter your custom command template. Available variables:")
                    prompter.print("    {issue_number}, {issue_title}, {prompt}, {worktree}, {model}")
                    custom_command = prompter.input("Custom command")
                    model = "sonnet"  # Not relevant for custom, but keep a default
                else:
                    model = prompter.choice("Model", ["sonnet", "opus", "haiku"])

                    # Permission mode for Claude CLI
                    prompter.print("\n  Permission mode controls how Claude handles tool permissions:")
                    prompter.print("    default          - Prompt for each action (safest)")
                    prompter.print("    acceptEdits      - Auto-accept file edits, prompt for others")
                    prompter.print("    bypassPermissions - Skip all prompts (use for trusted automation)")
                    permission_mode = prompter.choice(
                        "Permission mode",
                        ["default", "acceptEdits", "bypassPermissions"]
                    )

                    # Safety confirmation for bypassPermissions
                    if permission_mode == "bypassPermissions":
                        prompter.print("\n  ⚠️  WARNING: bypassPermissions allows the agent to:")
                        prompter.print("     - Execute any shell commands without confirmation")
                        prompter.print("     - Read/write any files without confirmation")
                        prompter.print("     - Access network resources without confirmation")
                        if not prompter.yes_no("Are you sure you want to bypass all permission prompts?", default=False):
                            permission_mode = "default"
                            prompter.print("  → Using 'default' mode instead")

                if "agents" not in config:
                    config["agents"] = {}

                agent_cfg: dict[str, Any] = {
                    "prompt": prompt_path,
                    "model": model,
                    "timeout_minutes": int(timeout),
                }

                if custom_command:
                    agent_cfg["command"] = custom_command
                else:
                    agent_cfg["permission_mode"] = permission_mode

                config["agents"][agent_label] = agent_cfg
                prompter.print(f"  ✓ Added {agent_label}")

    # Check for configured agents with missing labels on GitHub
    if configured_agents:
        missing_labels = [a for a in configured_agents if a not in state.github_labels]
        if missing_labels:
            prompter.print(f"\n⚠ These agents are in config but missing GitHub labels:")
            for label in missing_labels:
                prompter.print(f"    - {label}")
            if prompter.yes_no("Create missing labels on GitHub?"):
                repo = str(config["repo"])
                client = _github_adapter(repo)
                for label in missing_labels:
                    try:
                        client.create_label(
                            label,
                            color="1D76DB",
                            description=f"Issues for {label.split(':')[-1]} agent",
                            force=True,
                        )
                        prompter.print(f"  ✓ Created {label}")
                    except Exception:
                        prompter.print(f"  ✗ Failed to create {label}")

    # Ensure we have concurrency settings
    if "concurrency" not in config:
        prompter.print("\n--- Concurrency Settings ---")
        max_sessions = prompter.input("Max concurrent sessions", "3")
        config["concurrency"] = {"max_concurrent_sessions": int(max_sessions)}

    # Milestone sorting - only ask if not already configured
    if "milestone_sort" not in config:
        prompter.print("\n--- Issue Prioritization ---")
        prompter.print("How should issues be sorted when multiple are available?\n")
        prompter.print("  due_date - By milestone due date (earliest first)")
        prompter.print("  number   - By milestone number (lowest first)")
        prompter.print("  pattern  - Extract number from milestone name (e.g., 'M13' → 13)")
        prompter.print("  name     - Alphabetically by milestone name\n")
        milestone_sort = prompter.input("Milestone sort strategy", "due_date")
        if milestone_sort not in ("due_date", "number", "pattern", "name"):
            prompter.print(f"  Invalid strategy '{milestone_sort}', using 'due_date'")
            milestone_sort = "due_date"
        config["milestone_sort"] = milestone_sort

        if milestone_sort == "pattern":
            prompter.print("\n  Enter a regex pattern with one capture group for the number.")
            prompter.print("  Examples:")
            prompter.print("    M(\\d+)       → matches 'M13' → 13")
            prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
            pattern = prompter.input("  Pattern", r"M(\d+)")
            config["milestone_sort_config"] = {"pattern": pattern}

    # Check if agents need worktree_base
    agents_without_worktree = [
        name for name, cfg in config.get("agents", {}).items()
        if "worktree_base" not in cfg
    ]
    if agents_without_worktree:
        prompter.print("\n--- Worktree Location ---")
        prompter.print("Each issue gets its own git worktree for isolated work.")
        prompter.print("Examples:")
        prompter.print("  '../'           → sibling dirs (~/dev/myrepo-123)")
        prompter.print("  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)")
        worktree_base = prompter.input("Worktree base directory", "../")

        for agent_name in agents_without_worktree:
            config["agents"][agent_name]["worktree_base"] = worktree_base

        # If subdirectory, offer to add to .gitignore
        if not worktree_base.startswith(".."):
            worktree_dir = worktree_base.lstrip("./")
            gitignore_path = Path(".gitignore")
            needs_gitignore = True

            if gitignore_path.exists():
                gitignore_content = gitignore_path.read_text()
                if worktree_dir in gitignore_content:
                    needs_gitignore = False
                    prompter.print(f"  ✓ {worktree_dir} already in .gitignore")

            if needs_gitignore:
                if prompter.yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                    with open(gitignore_path, "a") as f:
                        f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                    prompter.print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # UI mode
    if "ui_mode" not in config:
        prompter.print("\n--- UI Mode ---")
        prompter.print("How do you want to monitor agent sessions?\n")
        prompter.print("  web    - Browser dashboard at localhost (recommended)")
        prompter.print("           Best for most users. Visual overview of all agents.")
        prompter.print("  tmux   - Terminal multiplexer sessions")
        prompter.print("           For terminal power users. Requires tmux installed.")
        prompter.print("  iterm2 - Native iTerm2 tabs (macOS only)")
        prompter.print("           Each agent runs in its own iTerm2 tab.\n")
        ui_mode = prompter.input("UI mode", "web")
        if ui_mode not in ("web", "tmux", "iterm2"):
            prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
            ui_mode = "web"
        config["ui_mode"] = ui_mode
        if ui_mode == "web":
            config["web_port"] = int(prompter.input("Web port", "8080"))

    # Label prefix (optional) - only ask if not already configured
    if "labels" not in config or "prefix" not in config.get("labels", {}):
        prompter.print("\n--- Label Prefix (Optional) ---")
        prompter.print("Add a prefix to avoid conflicts with existing labels.")
        prompter.print("  Example: prefix 'bot' → 'bot:in-progress', 'bot:blocked', etc.\n")
        label_prefix = prompter.input("Label prefix (leave empty for none)", "")
        if label_prefix:
            if "labels" not in config:
                config["labels"] = {}
            config["labels"]["prefix"] = label_prefix
            prompter.print(f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc.")

    # Two-stage review workflow - ask if not already configured (default enabled)
    if "code_review_agent" not in config:
        prompter.print("\n--- Review Workflow ---")
        prompter.print("Code review is RECOMMENDED to catch issues before merging:")
        prompter.print("  Stage 1: Per-PR code review (immediate, after each PR)")
        prompter.print("  Stage 2: Triage batch review (when N reviewed PRs accumulate)\n")

        # Stage 1: Per-PR Code Review (default enabled)
        if prompter.yes_no("Enable Stage 1: Per-PR code review?", default=True):
            prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
            code_review_agent = prompter.input("  Code review agent label", "agent:reviewer")
            code_review_label = prompter.input("  Label for PRs needing review", "needs-code-review")
            code_reviewed_label = prompter.input("  Label after review passes", "code-reviewed")

            config["code_review_agent"] = code_review_agent
            config["code_review_label"] = code_review_label
            config["code_reviewed_label"] = code_reviewed_label
            prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
            prompter.print(f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}")

            # Stage 2: Triage Batch Review (only if Stage 1 enabled)
            prompter.print("")
            if prompter.yes_no("Enable Stage 2: Triage batch review?", default=False):
                prompter.print("\n  --- Stage 2: Triage Batch Review ---")
                triage_review_agent = prompter.input("  triage review agent label", "agent:triage")
                triage_reviewed_label = prompter.input("  Label after triage review", "triage-reviewed")
                threshold = prompter.input("  Trigger after N code-reviewed PRs", "5")

                config["triage_review_agent"] = triage_review_agent
                config["triage_reviewed_label"] = triage_reviewed_label
                try:
                    threshold_int = int(threshold)
                    if threshold_int > 0:
                        config["triage_review_threshold"] = threshold_int
                        prompter.print(f"  ✓ triage review triggers after {threshold_int} PRs with '{code_reviewed_label}'")
                except ValueError:
                    pass
                prompter.print(f"  ✓ Label flow: {code_reviewed_label} → {triage_reviewed_label}")

    return config, updating_existing_path


def create_starter_prompt(agent_name: str, path: Path) -> None:
    """Create a starter prompt file for an agent."""
    agent_short = agent_name.split(":")[-1]
    content = f"""# {agent_short.title()} Agent Prompt

You are working on issue #{{issue_number}}: {{issue_title}}

## Your Role
You are the {agent_short} agent responsible for implementing changes in this area.

## Working Directory
Your worktree is at: {{worktree}}

## Instructions
1. Read the issue carefully and understand the requirements
2. Implement the necessary changes
3. Write tests if applicable
4. Run existing tests to ensure nothing is broken
5. When complete, use `agent-done` to create a PR

## Important
- Always use `agent-done` when finished (not `git push` directly)
- If blocked, use `agent-done --blocked "reason"`
- If you need human input, use `agent-done --needs-human "question"`
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_code_review_prompt(path: Path, code_review_label: str, code_reviewed_label: str) -> None:
    """Create a code review prompt with actual label values substituted.

    Template variables like {{pr_number}} become {pr_number} in output,
    which get_command() substitutes at runtime.
    """
    content = f"""# Code Review Agent

You are a code reviewer. Your job is to review PRs created by work agents, checking code quality, test coverage, and adherence to best practices.

## Your Task

You are reviewing PR #{{pr_number}} for issue #{{issue_number}}: {{issue_title}}

The PR has the `{code_review_label}` label and needs your review.

## Review Process

### 1. Fetch PR Details

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

### 4. Post Review Comments

If you find issues that need fixing:

```bash
gh pr review {{pr_number}} --request-changes --body "## Code Review

### Issues Found
- Issue 1: description
- Issue 2: description

### Suggestions
- Suggestion 1
- Suggestion 2

Please address these issues and push updates."
```

If the PR looks good:

```bash
gh pr review {{pr_number}} --approve --body "## Code Review

LGTM! The implementation looks good.

### What I Checked
- Code quality and style
- Test coverage
- Logic correctness

### Notes
- Any minor observations (optional)"
```

### 5. Update Labels

After approving, update the PR label:

```bash
gh pr edit {{pr_number}} --remove-label "{code_review_label}" --add-label "{code_reviewed_label}"
```

## Completion

When done reviewing:

```bash
agent-done completed \\
  --implementation "Reviewed PR #{{pr_number}}. [Approved/Requested changes]. [Summary of findings]" \\
  --problems "None" # or describe any concerns
```

## Review Principles

1. **Be constructive** - Explain why something should change, not just that it should
2. **Be specific** - Point to exact lines/files when possible
3. **Prioritize** - Distinguish blocking issues from nice-to-haves
4. **Be consistent** - Apply the same standards across all PRs
5. **Trust but verify** - Check that tests actually test the changes
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def create_triage_review_prompt(path: Path, review_label: str, reviewed_label: str) -> None:
    """Create a triage review prompt with actual label values substituted."""
    content = f"""# Triage Review Agent

You are a Triage/technical advisor **auditing** work done by AI agents.

**Important:** You do NOT approve PRs - that's for humans. Your job is to:
- Identify patterns across PRs (good and bad)
- Flag concerns for human review
- Suggest process improvements
- Create follow-up issues for recurring problems

## Review Mode

This prompt supports two modes based on the issue:

1. **Batch Review** (issue title contains "Batch Review" or "Triage Review"): Audit all PRs with `{review_label}` label
2. **Single Issue Review**: Audit the specific issue #{{issue_number}}

## Batch Review Process

### 1. Find PRs to Audit

```bash
gh pr list --label "{review_label}" --json number,title,body,url,headRefName
```

**If no PRs found:** Document this in your report and complete with "No PRs to review".

### 2. For Each PR, Investigate:

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

### 3. Comment on Each PR

```bash
gh pr comment <number> --body "## Triage Audit

### Assessment
{{status: Reviewed - no concerns / Flagged - minor concerns / Escalate - significant concerns}}

### What I Checked
- [ ] Code changes
- [ ] Test coverage
- [ ] Issue comments/context
- [ ] Patterns vs other PRs

### Feedback
{{specific constructive feedback, or 'No issues found'}}

### Good Practices Noted
{{what was done well - helps agents learn, or 'N/A'}}
"
```

### 4. Mark PR as Audited

After reviewing each PR, flip the label:
```bash
gh pr edit <number> --remove-label "{review_label}" --add-label "{reviewed_label}"
```

### 5. Create Investigation Log

Create a summary report as a comment on THIS issue. **Always document what you checked, even if nothing was found:**

```markdown
## Triage Audit Report

### Investigation Summary
- PRs checked: {{N}} (or "0 - no PRs with '{review_label}' label found")
- PRs flagged: {{N}} (or "0 - no concerns")
- Follow-up issues created: {{N}}

### PRs Audited
| PR | What I Checked | Status | Notes |
|----|----------------|--------|-------|
| #N | code, tests, comments | No concerns | Brief note |
| #N | code, tests | Flagged | Missing test coverage |
| (none) | - | - | No PRs with '{review_label}' label |

### Patterns Observed
- {{recurring issues across PRs, or "No patterns identified - insufficient sample size"}}
- {{common mistakes, or "None"}}
- {{good practices to encourage, or "None noted"}}

### Process Improvements
- {{suggestions for agent prompts, or "None needed"}}
- {{workflow improvements, or "None"}}
- {{tooling needs, or "None"}}

### Follow-up Actions Created
- Issue #X: {{description}}
- (none): No follow-up actions needed
```

### 6. Create Follow-up Issues (if needed)

For process improvements or recurring problems:
```bash
gh issue create --title "Process: {{improvement}}" --body "{{details}}" --label "process"
```

## Single Issue Review Process

When auditing a specific issue #{{issue_number}}: {{issue_title}}

### 1. Understand the Issue
```bash
gh issue view {{issue_number}} --comments
```

### 2. Find and Review the PR
Look for PR links in issue comments, then:
```bash
gh pr view <number> --json title,body,files
gh pr diff <number>
```

**If no PR found:** Document this and note the issue may still be in progress.

### 3. Post Audit Report
Comment on the issue with your analysis:

```markdown
## Triage Audit

### What I Checked
- [ ] Issue requirements
- [ ] PR code changes
- [ ] Test coverage
- [ ] Agent-reported problems

### Summary
{{brief assessment, or "No PR found for this issue"}}

### Problems Analysis
- Agent-reported problems: {{from "Problems Encountered" section, or "None reported"}}
- Additional concerns: {{anything you noticed, or "None"}}

### Recommendations
{{specific suggestions, or "None - implementation looks good"}}

### Status
- [ ] Reviewed - no concerns
- [ ] Flagged for human review: {{why}}
- [ ] Escalate: {{significant concerns}}
```

## Completion

When done, use `agent-done`:

```bash
agent-done completed \\
  --implementation "Audited {{N}} PRs. {{X}} no concerns, {{Y}} flagged for human review. Created {{M}} follow-up issues." \\
  --problems "{{any process issues found, or 'None'}}"
```

**If no PRs to review:**
```bash
agent-done completed \\
  --implementation "No PRs with '{review_label}' label found. Nothing to audit." \\
  --problems "None"
```

## Audit Principles

- **Be constructive** - agents are learning from your feedback
- **Focus on patterns** - individual issues matter less than systemic ones
- **Note what's good** - reinforcement helps improve agent behavior
- **Suggest prompt improvements** - if agents keep making the same mistake, the prompt needs work
- **Document everything** - always log what you checked, even if nothing was found
- **Flag, don't approve** - your job is to surface concerns, humans make final decisions
- **Don't block for style** - focus on correctness and maintainability
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that doesn't use aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def write_config(config: dict[str, Any], path: Path) -> None:
    """Write config to YAML file."""
    with open(path, "w") as f:
        yaml.dump(config, f, Dumper=_NoAliasDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


def run_wizard(target_path: Path | None = None, prompter: Prompter | None = None) -> None:
    """Main wizard entry point.

    Args:
        target_path: Directory to set up. If None, prompts user.
        prompter: Prompter for user interaction. If None, uses ConsolePrompter.
    """
    if prompter is None:
        prompter = ConsolePrompter()

    prompter.print("\n" + "=" * 50)
    prompter.print("  issue-orchestrator Setup Wizard")
    prompter.print("=" * 50)

    # Determine target directory
    if target_path is None:
        cwd = Path.cwd()
        prompter.print(f"\nCurrent directory: {cwd}")

        # Check if this looks like the issue-orchestrator package itself
        is_orchestrator_dir = (cwd / "src" / "issue_orchestrator").exists()
        if is_orchestrator_dir:
            prompter.print("⚠ This looks like the issue-orchestrator package directory.")
            prompter.print("  You probably want to set up a different project.\n")
            # Don't default to this directory
            target_input = prompter.input("Project directory to set up (required)", "")
            if not target_input:
                prompter.print("Error: Please specify a project directory.")
                sys.exit(1)
        else:
            target_input = prompter.input("Project directory to set up", str(cwd))

        target_path = Path(target_input).expanduser().resolve()

        if not target_path.exists():
            prompter.print(f"Error: {target_path} does not exist")
            sys.exit(1)
        if not target_path.is_dir():
            prompter.print(f"Error: {target_path} is not a directory")
            sys.exit(1)

    # Change to target directory for the rest of the wizard
    import os
    original_cwd = Path.cwd()
    os.chdir(target_path)
    prompter.print(f"\nSetting up: {target_path}\n")

    # Check prerequisites
    prompter.print("Checking prerequisites...")
    prereqs = check_prerequisites()

    all_ok = True
    for tool, ok in prereqs.items():
        status = "✓" if ok else "✗"
        prompter.print(f"  {status} {tool}")
        if not ok:
            all_ok = False

    if not all_ok:
        prompter.print("\n⚠ Some prerequisites are missing. Install them before continuing.")
        if not prereqs["github_auth"]:
            prompter.print("  Set a GitHub token (GITHUB_TOKEN or config)")
        if not prereqs["claude"]:
            prompter.print("  Install Claude Code CLI")
        if not prompter.yes_no("Continue anyway?", default=False):
            sys.exit(1)

    # Choose mode
    prompter.print("\n" + "-" * 50)
    mode = prompter.choice(
        "What would you like to do?",
        [
            "New project - set up from scratch",
            "Existing project - I have labels/issues already",
        ],
    )

    existing_config_path: Optional[Path] = None
    if "New" in mode:
        config = wizard_new_project(prompter)
    else:
        state = scan_existing_repo()
        config, existing_config_path = wizard_existing_project(state, prompter)

    # Add cleanup config with defaults (don't prompt - users can edit later)
    if "cleanup" not in config:
        # Include section based on their review workflow
        if config.get("triage_review_agent"):
            # Triage workflow - cleanup happens after triage review
            config["cleanup"] = {
                "with_cto": {
                    "close_ai_session_tabs": True,
                    "remove_worktrees": False,
                }
            }
        elif config.get("code_review_agent"):
            # Code review only - cleanup after code review
            config["cleanup"] = {
                "without_cto": {
                    "wait_for_code_review": True,
                    "close_ai_session_tabs": True,
                    "remove_worktrees": False,
                }
            }
        else:
            # No review workflow - cleanup on completion
            config["cleanup"] = {
                "without_cto": {
                    "wait_for_code_review": False,
                    "close_ai_session_tabs": True,
                    "remove_worktrees": False,
                }
            }

    # Review config
    prompter.print("\n" + "=" * 50)
    prompter.print("CONFIGURATION SUMMARY")
    prompter.print("=" * 50)
    prompter.print(yaml.dump(config, default_flow_style=False, sort_keys=False))

    # Save configuration - simple confirmation after reviewing summary
    if not prompter.yes_no("\nSave changes?"):
        prompter.print("Aborted.")
        sys.exit(0)

    if existing_config_path:
        # Updating existing config - save to same path
        output_path = existing_config_path
        write_config(config, output_path)
        prompter.print(f"✓ Updated {output_path}")
    else:
        # New config - ask for filename
        default_path = ".issue-orchestrator.yaml"
        output_path = Path(prompter.input(f"Config filename ({target_path}/)", default_path))

        # Check for existing file
        if output_path.exists():
            if not prompter.yes_no(f"{output_path} exists. Overwrite?"):
                prompter.print("Aborted.")
                sys.exit(0)

        write_config(config, output_path)
        prompter.print(f"✓ Saved {output_path}")

    # Create missing prompt files
    prompter.print("\n--- Prompt Files ---")

    # Get review config (new two-stage fields)
    code_review_agent = config.get("code_review_agent")
    code_review_label = config.get("code_review_label", "needs-code-review")
    code_reviewed_label = config.get("code_reviewed_label", "code-reviewed")
    triage_review_agent = config.get("triage_review_agent")
    triage_reviewed_label = config.get("triage_reviewed_label", "triage-reviewed")

    # Track all prompt files for the next steps summary
    all_prompt_paths: list[Path] = []

    for agent_name, agent_config in config.get("agents", {}).items():
        prompt_path = Path(agent_config["prompt"])
        all_prompt_paths.append(prompt_path)

        if not prompt_path.exists():
            # Check if this is the code review agent
            is_code_review_agent = (
                code_review_agent and agent_name == code_review_agent
            ) or (agent_name.lower() == "agent:reviewer")

            # Check if this is the triage review agent
            is_triage_review_agent = (
                triage_review_agent and agent_name == triage_review_agent
            ) or "triage" in agent_name.lower()

            if is_code_review_agent and code_review_agent:
                prompter.print(f"\n  Code review agent reviews each PR for quality, tests, and issues.")
                if prompter.yes_no(f"  Create code review prompt at {prompt_path}?"):
                    create_code_review_prompt(prompt_path, code_review_label, code_reviewed_label)
                    prompter.print(f"    ✓ Created - labels: {code_review_label} → {code_reviewed_label}")
            elif is_triage_review_agent and triage_review_agent:
                prompter.print(f"\n  Triage agent audits PRs in batch, identifies patterns, flags concerns for humans.")
                if prompter.yes_no(f"  Create triage audit prompt at {prompt_path}?"):
                    create_triage_review_prompt(prompt_path, code_reviewed_label, triage_reviewed_label)
                    prompter.print(f"    ✓ Created - labels: {code_reviewed_label} → {triage_reviewed_label}")
            else:
                prompter.print(f"\n  Work agent implements issues and creates PRs.")
                if prompter.yes_no(f"  Create starter prompt at {prompt_path}?"):
                    create_starter_prompt(agent_name, prompt_path)
                    prompter.print(f"    ✓ Created {prompt_path}")

    # Create priority and status labels (agent labels handled earlier)
    repo = config.get("repo")
    if repo:
        # Gather all labels we want to ensure exist
        labels_config = config.get("labels", {})
        label_prefix = labels_config.get("prefix", "")

        def prefixed(label: str) -> str:
            """Apply label prefix if configured."""
            return f"{label_prefix}:{label}" if label_prefix else label

        priority_labels = [
            ("priority:high", "D93F0B", "Urgent - do first"),
            ("priority:medium", "FBCA04", "Normal priority"),
            ("priority:low", "0E8A16", "Nice to have"),
        ]
        status_labels = [
            (prefixed(labels_config.get("in_progress", "in-progress")), "5319E7", "Agent is working on this"),
            (prefixed(labels_config.get("blocked", "blocked")), "B60205", "Agent is blocked"),
            (prefixed(labels_config.get("needs_human", "needs-human")), "FBCA04", "Agent needs human input"),
        ]
        all_labels = priority_labels + status_labels

        # Add review labels if configured (two-stage review workflow)
        if code_review_agent:
            all_labels.extend([
                (code_review_label, "7057FF", "PR needs code review"),
                (code_reviewed_label, "0E8A16", "PR has been code reviewed"),
            ])
        if triage_review_agent:
            all_labels.append(
                (triage_reviewed_label, "1D76DB", "PR has been triage reviewed")
            )

        # Check which labels already exist
        existing_labels = set(fetch_github_labels(repo))
        missing_labels = [(name, color, desc) for name, color, desc in all_labels if name not in existing_labels]

        if not missing_labels:
            prompter.print("\n--- GitHub Labels ---")
            prompter.print("  ✓ All required labels already exist on GitHub")
        else:
            prompter.print("\n--- GitHub Labels ---")
            prompter.print("The following labels are missing from GitHub:")
            for name, _, desc in missing_labels:
                prompter.print(f"  • {name} - {desc}")

            if prompter.yes_no(f"\nCreate these {len(missing_labels)} labels on GitHub?"):
                client = _github_adapter(repo)
                for name, color, desc in missing_labels:
                    client.create_label(
                        name,
                        color=color,
                        description=desc,
                        force=True,
                    )
                prompter.print("  ✓ Labels created")


    prompter.print("\n" + "=" * 50)
    prompter.print("Setup complete! Next steps:")
    prompter.print("=" * 50)

    # List prompt files to review
    prompter.print("\n  1. Review/edit your prompt files:")
    for prompt_path in all_prompt_paths:
        prompter.print(f"     • {prompt_path}")

    # List agent labels to add to issues
    agent_labels = list(config.get("agents", {}).keys())
    # Exclude review agents from the list (they work on PRs, not issues)
    work_agent_labels = [
        label for label in agent_labels
        if label != code_review_agent and label != triage_review_agent
    ]
    prompter.print("\n  2. Add agent labels to your GitHub issues:")
    for label in work_agent_labels:
        prompter.print(f"     • {label}")

    prompter.print("\n  3. Run: issue-orchestrator start")
    prompter.print("")


if __name__ == "__main__":
    run_wizard()
