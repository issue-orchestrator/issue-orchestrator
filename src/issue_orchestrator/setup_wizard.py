"""Interactive setup wizard for issue-orchestrator."""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DetectedState:
    """What we found in an existing repo."""

    repo: str | None = None
    github_labels: list[str] = field(default_factory=list)
    agent_labels: list[str] = field(default_factory=list)
    existing_config: dict | None = None
    config_path: Path | None = None
    prompt_candidates: list[Path] = field(default_factory=list)


def run_gh(args: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    """Run gh CLI command, return (success, output)."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, ""


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

    # gh CLI
    ok, _ = run_gh(["--version"])
    checks["gh"] = ok

    # gh auth
    ok, _ = run_gh(["auth", "status"])
    checks["gh_auth"] = ok

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
    ok, output = run_gh(["label", "list", "--repo", repo, "--limit", "100", "--json", "name"])
    if not ok:
        return []

    try:
        import json

        labels = json.loads(output)
        return [label["name"] for label in labels]
    except (json.JSONDecodeError, KeyError):
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


def prompt_input(question: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        result = input(f"{question} [{default}]: ").strip()
        return result if result else default
    return input(f"{question}: ").strip()


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no answer."""
    suffix = "[Y/n]" if default else "[y/N]"
    result = input(f"{question} {suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def prompt_choice(question: str, choices: list[str], allow_custom: bool = False) -> str:
    """Prompt user to choose from a list."""
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


def wizard_new_project() -> dict[str, Any]:
    """Walk through new project setup."""
    config: dict[str, Any] = {"agents": {}}

    print("\n" + "=" * 50)
    print("NEW PROJECT SETUP")
    print("=" * 50)

    # Repo
    detected_repo = detect_repo()
    if detected_repo:
        config["repo"] = prompt_input("GitHub repo", detected_repo)
    else:
        config["repo"] = prompt_input("GitHub repo (owner/name)")

    # Agents
    print("\n--- Agent Configuration ---")
    print("Agents are identified by GitHub labels (e.g., 'agent:backend').")
    print("Each agent needs a prompt file with instructions.\n")

    while True:
        agent_name = prompt_input("Agent label (e.g., 'agent:backend', or empty to finish)")
        if not agent_name:
            if not config["agents"]:
                print("You need at least one agent!")
                continue
            break

        if not agent_name.startswith("agent:"):
            if prompt_yes_no(f"Add 'agent:' prefix to make it '{f'agent:{agent_name}'}'?"):
                agent_name = f"agent:{agent_name}"

        prompt_path = prompt_input(
            f"Prompt file path for {agent_name}",
            f".issue-orchestrator/prompts/{agent_name.split(':')[-1]}.md",
        )

        model = prompt_choice("Model for this agent", ["sonnet", "opus", "haiku"])

        timeout = prompt_input("Timeout in minutes", "45")

        config["agents"][agent_name] = {
            "prompt": prompt_path,
            "model": model,
            "timeout_minutes": int(timeout),
        }

        print(f"✓ Added {agent_name}\n")

    # Concurrency
    print("\n--- Concurrency Settings ---")
    max_sessions = prompt_input("Max concurrent agent sessions", "3")
    config["concurrency"] = {
        "max_concurrent_sessions": int(max_sessions),
    }

    # Worktree location
    print("\n--- Worktree Location ---")
    print("Each issue gets its own git worktree for isolated work.")
    print("Examples:")
    print("  '../'           → sibling dirs (~/dev/myrepo-123)")
    print("  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)")
    worktree_base = prompt_input("Worktree base directory", "../")

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
                print(f"  ✓ {worktree_dir} already in .gitignore")

        if needs_gitignore:
            if prompt_yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                with open(gitignore_path, "a") as f:
                    f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # UI Mode
    print("\n--- UI Mode ---")
    print("How do you want to monitor agent sessions?\n")
    print("  web    - Browser dashboard at localhost (recommended)")
    print("           Best for most users. Visual overview of all agents.")
    print("  tmux   - Terminal multiplexer sessions")
    print("           For terminal power users. Requires tmux installed.")
    print("  iterm2 - Native iTerm2 tabs (macOS only)")
    print("           Each agent runs in its own iTerm2 tab.\n")
    ui_mode = prompt_input("UI mode", "web")
    if ui_mode not in ("web", "tmux", "iterm2"):
        print(f"  Invalid mode '{ui_mode}', using 'web'")
        ui_mode = "web"
    config["ui_mode"] = ui_mode
    if ui_mode == "web":
        port = prompt_input("Web dashboard port", "8080")
        config["web_port"] = int(port)

    # Labels
    print("\n--- Label Configuration ---")
    if not prompt_yes_no("Use default labels (in-progress, blocked, needs-human)?"):
        config["labels"] = {
            "in_progress": prompt_input("In-progress label", "in-progress"),
            "blocked": prompt_input("Blocked label", "blocked"),
            "needs_human": prompt_input("Needs-human label", "needs-human"),
        }

    return config


def wizard_existing_project(state: DetectedState) -> dict[str, Any]:
    """Walk through existing project onboarding."""
    print("\n" + "=" * 50)
    print("EXISTING PROJECT ONBOARDING")
    print("=" * 50)

    # Show what we found
    print("\n--- Detected State ---")
    print(f"  Repo: {state.repo or 'Not detected'}")
    print(f"  Existing config: {state.config_path or 'None'}")
    print(f"  GitHub labels: {len(state.github_labels)} total")
    print(f"  Agent labels: {', '.join(state.agent_labels) or 'None'}")
    print(f"  Prompt candidates: {len(state.prompt_candidates)} files")

    # Start with existing config or fresh
    config: dict[str, Any]
    if state.existing_config:
        print(f"\n✓ Found existing config at {state.config_path}")
        if prompt_yes_no("Update existing config?"):
            config = dict(state.existing_config)
        else:
            config = {"agents": {}}
    else:
        config = {"agents": {}}

    # Ensure repo is set
    if "repo" not in config:
        config["repo"] = state.repo or prompt_input("GitHub repo (owner/name)")

    # Check for agent labels not in config
    configured_agents = set(config.get("agents", {}).keys())
    unconfigured_agents = [a for a in state.agent_labels if a not in configured_agents]

    if unconfigured_agents:
        print(f"\n--- Found {len(unconfigured_agents)} agent labels not in config ---")
        for agent_label in unconfigured_agents:
            print(f"\nAgent: {agent_label}")
            if prompt_yes_no(f"Add {agent_label} to config?"):
                # Suggest prompt files
                agent_short = agent_label.split(":")[-1]
                matching_prompts = [
                    p for p in state.prompt_candidates if agent_short.lower() in p.name.lower()
                ]

                if matching_prompts:
                    print("  Possible prompt files:")
                    for i, p in enumerate(matching_prompts[:5], 1):
                        print(f"    {i}. {p.relative_to(Path.cwd())}")
                    choice = prompt_input("Choose (number) or enter path", "1")
                    try:
                        idx = int(choice) - 1
                        prompt_path = str(matching_prompts[idx].relative_to(Path.cwd()))
                    except (ValueError, IndexError):
                        prompt_path = choice
                else:
                    prompt_path = prompt_input(
                        "Prompt file path",
                        f".issue-orchestrator/prompts/{agent_short}.md",
                    )

                model = prompt_choice("Model", ["sonnet", "opus", "haiku"])
                timeout = prompt_input("Timeout (minutes)", "45")

                if "agents" not in config:
                    config["agents"] = {}

                config["agents"][agent_label] = {
                    "prompt": prompt_path,
                    "model": model,
                    "timeout_minutes": int(timeout),
                }
                print(f"  ✓ Added {agent_label}")

    # Check for configured agents with missing labels on GitHub
    if configured_agents:
        missing_labels = [a for a in configured_agents if a not in state.github_labels]
        if missing_labels:
            print(f"\n⚠ These agents are in config but missing GitHub labels:")
            for label in missing_labels:
                print(f"    - {label}")
            if prompt_yes_no("Create missing labels on GitHub?"):
                repo = str(config["repo"])
                for label in missing_labels:
                    ok, _ = run_gh(
                        [
                            "label",
                            "create",
                            label,
                            "--repo",
                            repo,
                            "--color",
                            "1D76DB",
                            "--description",
                            f"Issues for {label.split(':')[-1]} agent",
                        ]
                    )
                    if ok:
                        print(f"  ✓ Created {label}")
                    else:
                        print(f"  ✗ Failed to create {label}")

    # Ensure we have concurrency settings
    if "concurrency" not in config:
        print("\n--- Concurrency Settings ---")
        max_sessions = prompt_input("Max concurrent sessions", "3")
        config["concurrency"] = {"max_concurrent_sessions": int(max_sessions)}

    # Check if agents need worktree_base
    agents_without_worktree = [
        name for name, cfg in config.get("agents", {}).items()
        if "worktree_base" not in cfg
    ]
    if agents_without_worktree:
        print("\n--- Worktree Location ---")
        print("Each issue gets its own git worktree for isolated work.")
        print("Examples:")
        print("  '../'           → sibling dirs (~/dev/myrepo-123)")
        print("  './worktrees'   → subdirectory (~/dev/myrepo/worktrees/myrepo-123)")
        worktree_base = prompt_input("Worktree base directory", "../")

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
                    print(f"  ✓ {worktree_dir} already in .gitignore")

            if needs_gitignore:
                if prompt_yes_no(f"Add '{worktree_dir}/' to .gitignore?"):
                    with open(gitignore_path, "a") as f:
                        f.write(f"\n# Issue orchestrator worktrees\n{worktree_dir}/\n")
                    print(f"  ✓ Added {worktree_dir}/ to .gitignore")

    # UI mode
    if "ui_mode" not in config:
        print("\n--- UI Mode ---")
        print("How do you want to monitor agent sessions?\n")
        print("  web    - Browser dashboard at localhost (recommended)")
        print("           Best for most users. Visual overview of all agents.")
        print("  tmux   - Terminal multiplexer sessions")
        print("           For terminal power users. Requires tmux installed.")
        print("  iterm2 - Native iTerm2 tabs (macOS only)")
        print("           Each agent runs in its own iTerm2 tab.\n")
        ui_mode = prompt_input("UI mode", "web")
        if ui_mode not in ("web", "tmux", "iterm2"):
            print(f"  Invalid mode '{ui_mode}', using 'web'")
            ui_mode = "web"
        config["ui_mode"] = ui_mode
        if ui_mode == "web":
            config["web_port"] = int(prompt_input("Web port", "8080"))

    return config


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


class _NoAliasDumper(yaml.SafeDumper):
    """YAML dumper that doesn't use aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def write_config(config: dict[str, Any], path: Path) -> None:
    """Write config to YAML file."""
    with open(path, "w") as f:
        yaml.dump(config, f, Dumper=_NoAliasDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


def create_start_script(config: dict, script_path: Path) -> None:
    """Create a shell script to start the orchestrator."""
    ui_mode = config.get("ui_mode", "web")
    web_port = config.get("web_port", 8080)

    # Build the start command with options
    start_cmd = "issue-orchestrator start"
    if ui_mode != "web":
        start_cmd += f" --ui-mode {ui_mode}"
    if ui_mode == "web" and web_port != 8080:
        start_cmd += f" --port {web_port}"

    script_content = f'''#!/bin/bash
# Start issue-orchestrator agents
# Generated by: issue-orchestrator setup

set -e

# Change to the directory containing this script
cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Start the orchestrator
echo "Starting issue-orchestrator..."
{start_cmd}
'''

    script_path.write_text(script_content)
    # Make executable
    script_path.chmod(0o755)


def create_start_script_fish(config: dict, script_path: Path) -> None:
    """Create a fish shell script to start the orchestrator."""
    ui_mode = config.get("ui_mode", "web")
    web_port = config.get("web_port", 8080)

    start_cmd = "issue-orchestrator start"
    if ui_mode != "web":
        start_cmd += f" --ui-mode {ui_mode}"
    if ui_mode == "web" and web_port != 8080:
        start_cmd += f" --port {web_port}"

    script_content = f'''#!/usr/bin/env fish
# Start issue-orchestrator agents
# Generated by: issue-orchestrator setup

# Change to the directory containing this script
cd (dirname (status filename))

# Activate virtual environment if it exists
if test -f ".venv/bin/activate.fish"
    source .venv/bin/activate.fish
else if test -f "venv/bin/activate.fish"
    source venv/bin/activate.fish
end

# Start the orchestrator
echo "Starting issue-orchestrator..."
{start_cmd}
'''

    script_path.write_text(script_content)
    script_path.chmod(0o755)


def run_wizard(target_path: Path | None = None) -> None:
    """Main wizard entry point.

    Args:
        target_path: Directory to set up. If None, prompts user.
    """
    print("\n" + "=" * 50)
    print("  issue-orchestrator Setup Wizard")
    print("=" * 50)

    # Determine target directory
    if target_path is None:
        cwd = Path.cwd()
        print(f"\nCurrent directory: {cwd}")

        # Check if this looks like the issue-orchestrator package itself
        is_orchestrator_dir = (cwd / "src" / "issue_orchestrator").exists()
        if is_orchestrator_dir:
            print("⚠ This looks like the issue-orchestrator package directory.")
            print("  You probably want to set up a different project.\n")
            # Don't default to this directory
            target_input = prompt_input("Project directory to set up (required)", "")
            if not target_input:
                print("Error: Please specify a project directory.")
                sys.exit(1)
        else:
            target_input = prompt_input("Project directory to set up", str(cwd))

        target_path = Path(target_input).expanduser().resolve()

        if not target_path.exists():
            print(f"[red]Error: {target_path} does not exist[/red]")
            sys.exit(1)
        if not target_path.is_dir():
            print(f"[red]Error: {target_path} is not a directory[/red]")
            sys.exit(1)

    # Change to target directory for the rest of the wizard
    import os
    original_cwd = Path.cwd()
    os.chdir(target_path)
    print(f"\nSetting up: {target_path}\n")

    # Check prerequisites
    print("Checking prerequisites...")
    prereqs = check_prerequisites()

    all_ok = True
    for tool, ok in prereqs.items():
        status = "✓" if ok else "✗"
        print(f"  {status} {tool}")
        if not ok:
            all_ok = False

    if not all_ok:
        print("\n⚠ Some prerequisites are missing. Install them before continuing.")
        if not prereqs["gh_auth"]:
            print("  Run: gh auth login")
        if not prereqs["claude"]:
            print("  Install Claude Code CLI")
        if not prompt_yes_no("Continue anyway?", default=False):
            sys.exit(1)

    # Choose mode
    print("\n" + "-" * 50)
    mode = prompt_choice(
        "What would you like to do?",
        [
            "New project - set up from scratch",
            "Existing project - I have labels/issues already",
        ],
    )

    if "New" in mode:
        config = wizard_new_project()
    else:
        state = scan_existing_repo()
        config = wizard_existing_project(state)

    # Review config
    print("\n" + "=" * 50)
    print("CONFIGURATION SUMMARY")
    print("=" * 50)
    print(yaml.dump(config, default_flow_style=False, sort_keys=False))

    if not prompt_yes_no("Save this configuration?"):
        print("Aborted.")
        sys.exit(0)

    # Choose output path
    default_path = ".issue-orchestrator.yaml"
    output_path = Path(prompt_input("Config file path", default_path))

    # Check for existing
    if output_path.exists():
        if not prompt_yes_no(f"{output_path} exists. Overwrite?"):
            print("Aborted.")
            sys.exit(0)

    write_config(config, output_path)
    print(f"\n✓ Saved config to {output_path}")

    # Create missing prompt files
    print("\n--- Prompt Files ---")
    for agent_name, agent_config in config.get("agents", {}).items():
        prompt_path = Path(agent_config["prompt"])
        if not prompt_path.exists():
            if prompt_yes_no(f"Create starter prompt at {prompt_path}?"):
                create_starter_prompt(agent_name, prompt_path)
                print(f"  ✓ Created {prompt_path}")

    # Create labels
    if prompt_yes_no("\nCreate/verify GitHub labels?"):
        repo = config.get("repo")
        if repo:
            # Agent labels
            for agent_name in config.get("agents", {}).keys():
                run_gh(
                    [
                        "label",
                        "create",
                        agent_name,
                        "--repo",
                        repo,
                        "--color",
                        "1D76DB",
                        "--force",
                    ]
                )

            # Priority labels
            priority_labels = [
                ("priority:high", "D93F0B", "Urgent - do first"),
                ("priority:medium", "FBCA04", "Normal priority"),
                ("priority:low", "0E8A16", "Nice to have"),
            ]
            for name, color, desc in priority_labels:
                run_gh(
                    [
                        "label",
                        "create",
                        name,
                        "--repo",
                        repo,
                        "--color",
                        color,
                        "--description",
                        desc,
                        "--force",
                    ]
                )

            # Status labels
            labels_config = config.get("labels", {})
            status_labels = [
                (labels_config.get("in_progress", "in-progress"), "5319E7"),
                (labels_config.get("blocked", "blocked"), "B60205"),
                (labels_config.get("needs_human", "needs-human"), "FBCA04"),
            ]
            for name, color in status_labels:
                run_gh(["label", "create", name, "--repo", repo, "--color", color, "--force"])

            print("  ✓ Labels created/updated")

    # Offer to create start script
    print("\n--- Start Script ---")
    default_script = Path("start-agents.sh")
    existing_scripts = list(Path.cwd().glob("start*.sh")) + list(Path.cwd().glob("run*.sh"))

    if existing_scripts:
        print("Found existing scripts:")
        for s in existing_scripts[:5]:
            print(f"  - {s.name}")

    if prompt_yes_no("Create a start script?", default=not existing_scripts):
        script_name = prompt_input("Script name", str(default_script))
        script_path = Path(script_name)

        if script_path.exists():
            if not prompt_yes_no(f"{script_path} exists. Overwrite?", default=False):
                print("  Skipped.")
            else:
                create_start_script(config, script_path)
                print(f"  ✓ Created {script_path}")
        else:
            create_start_script(config, script_path)
            print(f"  ✓ Created {script_path}")

        # Also offer fish version if user might use fish
        if prompt_yes_no("Also create fish shell version?", default=False):
            fish_path = script_path.with_suffix(".fish")
            create_start_script_fish(config, fish_path)
            print(f"  ✓ Created {fish_path}")

    print("\n" + "=" * 50)
    print("Setup complete! Next steps:")
    print("=" * 50)
    print("  1. Review/edit your prompt files")
    print("  2. Add agent labels to your GitHub issues")
    print("  3. Run: issue-orchestrator start")
    if existing_scripts or default_script.exists():
        print(f"     Or: ./{default_script.name if default_script.exists() else existing_scripts[0].name}")
    print()


if __name__ == "__main__":
    run_wizard()
