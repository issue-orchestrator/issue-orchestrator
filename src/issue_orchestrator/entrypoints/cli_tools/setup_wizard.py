"""Interactive setup wizard for issue-orchestrator."""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, cast

import yaml

# Import provider registry for agent type selection
from issue_orchestrator.agent_runner import list_providers, get_provider

# Schema metadata for defaults/labels/hints
from ...infra.settings_schema import get_setup_fields


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

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
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

    def choice(
        self, question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
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


@dataclass
class PlannedWrite:
    """A file write that would be performed."""

    path: Path
    content: str
    action: str  # "create", "overwrite", or "append"

    def size_display(self) -> str:
        """Return human-readable size."""
        size = len(self.content.encode("utf-8"))
        if size < 1024:
            return f"{size} B"
        return f"{size / 1024:.1f} KB"


class FileCollector:
    """Collects planned file writes for dry-run mode."""

    def __init__(self) -> None:
        self.writes: list[PlannedWrite] = []
        self.labels: list[tuple[str, str, str]] = []  # (name, color, description)

    def add_write(self, path: Path, content: str, action: str = "create") -> None:
        """Record a planned file write."""
        self.writes.append(PlannedWrite(path, content, action))

    def add_label(self, name: str, color: str, description: str) -> None:
        """Record a planned GitHub label creation."""
        self.labels.append((name, color, description))


def _get_repository_host(repo: str):
    """Get a RepositoryHost for the given repo.

    All GitHub access in setup wizard is routed through the repository host for
    consistent auditing and rate-limit handling.
    """
    from ...execution.providers import create_repository_host

    return create_repository_host(repo=repo)


def run_git(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run git command, return (success, output)."""
    from ...execution.git_tools import run_git as run_git_impl

    return run_git_impl(args, cwd)


def check_prerequisites() -> dict[str, bool]:
    """Check if required tools are installed."""
    checks = {}

    # git
    ok, _ = run_git(["--version"])
    checks["git"] = ok

    # GitHub token
    try:
        from ...execution.providers import resolve_github_token

        resolve_github_token(configured_token=None, configured_env=None)
        checks["github_auth"] = True
    except Exception:
        checks["github_auth"] = False

    # Check AI providers from registry
    providers = list_providers()
    any_provider_available = False
    for name in providers:
        provider = get_provider(name)
        is_available = provider.is_available()
        checks[f"provider:{name}"] = is_available
        if is_available:
            any_provider_available = True

    # At least one provider should be available
    checks["any_ai_provider"] = any_provider_available

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
        labels = _get_repository_host(repo).list_labels()
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


def find_existing_config(
    start_path: Path | None = None,
) -> tuple[Path | None, dict | None]:
    """Find existing config file.

    Looks for configs in .issue-orchestrator/config/ only.
    """
    from ...infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME

    if start_path is None:
        start_path = Path.cwd()

    # Only look in the canonical config location
    candidates = [
        f"{CONFIG_DIR}/{DEFAULT_CONFIG_NAME}",  # .issue-orchestrator/config/default.yaml
        f"{CONFIG_DIR}/*.yaml",  # Any yaml in config dir (glob)
    ]

    current = start_path
    while current != current.parent:
        for candidate in candidates:
            if "*" in candidate:
                # Glob pattern
                matches = list(current.glob(candidate))
                if matches:
                    config_path = matches[0]  # Take first match
                    try:
                        with open(config_path) as f:
                            return config_path, yaml.safe_load(f)
                    except yaml.YAMLError:
                        pass
            else:
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
        ".prompts/**/*.md",
        "**/prompts/*.md",
        ".issue-orchestrator/prompts/**/*.md",  # Legacy location
        "**/*orchestrator*.md",
        "**/*-agent*.md",
        "**/*_agent*.md",
    ]

    # Check high-priority first
    for pattern in high_priority_patterns:
        for path in start_path.glob(pattern):
            if path.is_file() and path not in candidates:
                # Skip node_modules, .git, etc.
                if any(
                    part.startswith(".") or part == "node_modules"
                    for part in path.parts
                ):
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


def wizard_new_project(prompter: Prompter) -> dict[str, Any]:  # noqa: C901, PLR0912 - interactive wizard with branches for each config option
    """Walk through new project setup."""
    config: dict[str, Any] = {"agents": {}}

    prompter.print("\n" + "=" * 50)
    prompter.print("NEW PROJECT SETUP")
    prompter.print("=" * 50)

    setup_mode = prompter.choice(
        "Setup depth",
        [
            "Quick setup (recommended)",
            "Advanced setup",
        ],
    )
    advanced = "Advanced" in setup_mode

    # Repo
    detected_repo = detect_repo()
    if detected_repo:
        repo_name = prompter.input("GitHub repo", detected_repo)
    else:
        repo_name = prompter.input("GitHub repo (owner/name)")
    config["repo"] = {"name": repo_name}

    # Agents
    prompter.print("\n--- Agent Configuration ---")
    prompter.print("Agents are identified by GitHub labels (e.g., 'agent:backend').")
    prompter.print("Each agent needs a prompt file with instructions.\n")

    while True:
        agent_name = prompter.input(
            "Agent label (e.g., 'agent:backend', or empty to finish)"
        )
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
            f".prompts/{agent_name.split(':')[-1]}.md",
        )

        timeout = prompter.input("Timeout in minutes", "45")

        # Ask about agent provider
        providers = list_providers()
        available_providers = [p for p in providers if get_provider(p).is_available()]
        agent_type: str
        if not advanced and len(available_providers) == 1:
            agent_type = available_providers[0]
            prompter.print(f"\n  Using available provider: {agent_type}")
        else:
            prompter.print("\n  Agent provider options:")
            for p in providers:
                provider_obj = get_provider(p)
                available = "✓" if provider_obj.is_available() else "✗ not installed"
                prompter.print(f"    {p}  - {provider_obj.description} ({available})")
            prompter.print("    custom  - Use a custom command/script")

            provider_choices = providers + ["custom"]
            agent_type = prompter.choice("Agent provider", provider_choices)

        custom_command = None
        permission_mode = "default"
        selected_provider = None

        if agent_type == "custom":
            prompter.print(
                "\n  Enter your custom command template. Available variables:"
            )
            prompter.print(
                "    {issue_number}, {issue_title}, {prompt}, {worktree}, {model}"
            )
            custom_command = prompter.input("Custom command")
            model = "sonnet"  # Not relevant for custom, but keep a default
        else:
            selected_provider = agent_type
            # Model selection depends on provider
            if selected_provider == "claude-code":
                model = prompter.choice(
                    "Model for this agent", ["sonnet", "opus", "haiku"]
                )

                # Permission mode for Claude CLI
                prompter.print(
                    "\n  Permission mode controls how Claude handles tool permissions:"
                )
                prompter.print("    default          - Prompt for each action (safest)")
                prompter.print(
                    "    acceptEdits      - Auto-accept file edits, prompt for others"
                )
                prompter.print(
                    "    bypassPermissions - Skip all prompts (use for trusted automation)"
                )
                permission_mode = prompter.choice(
                    "Permission mode", ["default", "acceptEdits", "bypassPermissions"]
                )

                # Safety confirmation for bypassPermissions
                if permission_mode == "bypassPermissions":
                    prompter.print(
                        "\n  ⚠️  WARNING: bypassPermissions allows the agent to:"
                    )
                    prompter.print(
                        "     - Execute any shell commands without confirmation"
                    )
                    prompter.print("     - Read/write any files without confirmation")
                    prompter.print(
                        "     - Access network resources without confirmation"
                    )
                    if not prompter.yes_no(
                        "Are you sure you want to bypass all permission prompts?",
                        default=False,
                    ):
                        permission_mode = "default"
                        prompter.print("  → Using 'default' mode instead")
            elif selected_provider == "codex":
                model = prompter.input("Model for Codex", "o3")
            else:
                model = prompter.input("Model name", "default")

        # Ask if this agent does code reviews (affects initial_prompt template)
        prompter.print("\n  Does this agent do code reviews?")
        prompter.print("    Review agents need {pr_number} in their prompt template.")
        is_review_agent = prompter.yes_no("Is this a review agent?", default=False)

        # Generate appropriate initial_prompt based on agent type
        if is_review_agent:
            initial_prompt = (
                "Review PR #{pr_number} for issue #{issue_number}: {issue_title}. "
                "Follow the instructions in {prompt}. When done, use reviewer-done to report your verdict."
            )
        else:
            initial_prompt = (
                "Work on issue #{issue_number}: {issue_title}. "
                "Follow the instructions in {prompt}. When done, use coding-done to report completion."
            )

        agent_config: dict[str, Any] = {
            "prompt": prompt_path,
            "model": model,
            "timeout_minutes": int(timeout),
            "initial_prompt": initial_prompt,
        }

        if custom_command:
            agent_config["command"] = custom_command
        elif selected_provider:
            agent_config["provider"] = selected_provider
            # Add provider-specific args
            if selected_provider == "claude-code" and permission_mode != "default":
                agent_config["provider_args"] = {"permission_mode": permission_mode}

        config["agents"][agent_name] = agent_config

        prompter.print(f"✓ Added {agent_name}\n")

    # Concurrency — schema-driven
    prompter.print("\n--- Concurrency Settings ---")
    concurrency_values: dict[str, Any] = {}
    for field in get_setup_fields("concurrency"):
        raw = prompter.input(field["prompt"], str(field["default"]))
        concurrency_values[field["name"]] = int(raw)
    config["execution"] = {
        "concurrency": concurrency_values,
    }

    # Issue scheduling / milestones
    if advanced:
        prompter.print("\n--- Issue Scheduling ---")
        prompter.print("Issues are scheduled using a multi-level sort:\n")
        prompter.print(
            "  1. Dependencies  - Issues with 'blocked by #N' wait for #N to close"
        )
        prompter.print("  2. Milestone     - Configurable strategy (see below)")
        prompter.print(
            "  3. Priority tier - From issue title: [P0-x] < [P1-x] < ... < [P9-x]"
        )
        prompter.print("  4. Sequence      - The number after Px: [P1-001] < [P1-002]")
        prompter.print("  5. Issue number  - Tie-breaker (lower first)\n")
        prompter.print("Milestone sort strategies:")
        prompter.print("  due_date         - By milestone due date (earliest first)")
        prompter.print("  milestone_number - Extract number from name (M1 < M2 < M10)")
        prompter.print("  pattern          - Custom regex to extract number")
        prompter.print("  name             - Alphabetically by milestone name\n")
        valid_strategies = ("due_date", "milestone_number", "pattern", "name")
        while True:
            milestone_sort = prompter.input("Milestone sort strategy", "due_date")
            if milestone_sort in valid_strategies:
                break
            prompter.print(
                f"  Invalid strategy '{milestone_sort}'. Please choose from: {', '.join(valid_strategies)}"
            )
        milestones_config: dict[str, Any] = {"sort": milestone_sort}
        config["milestones"] = milestones_config

        if milestone_sort == "pattern":
            prompter.print(
                "\n  Enter a regex pattern with one capture group for the number."
            )
            prompter.print("  Examples:")
            prompter.print("    M(\\d+)       → matches 'M13' → 13")
            prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
            pattern = prompter.input("  Pattern", r"M(\d+)")
            milestones_config["sort_config"] = {"pattern": pattern}

        order_raw = prompter.input(
            "Optional milestone order (comma-separated, blank for none)", ""
        )
        if order_raw.strip():
            milestones_config["order"] = [
                m.strip() for m in order_raw.split(",") if m.strip()
            ]

        # Foundation milestone for dependency scope
        prompter.print(
            "\n  Dependencies must be within the same milestone OR in the foundation milestone."
        )
        prompter.print(
            "  Example: Issues in M2 can depend on M2 issues or M0 (foundation) issues."
        )
        foundation_milestone = prompter.input("Foundation milestone", "M0")
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["foundation"] = foundation_milestone
    else:
        config["milestones"] = {"sort": "due_date", "foundation": "M0"}

    # Worktree location — schema-driven
    prompter.print("\n--- Worktree Location ---")
    prompter.print("Each issue gets its own git worktree for isolated work.")
    prompter.print("Examples:")
    prompter.print("  '../'           → sibling dirs (~/dev/myrepo-123)")
    prompter.print(
        "  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)"
    )
    _wt_fields = get_setup_fields("worktrees")
    _wt_field = (
        _wt_fields[0]
        if _wt_fields
        else {"prompt": "Worktree Base Directory", "default": "../"}
    )
    worktree_base = prompter.input(_wt_field["prompt"], str(_wt_field["default"]))

    # Set at top-level (not per-agent)
    worktrees_config: dict[str, Any] = {"base": worktree_base}
    config["worktrees"] = worktrees_config

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

    # Worktree setup commands
    prompter.print("\n--- Worktree Setup Commands ---")
    prompter.print(
        "Commands to run in each new worktree after creation (e.g., install deps)."
    )
    prompter.print("Examples:")
    prompter.print("  npm install")
    prompter.print("  pip install -e '.[dev]'")
    prompter.print("  make setup")
    setup_input = prompter.input(
        "Setup commands (comma-separated, or empty to skip)", ""
    )
    if setup_input.strip():
        setup_cmds = [cmd.strip() for cmd in setup_input.split(",") if cmd.strip()]
        worktrees_config["setup"] = setup_cmds

    # UI Mode
    prompter.print("\n--- UI Mode ---")
    prompter.print("How do you want to monitor agent sessions?\n")
    prompter.print("  web    - Browser dashboard (recommended)")
    prompter.print(
        "           Best for most users. Opens in your browser or forwarded client URL."
    )
    prompter.print("  tmux   - Terminal multiplexer sessions")
    prompter.print("           For terminal power users. Requires tmux installed.\n")
    ui_mode = prompter.input("UI mode", "web")
    if ui_mode not in ("web", "tmux"):
        prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
        ui_mode = "web"
    ui_config: dict[str, Any] = {"mode": ui_mode}
    config["ui"] = ui_config
    if ui_mode == "web":
        # Schema-driven UI fields (web_port)
        for field in get_setup_fields("ui"):
            cond = field.get("condition")
            if cond and cond.get("field") == "ui_mode" and cond.get("value") != ui_mode:
                continue
            raw = prompter.input(field["prompt"], str(field["default"]))
            ui_config[field["name"]] = int(raw)
        prompter.print("\n--- Terminal Backend (web mode) ---")
        prompter.print("Choose how agent sessions are executed:\n")
        prompter.print("  tmux       - Default (stable, interactive)")
        prompter.print(
            "  subprocess - No tmux dependency; records raw terminal output in "
            ".issue-orchestrator/sessions/<run>/terminal-recording.jsonl\n"
        )
        terminal_backend = prompter.input("Terminal backend", "tmux")
        if terminal_backend not in ("tmux", "subprocess"):
            prompter.print(f"  Invalid backend '{terminal_backend}', using 'tmux'")
            terminal_backend = "tmux"
        if terminal_backend == "subprocess":
            execution_config = cast(dict[str, Any], config.setdefault("execution", {}))
            execution_config["terminal_adapter"] = "subprocess"

    # Labels - use defaults, can be customized in YAML later
    # Default labels: in-progress, blocked, needs-human
    prompter.print("\n--- Label Prefix ---")
    prompter.print("Prefix for status labels to avoid conflicts with existing labels.")
    prompter.print("  Example: 'io' → 'io:in-progress', 'io:blocked', etc.\n")
    label_prefix = prompter.input("Label prefix (optional)", "")
    if label_prefix:
        config["labels"] = {"prefix": label_prefix}
        prompter.print(
            f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc."
        )

    # Validation
    prompter.print("\n--- Validation ---")
    prompter.print("Validation runs on coding-done and pre-push.")
    validation_cmd = prompter.input("Validation command (optional)", "make test")
    if validation_cmd.strip():
        timeout = prompter.input("Validation timeout (seconds)", "300")
        config["validation"] = {
            "cmd": validation_cmd.strip(),
            "timeout_seconds": int(timeout),
        }

    # Filtering (optional)
    prompter.print("\n--- Filtering (Optional) ---")
    label_filter = prompter.input("Only process issues with label (optional)", "")
    if label_filter.strip():
        config["filtering"] = {"label": label_filter.strip()}

    # Review workflow
    prompter.print("\n--- Review Workflow ---")
    prompter.print("Optional automated review for PRs created by agents.\n")
    enable_review = prompter.yes_no("Enable code review?", default=False)
    if enable_review:
        prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
        code_review_agent = prompter.input(
            "  Code review agent label", "agent:reviewer"
        )
        code_review_label = prompter.input(
            "  Label for PRs needing review", "needs-code-review"
        )
        code_reviewed_label = prompter.input(
            "  Label after review passes", "code-reviewed"
        )

        # Use new review structure with enabled flag
        config["review"] = {
            "enabled": True,
            "default": code_review_agent,
            "code_review_label": code_review_label,
            "code_reviewed_label": code_reviewed_label,
        }
        prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
        prompter.print(f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}")

        # Stage 2: Triage Batch Review (advanced only)
        if advanced:
            prompter.print("")
            if prompter.yes_no("Enable Stage 2: Triage batch review?", default=False):
                prompter.print("\n  --- Stage 2: Triage Batch Review ---")
                triage_review_agent = prompter.input(
                    "  triage review agent label", "agent:triage"
                )
                triage_reviewed_label = prompter.input(
                    "  Label after triage review", "triage-reviewed"
                )
                threshold = prompter.input("  Trigger after N code-reviewed PRs", "5")

                config["review"]["triage_review_agent"] = triage_review_agent
                config["review"]["triage_reviewed_label"] = triage_reviewed_label
                try:
                    threshold_int = int(threshold)
                    if threshold_int > 0:
                        config["review"]["triage_review_threshold"] = threshold_int
                        prompter.print(
                            f"  ✓ triage review triggers after {threshold_int} PRs with '{code_reviewed_label}'"
                        )
                except ValueError:
                    pass
                prompter.print(
                    f"  ✓ Label flow: {code_reviewed_label} → {triage_reviewed_label}"
                )

    return config


def wizard_existing_project(  # noqa: C901, PLR0912 - interactive wizard with branches for detected state and config options
    state: DetectedState, prompter: Prompter
) -> tuple[dict[str, Any], Optional[Path]]:
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
    repo_value = state.repo or prompter.input("GitHub repo (owner/name)")
    if "repo" not in config or not isinstance(config.get("repo"), dict):
        config["repo"] = {"name": repo_value}
    elif "name" not in config["repo"]:
        config["repo"]["name"] = repo_value

    # Check for agent labels not in config
    configured_agents = set(config.get("agents", {}).keys())
    unconfigured_agents = [a for a in state.agent_labels if a not in configured_agents]

    if unconfigured_agents:
        prompter.print(
            f"\n--- Found {len(unconfigured_agents)} agent labels not in config ---"
        )
        for agent_label in unconfigured_agents:
            prompter.print(f"\nAgent: {agent_label}")
            if prompter.yes_no(f"Add {agent_label} to config?"):
                # Suggest prompt files
                agent_short = agent_label.split(":")[-1]
                matching_prompts = [
                    p
                    for p in state.prompt_candidates
                    if agent_short.lower() in p.name.lower()
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
                        f".prompts/{agent_short}.md",
                    )

                timeout = prompter.input("Timeout (minutes)", "45")

                # Ask about agent provider
                providers = list_providers()
                prompter.print("\n  Agent provider options:")
                for p in providers:
                    provider_obj = get_provider(p)
                    available = (
                        "✓" if provider_obj.is_available() else "✗ not installed"
                    )
                    prompter.print(
                        f"    {p}  - {provider_obj.description} ({available})"
                    )
                prompter.print("    custom  - Use a custom command/script")

                provider_choices = providers + ["custom"]
                agent_type = prompter.choice("Agent provider", provider_choices)

                custom_command = None
                permission_mode = "default"
                selected_provider = None

                if agent_type == "custom":
                    prompter.print(
                        "\n  Enter your custom command template. Available variables:"
                    )
                    prompter.print(
                        "    {issue_number}, {issue_title}, {prompt}, {worktree}, {model}"
                    )
                    custom_command = prompter.input("Custom command")
                    model = "sonnet"  # Not relevant for custom, but keep a default
                else:
                    selected_provider = agent_type
                    # Model selection depends on provider
                    if selected_provider == "claude-code":
                        model = prompter.choice("Model", ["sonnet", "opus", "haiku"])

                        # Permission mode for Claude CLI
                        prompter.print(
                            "\n  Permission mode controls how Claude handles tool permissions:"
                        )
                        prompter.print(
                            "    default          - Prompt for each action (safest)"
                        )
                        prompter.print(
                            "    acceptEdits      - Auto-accept file edits, prompt for others"
                        )
                        prompter.print(
                            "    bypassPermissions - Skip all prompts (use for trusted automation)"
                        )
                        permission_mode = prompter.choice(
                            "Permission mode",
                            ["default", "acceptEdits", "bypassPermissions"],
                        )

                        # Safety confirmation for bypassPermissions
                        if permission_mode == "bypassPermissions":
                            prompter.print(
                                "\n  ⚠️  WARNING: bypassPermissions allows the agent to:"
                            )
                            prompter.print(
                                "     - Execute any shell commands without confirmation"
                            )
                            prompter.print(
                                "     - Read/write any files without confirmation"
                            )
                            prompter.print(
                                "     - Access network resources without confirmation"
                            )
                            if not prompter.yes_no(
                                "Are you sure you want to bypass all permission prompts?",
                                default=False,
                            ):
                                permission_mode = "default"
                                prompter.print("  → Using 'default' mode instead")
                    elif selected_provider == "codex":
                        model = prompter.input("Model for Codex", "o3")
                    else:
                        model = prompter.input("Model name", "default")

                # Ask if this agent does code reviews (affects initial_prompt template)
                prompter.print("\n  Does this agent do code reviews?")
                prompter.print(
                    "    Review agents need {pr_number} in their prompt template."
                )
                is_review_agent = prompter.yes_no(
                    "Is this a review agent?", default=False
                )

                # Generate appropriate initial_prompt based on agent type
                if is_review_agent:
                    initial_prompt = (
                        "Review PR #{pr_number} for issue #{issue_number}: {issue_title}. "
                        "Follow the instructions in {prompt}. When done, use reviewer-done to report your verdict."
                    )
                else:
                    initial_prompt = (
                        "Work on issue #{issue_number}: {issue_title}. "
                        "Follow the instructions in {prompt}. When done, use coding-done to report completion."
                    )

                if "agents" not in config:
                    config["agents"] = {}

                agent_cfg: dict[str, Any] = {
                    "prompt": prompt_path,
                    "model": model,
                    "timeout_minutes": int(timeout),
                    "initial_prompt": initial_prompt,
                }

                if custom_command:
                    agent_cfg["command"] = custom_command
                elif selected_provider:
                    agent_cfg["provider"] = selected_provider
                    # Add provider-specific args
                    if (
                        selected_provider == "claude-code"
                        and permission_mode != "default"
                    ):
                        agent_cfg["provider_args"] = {
                            "permission_mode": permission_mode
                        }

                config["agents"][agent_label] = agent_cfg
                prompter.print(f"  ✓ Added {agent_label}")

    # Ensure we have concurrency settings
    if "execution" not in config or "concurrency" not in config.get("execution", {}):
        prompter.print("\n--- Concurrency Settings ---")
        max_sessions = prompter.input("Max concurrent sessions", "3")
        config.setdefault("execution", {})
        config["execution"]["concurrency"] = {
            "max_concurrent_sessions": int(max_sessions)
        }

    # Milestone sorting - only ask if not already configured
    if "milestones" not in config or "sort" not in config.get("milestones", {}):
        prompter.print("\n--- Issue Scheduling ---")
        prompter.print("Issues are scheduled using a multi-level sort:\n")
        prompter.print(
            "  1. Dependencies  - Issues with 'blocked by #N' wait for #N to close"
        )
        prompter.print("  2. Milestone     - Configurable strategy (see below)")
        prompter.print(
            "  3. Priority tier - From issue title: [P0-x] < [P1-x] < ... < [P9-x]"
        )
        prompter.print("  4. Sequence      - The number after Px: [P1-001] < [P1-002]")
        prompter.print("  5. Issue number  - Tie-breaker (lower first)\n")
        prompter.print("Milestone sort strategies:")
        prompter.print("  due_date         - By milestone due date (earliest first)")
        prompter.print("  milestone_number - Extract number from name (M1 < M2 < M10)")
        prompter.print("  pattern          - Custom regex to extract number")
        prompter.print("  name             - Alphabetically by milestone name\n")
        valid_strategies = ("due_date", "milestone_number", "pattern", "name")
        while True:
            milestone_sort = prompter.input("Milestone sort strategy", "due_date")
            if milestone_sort in valid_strategies:
                break
            prompter.print(
                f"  Invalid strategy '{milestone_sort}'. Please choose from: {', '.join(valid_strategies)}"
            )
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["sort"] = milestone_sort

        if milestone_sort == "pattern":
            prompter.print(
                "\n  Enter a regex pattern with one capture group for the number."
            )
            prompter.print("  Examples:")
            prompter.print("    M(\\d+)       → matches 'M13' → 13")
            prompter.print("    Sprint (\\d+) → matches 'Sprint 5' → 5")
            pattern = prompter.input("  Pattern", r"M(\d+)")
            milestones_config["sort_config"] = {"pattern": pattern}

    if "milestones" not in config or "order" not in config.get("milestones", {}):
        order_raw = prompter.input(
            "Optional milestone order (comma-separated, blank for none)", ""
        )
        if order_raw.strip():
            milestones_config = cast(
                dict[str, Any], config.setdefault("milestones", {})
            )
            milestones_config["order"] = [
                m.strip() for m in order_raw.split(",") if m.strip()
            ]

    # Foundation milestone for dependency scope - only ask if not already configured
    if "milestones" not in config or "foundation" not in config.get("milestones", {}):
        prompter.print(
            "\n  Dependencies must be within the same milestone OR in the foundation milestone."
        )
        prompter.print(
            "  Example: Issues in M2 can depend on M2 issues or M0 (foundation) issues."
        )
        foundation_milestone = prompter.input("Foundation milestone", "M0")
        milestones_config = cast(dict[str, Any], config.setdefault("milestones", {}))
        milestones_config["foundation"] = foundation_milestone

    # Check if config needs worktrees.base — schema-driven
    if "worktrees" not in config or "base" not in config.get("worktrees", {}):
        prompter.print("\n--- Worktree Location ---")
        prompter.print("Each issue gets its own git worktree for isolated work.")
        prompter.print("Examples:")
        prompter.print("  '../'           → sibling dirs (~/dev/myrepo-123)")
        prompter.print(
            "  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)"
        )
        _wt_fields = get_setup_fields("worktrees")
        _wt_field = (
            _wt_fields[0]
            if _wt_fields
            else {"prompt": "Worktree Base Directory", "default": "../"}
        )
        worktree_base = prompter.input(_wt_field["prompt"], str(_wt_field["default"]))
        worktrees_config = cast(dict[str, Any], config.setdefault("worktrees", {}))
        worktrees_config["base"] = worktree_base

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

    # Worktree setup commands (if not already configured)
    worktrees_cfg = config.get("worktrees", {})
    if "setup" not in worktrees_cfg:
        prompter.print("\n--- Worktree Setup Commands ---")
        prompter.print(
            "Commands to run in each new worktree after creation (e.g., install deps)."
        )
        prompter.print("Examples:")
        prompter.print("  npm install")
        prompter.print("  pip install -e '.[dev]'")
        prompter.print("  make setup")
        setup_input = prompter.input(
            "Setup commands (comma-separated, or empty to skip)", ""
        )
        if setup_input.strip():
            setup_cmds = [cmd.strip() for cmd in setup_input.split(",") if cmd.strip()]
            wt_config = cast(dict[str, Any], config.setdefault("worktrees", {}))
            wt_config["setup"] = setup_cmds

    # UI mode
    if "ui" not in config or "mode" not in config.get("ui", {}):
        prompter.print("\n--- UI Mode ---")
        prompter.print("How do you want to monitor agent sessions?\n")
        prompter.print("  web    - Browser dashboard (recommended)")
        prompter.print(
            "           Best for most users. Opens in your browser or forwarded client URL."
        )
        prompter.print("  tmux   - Terminal multiplexer sessions")
        prompter.print(
            "           For terminal power users. Requires tmux installed.\n"
        )
        ui_mode = prompter.input("UI mode", "web")
        if ui_mode not in ("web", "tmux"):
            prompter.print(f"  Invalid mode '{ui_mode}', using 'web'")
            ui_mode = "web"
        ui_config = cast(dict[str, Any], config.setdefault("ui", {}))
        ui_config["mode"] = ui_mode
        if ui_mode == "web":
            for field in get_setup_fields("ui"):
                cond = field.get("condition")
                if (
                    cond
                    and cond.get("field") == "ui_mode"
                    and cond.get("value") != ui_mode
                ):
                    continue
                raw = prompter.input(field["prompt"], str(field["default"]))
                ui_config[field["name"]] = int(raw)
            prompter.print("\n--- Terminal Backend (web mode) ---")
            prompter.print("Choose how agent sessions are executed:\n")
            prompter.print("  tmux       - Default (stable, interactive)")
            prompter.print(
                "  subprocess - No tmux dependency; records raw terminal output in "
                ".issue-orchestrator/sessions/<run>/terminal-recording.jsonl\n"
            )
            terminal_backend = prompter.input("Terminal backend", "tmux")
            if terminal_backend not in ("tmux", "subprocess"):
                prompter.print(f"  Invalid backend '{terminal_backend}', using 'tmux'")
                terminal_backend = "tmux"
            if terminal_backend == "subprocess":
                execution_config = cast(
                    dict[str, Any], config.setdefault("execution", {})
                )
                execution_config["terminal_adapter"] = "subprocess"

    # Label prefix - only ask if not already configured
    if "labels" not in config or "prefix" not in config.get("labels", {}):
        prompter.print("\n--- Label Prefix ---")
        prompter.print(
            "Prefix for status labels to avoid conflicts with existing labels."
        )
        prompter.print("  Example: 'io' → 'io:in-progress', 'io:blocked', etc.\n")
        label_prefix = prompter.input("Label prefix", "io")
        if label_prefix:
            if "labels" not in config:
                config["labels"] = {}
            config["labels"]["prefix"] = label_prefix
            prompter.print(
                f"  ✓ Labels will be prefixed: {label_prefix}:in-progress, {label_prefix}:blocked, etc."
            )

    # Two-stage review workflow - ask if not already configured (default enabled)
    has_review_config = "review" in config and config["review"].get("enabled")
    if not has_review_config:
        prompter.print("\n--- Review Workflow ---")
        prompter.print("Code review is RECOMMENDED to catch issues before merging:")
        prompter.print("  Stage 1: Per-PR code review (immediate, after each PR)")
        prompter.print(
            "  Stage 2: Triage batch review (when N reviewed PRs accumulate)\n"
        )

        # Stage 1: Per-PR Code Review (default enabled)
        if prompter.yes_no("Enable Stage 1: Per-PR code review?", default=True):
            prompter.print("\n  --- Stage 1: Per-PR Code Review ---")
            code_review_agent = prompter.input(
                "  Code review agent label", "agent:reviewer"
            )
            code_review_label = prompter.input(
                "  Label for PRs needing review", "needs-code-review"
            )
            code_reviewed_label = prompter.input(
                "  Label after review passes", "code-reviewed"
            )

            # Use new review structure with enabled flag
            config["review"] = {
                "enabled": True,
                "default": code_review_agent,
                "code_review_label": code_review_label,
                "code_reviewed_label": code_reviewed_label,
            }
            prompter.print(f"  ✓ PRs will be reviewed by {code_review_agent}")
            prompter.print(
                f"  ✓ Label flow: {code_review_label} → {code_reviewed_label}"
            )

            # Stage 2: Triage Batch Review (only if Stage 1 enabled)
            prompter.print("")
            if prompter.yes_no("Enable Stage 2: Triage batch review?", default=False):
                prompter.print("\n  --- Stage 2: Triage Batch Review ---")
                triage_review_agent = prompter.input(
                    "  triage review agent label", "agent:triage"
                )
                triage_reviewed_label = prompter.input(
                    "  Label after triage review", "triage-reviewed"
                )
                threshold = prompter.input("  Trigger after N code-reviewed PRs", "5")

                config["review"]["triage_review_agent"] = triage_review_agent
                config["review"]["triage_reviewed_label"] = triage_reviewed_label
                try:
                    threshold_int = int(threshold)
                    if threshold_int > 0:
                        config["review"]["triage_review_threshold"] = threshold_int
                        prompter.print(
                            f"  ✓ triage review triggers after {threshold_int} PRs with '{code_reviewed_label}'"
                        )
                except ValueError:
                    pass
                prompter.print(
                    f"  ✓ Label flow: {code_reviewed_label} → {triage_reviewed_label}"
                )

    return config, updating_existing_path


def create_starter_prompt(
    agent_name: str,
    path: Path,
    file_collector: FileCollector | None = None,
) -> None:
    """Create a starter prompt file for an agent."""
    agent_short = agent_name.split(":")[-1]
    content = f"""# {agent_short.title()} Agent Prompt

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

You MUST use `coding-done` to complete. This runs validation, then the orchestrator pushes your code and creates the PR.

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
1. Validation runs (tests, linting) - if it fails, fix and retry
2. Orchestrator pushes your branch
3. Orchestrator creates PR and posts comment
4. Session completes
"""
    if file_collector is not None:
        file_collector.add_write(path, content, "create")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def create_code_review_prompt(
    path: Path,
    code_review_label: str,
    code_reviewed_label: str,
    file_collector: FileCollector | None = None,
) -> None:
    """Create a code review prompt with actual label values substituted.

    Template variables like {{pr_number}} become {pr_number} in output,
    which get_command() substitutes at runtime.
    """
    content = f"""# Code Review Agent

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
  --risk low  # or medium, high
```

### If changes are needed:

```bash
reviewer-done changes_requested \\
  --issues "Specific issues that need fixing (be detailed)" \\
  --risk medium  # or low, high
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
    if file_collector is not None:
        file_collector.add_write(path, content, "create")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def create_triage_review_prompt(
    path: Path,
    review_label: str,
    reviewed_label: str,
    file_collector: FileCollector | None = None,
) -> None:
    """Create a triage review prompt with actual label values substituted."""
    content = f"""# Triage Review Agent

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
    if file_collector is not None:
        file_collector.add_write(path, content, "create")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that doesn't use aliases."""

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


def write_config(
    config: dict[str, Any],
    path: Path,
    file_collector: FileCollector | None = None,
) -> None:
    """Write config to YAML file.

    Args:
        config: Configuration dictionary to write.
        path: Path to write the config file.
        file_collector: If provided, collect the write instead of executing it.
    """
    import io

    buffer = io.StringIO()
    yaml.dump(
        config,
        buffer,
        Dumper=_NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    content = CONFIG_HEADER + buffer.getvalue()

    if file_collector is not None:
        action = "overwrite" if path.exists() else "create"
        file_collector.add_write(path, content, action)
    else:
        with open(path, "w") as f:
            f.write(content)


def _print_changes_summary(
    collector: FileCollector, prompter: Prompter, dry_run: bool = False
) -> None:
    """Print summary of changes to be applied."""
    prompter.print("\n" + "=" * 50)
    if dry_run:
        prompter.print("CHANGES THAT WOULD BE APPLIED")
    else:
        prompter.print("CHANGES TO APPLY")
    prompter.print("=" * 50)

    if collector.writes:
        prompter.print("\nFiles to create/modify:")
        for write in collector.writes:
            prompter.print(f"  [{write.action}] {write.path} ({write.size_display()})")
    else:
        prompter.print("\nFiles: (none)")

    if collector.labels:
        prompter.print("\nGitHub labels to create:")
        for name, _color, desc in collector.labels:
            prompter.print(f"  • {name} - {desc}")
    else:
        prompter.print("\nGitHub labels: (none - all exist)")

    prompter.print("")


def _apply_changes(
    collector: FileCollector, repo: str | None, prompter: Prompter
) -> None:
    """Apply all collected changes."""
    # Write files
    for write in collector.writes:
        write.path.parent.mkdir(parents=True, exist_ok=True)
        if write.action == "append":
            with open(write.path, "a") as f:
                f.write(write.content)
        else:
            write.path.write_text(write.content)
        prompter.print(f"  ✓ {write.action.title()}d {write.path}")

    # Create labels
    if collector.labels and repo:
        client = _get_repository_host(repo)
        for name, color, desc in collector.labels:
            client.create_label(name, color=color, description=desc, force=True)
        prompter.print(f"  ✓ Created {len(collector.labels)} GitHub labels")


def setup_ai_providers(prompter: Prompter) -> None:
    """Ask about AI providers and help store keys in keyring."""
    from ...infra.ai_keys import AI_PROVIDERS, store_ai_key, read_ai_key

    prompter.print("\n" + "=" * 50)
    prompter.print("AI PROVIDER SETUP")
    prompter.print("=" * 50)
    prompter.print("\nYour agents need API keys to authenticate with AI providers.")
    prompter.print("Keys are stored securely in your system keyring.\n")

    for key_name, info in AI_PROVIDERS.items():
        existing = read_ai_key(key_name)
        if existing:
            status = "[configured]"
            prompter.print(f"  {info['name']}: {status}")
            if not prompter.yes_no(f"  Update {info['name']} key?", default=False):
                continue
        else:
            if not prompter.yes_no(
                f"Configure {info['name']}?", default=key_name == "ANTHROPIC_API_KEY"
            ):
                continue

        # Show setup instructions
        prompter.print(f"\n  --- {info['name']} Setup ---")
        if info.get("setup_cmd"):
            prompter.print(f"  Run in another terminal: {info['setup_cmd']}")
            prompter.print("  Then paste the key here.")
        else:
            prompter.print(f"  {info.get('setup_help', '')}")
        prompter.print(f"  URL: {info.get('url', '')}\n")

        # Prompt for key
        import getpass

        value = getpass.getpass(f"  Paste your {key_name}: ")
        if value.strip():
            try:
                store_ai_key(key_name, value.strip())
                prompter.print(f"  ✓ Stored {key_name} in keyring\n")
            except Exception as e:
                prompter.print(f"  ✗ Failed to store key: {e}")
                prompter.print(
                    "    You can set it as an environment variable instead.\n"
                )
        else:
            prompter.print("  Skipped (no key provided)\n")


def run_wizard(  # noqa: C901, PLR0912 - main wizard entry point with prerequisite checks, mode selection, and confirmation flow
    target_path: Path | None = None,
    prompter: Prompter | None = None,
    dry_run: bool = False,
) -> None:
    """Main wizard entry point.

    Args:
        target_path: Directory to set up. If None, prompts user.
        prompter: Prompter for user interaction. If None, uses ConsolePrompter.
        dry_run: If True, show what would be done without writing files.
    """
    if prompter is None:
        prompter = ConsolePrompter()

    # Always create file collector to track changes before applying
    file_collector = FileCollector()

    prompter.print("\n" + "=" * 50)
    if dry_run:
        prompter.print("  issue-orchestrator Setup Wizard (DRY RUN)")
    else:
        prompter.print("  issue-orchestrator Setup Wizard")
    prompter.print("=" * 50)

    # Determine target directory
    if target_path is None:
        cwd = Path.cwd()
        prompter.print(f"\nCurrent directory: {cwd}")

        # Check if this looks like the issue-orchestrator package itself
        is_orchestrator_dir = (cwd / "src" / "issue_orchestrator").exists()
        if is_orchestrator_dir:
            prompter.print(
                "⚠ This looks like the issue-orchestrator package directory."
            )
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

    _original_cwd = Path.cwd()
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
        prompter.print(
            "\n⚠ Some prerequisites are missing. Install them before continuing."
        )
        if not prereqs["github_auth"]:
            prompter.print("  Set a GitHub token (GITHUB_TOKEN or config)")
        if not prereqs.get("any_ai_provider", False):
            prompter.print("  Install at least one AI provider CLI:")
            for name in list_providers():
                provider = get_provider(name)
                prompter.print(f"    - {name}: {provider.description}")
        if not prompter.yes_no("Continue anyway?", default=False):
            sys.exit(1)

    # Explain the wizard flow
    if dry_run:
        prompter.print("\n⚠ DRY RUN MODE - No changes will be made")
    prompter.print("\nThis wizard will:")
    prompter.print("  1. Ask questions about your project configuration")
    prompter.print("  2. Show a summary of all changes")
    prompter.print("  3. Apply changes only after your approval")

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
        review_cfg = config.get("review", {})
        has_triage = review_cfg.get("triage_review_agent")
        has_code_review = review_cfg.get("enabled") or review_cfg.get("default")

        # Include section based on their review workflow
        if has_triage:
            # Triage workflow - cleanup happens after triage review
            config["cleanup"] = {
                "with_triage": {
                    "close_ai_session_tabs": True,
                    "remove_worktrees": False,
                }
            }
        elif has_code_review:
            # Code review only - cleanup after code review
            config["cleanup"] = {
                "without_triage": {
                    "wait_for_code_review": True,
                    "close_ai_session_tabs": True,
                    "remove_worktrees": False,
                }
            }
        else:
            # No review workflow - cleanup on completion
            config["cleanup"] = {
                "without_triage": {
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

    # Determine config file path and collect the write
    # Use absolute paths to avoid issues with cwd
    from ...infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME

    default_config_path = (
        f"{CONFIG_DIR}/{DEFAULT_CONFIG_NAME}"  # .issue-orchestrator/config/default.yaml
    )

    if existing_config_path:
        output_path = (
            target_path / existing_config_path
            if not existing_config_path.is_absolute()
            else existing_config_path
        )
    elif dry_run:
        output_path = target_path / default_config_path
    else:
        user_path = Path(
            prompter.input(f"Config filename ({target_path}/)", default_config_path)
        )
        output_path = (
            target_path / user_path if not user_path.is_absolute() else user_path
        )

    write_config(config, output_path, file_collector)

    # Collect prompt file writes (removed intermediate confirmations)

    # Get review config
    review_config = config.get("review", {})
    code_review_agent = review_config.get("default")
    code_review_label = review_config.get("code_review_label", "needs-code-review")
    code_reviewed_label = review_config.get("code_reviewed_label", "code-reviewed")
    triage_review_agent = review_config.get("triage_review_agent")
    triage_reviewed_label = review_config.get(
        "triage_reviewed_label", "triage-reviewed"
    )

    # Track all prompt files for the next steps summary
    all_prompt_paths: list[Path] = []

    for agent_name, agent_config in config.get("agents", {}).items():
        prompt_rel_path = Path(agent_config["prompt"])
        # Use absolute path for file operations
        prompt_path = (
            target_path / prompt_rel_path
            if not prompt_rel_path.is_absolute()
            else prompt_rel_path
        )
        all_prompt_paths.append(prompt_rel_path)  # Keep relative for display

        # Collect prompt file writes for missing files
        if not prompt_path.exists():
            is_code_review_agent = (
                code_review_agent and agent_name == code_review_agent
            ) or (agent_name.lower() == "agent:reviewer")

            is_triage_review_agent = (
                triage_review_agent and agent_name == triage_review_agent
            ) or "triage" in agent_name.lower()

            if is_code_review_agent and code_review_agent:
                create_code_review_prompt(
                    prompt_path, code_review_label, code_reviewed_label, file_collector
                )
            elif is_triage_review_agent and triage_review_agent:
                create_triage_review_prompt(
                    prompt_path,
                    code_reviewed_label,
                    triage_reviewed_label,
                    file_collector,
                )
            else:
                create_starter_prompt(agent_name, prompt_path, file_collector)

    # Collect all labels to create on GitHub
    repo_config = config.get("repo") or {}
    repo_name = (
        repo_config.get("name") if isinstance(repo_config, dict) else repo_config
    )
    if repo_name:
        # Gather all labels we want to ensure exist
        labels_config = config.get("labels", {})
        label_prefix = labels_config.get("prefix", "")

        def prefixed(label: str) -> str:
            """Apply label prefix if configured."""
            return f"{label_prefix}:{label}" if label_prefix else label

        # Agent labels (e.g., agent:developer, agent:reviewer)
        agent_labels = [
            (agent_name, "1D76DB", f"Issues for {agent_name.split(':')[-1]} agent")
            for agent_name in config.get("agents", {}).keys()
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
        all_labels = agent_labels + priority_labels + status_labels

        # Add review labels if configured (two-stage review workflow)
        if code_review_agent:
            all_labels.extend(
                [
                    (code_review_label, "7057FF", "PR needs code review"),
                    (code_reviewed_label, "0E8A16", "PR has been code reviewed"),
                ]
            )
        if triage_review_agent:
            all_labels.append(
                (triage_reviewed_label, "1D76DB", "PR has been triage reviewed")
            )

        # Check which labels already exist and collect missing ones
        existing_labels = set(fetch_github_labels(repo_name))
        missing_labels = [
            (name, color, desc)
            for name, color, desc in all_labels
            if name not in existing_labels
        ]

        for name, color, desc in missing_labels:
            file_collector.add_label(name, color, desc)

    # Show summary and ask for confirmation
    _print_changes_summary(file_collector, prompter, dry_run)

    if dry_run:
        prompter.print("Run without --dry-run to apply these changes.")
        return

    if not prompter.yes_no("\nApply these changes?"):
        prompter.print("Aborted.")
        sys.exit(0)

    # Apply all changes
    prompter.print("\nApplying changes...")
    _apply_changes(file_collector, repo_name, prompter)

    # Install AI agent hooks (blocks --no-verify and other bypass attempts)
    install_hooks_now = prompter.yes_no(
        "\nInstall AI agent hooks now? (recommended)", default=True
    )
    if install_hooks_now:
        try:
            from ...infra.config import Config
            from ...infra.hooks.hooks import install_hooks_for_config

            temp_config = Config.load(output_path)
            installed = install_hooks_for_config(temp_config, target_path)
            if installed:
                prompter.print("\nHooks installed:")
                for agent_type, paths in installed.items():
                    for path in paths:
                        prompter.print(f"  ✓ {agent_type.value}: {path}")
            else:
                prompter.print("\nNo hooks installed (no supported agents detected).")
        except Exception as exc:
            prompter.print(f"\n⚠ Hook installation failed: {exc}")
            prompter.print("  You can retry later with: issue-orchestrator setup-hooks")

    if (config.get("validation") or {}).get("cmd"):
        harden_repo_now = prompter.yes_no(
            "\nInstall repo-local pre-push guardrails now? (recommended)",
            default=True,
        )
        if harden_repo_now:
            try:
                from ...infra.config import Config
                from ...infra.repo_hardening import harden_repo

                temp_config = Config.load(output_path)
                result = harden_repo(temp_config, target_root=target_path)
                prompter.print("\nRepo guardrails installed:")
                prompter.print(f"  ✓ Hooks path: {result.hooks_path_config}")
                prompter.print(f"  ✓ {result.pre_push_hook.relative_to(target_path)}")
                prompter.print(f"  ✓ {result.verify_script.relative_to(target_path)}")
            except Exception as exc:
                prompter.print(f"\n⚠ Repo hardening failed: {exc}")
                prompter.print(
                    "  You can retry later with: issue-orchestrator harden-repo"
                )
    else:
        prompter.print(
            "\nRepo-local pre-push hardening skipped: configure validation.cmd first, "
            "then run 'issue-orchestrator harden-repo'."
        )

    # AI provider key setup
    if prompter.yes_no("\nSet up AI provider API keys now?", default=True):
        setup_ai_providers(prompter)

    # MCP review exchange probe (if configured)
    try:
        from ...infra.config import Config
        from ...infra.review_exchange_probe import probe_review_exchange
        from ...execution.command_runner import LocalCommandRunner

        exchange = (config.get("review") or {}).get("exchange", {})
        if isinstance(exchange, dict) and exchange.get("mode") in ("via-mcp", "auto"):
            prompter.print("\nRunning MCP review-exchange validation...")
            temp_config = Config.load(output_path)
            checks = probe_review_exchange(
                temp_config, LocalCommandRunner(), force=True
            )
            for check in checks:
                prompter.print(f"  - {check.name}: {check.status} ({check.detail})")
    except Exception as exc:
        prompter.print(f"\nMCP validation skipped: {exc}")

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
        label
        for label in agent_labels
        if label != code_review_agent and label != triage_review_agent
    ]
    prompter.print("\n  2. Add agent labels to your GitHub issues:")
    for label in work_agent_labels:
        prompter.print(f"     • {label}")

    prompter.print("\n  3. Run: issue-orchestrator start")

    if not install_hooks_now:
        prompter.print(
            "\n  4. Install AI agent hooks (recommended): issue-orchestrator setup-hooks"
        )
        prompter.print(
            "  5. Harden repo guardrails (recommended): issue-orchestrator harden-repo"
        )

    prompter.print("\n  Advanced features (enable in config later):")
    prompter.print(
        "     E2E Test Runner - Automatically runs your test suite when main"
    )
    prompter.print("     branch changes. Tracks flaky tests, retries failures, shows")
    prompter.print("     progress in dashboard. Enable: e2e.enabled: true")
    prompter.print("     See: docs/user/e2e.md")
    prompter.print("")


if __name__ == "__main__":
    run_wizard()
