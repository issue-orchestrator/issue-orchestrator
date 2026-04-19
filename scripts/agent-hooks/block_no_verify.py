#!/usr/bin/env python3
# Managed by issue-orchestrator setup-guardrails: block-no-verify helper

"""Shared hook policy for blocking no-verify and restricted commands.

Also blocks the specific workarounds agents reach for when the
``coding-done`` dirty-tree guard rejects them: editing
``.git/info/exclude``, appending to ``.gitignore``, and marking tracked
files ``--assume-unchanged`` / ``--skip-worktree``. Each of these
*hides* dirtiness from the guard rather than resolving it; all four
were observed on live sessions (see #5949). Claude Code's native
sensitive-file gate blocks the ``.git/info/exclude`` edit interactively
and hangs the session for the full 90-minute timeout; this hook fires
before that gate so the agent gets a fail-fast rejection with a clear
next step instead.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
import sys
from pathlib import Path


@dataclass(frozen=True)
class HookDecision:
    """Decision for a hook evaluation."""

    allowed: bool
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allowed else 2


def extract_command_from_input(raw: str) -> str:
    """Extract the shell command from hook JSON input."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    cmd = ""

    tool_args = data.get("toolArgs")
    if isinstance(tool_args, str):
        try:
            args_data = json.loads(tool_args)
            if isinstance(args_data, dict):
                cmd = args_data.get("command", "")
        except (json.JSONDecodeError, TypeError):
            cmd = ""

    if not cmd:
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "")

    if not cmd:
        cmd = data.get("command", "")

    return cmd if isinstance(cmd, str) else ""


def _parse_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _is_git_push(argv: list[str]) -> bool:
    return len(argv) >= 2 and argv[0] == "git" and argv[1] == "push"


def is_dry_run_no_verify_push(command: str) -> bool:
    argv = _parse_argv(command)
    if not argv or not _is_git_push(argv):
        return False
    return "--dry-run" in argv and "--no-verify" in argv


def _allow_flag_present(start_dir: Path) -> bool:
    search_dir = start_dir
    while True:
        candidate = search_dir / ".issue-orchestrator" / "allow-no-verify-dry-run"
        if candidate.exists():
            return True
        if search_dir.parent == search_dir:
            return False
        search_dir = search_dir.parent


# Shared suffix appended to every dirty-tree-workaround rejection
# reason. Must tell the agent exactly what to do next — without this,
# a blocked agent just cycles through novel workarounds we haven't
# banned yet, which is the whole failure mode this hook exists to
# prevent. Treat as a load-bearing invariant; every new dirty-tree
# workaround pattern MUST use this suffix.
_DIRTY_WORKAROUND_SUFFIX = (
    " If the dirty tree is legitimately unresolvable, escalate with "
    "`coding-done needs_human --question ...`. Do NOT hide files."
)

# Static policy: dirty-tree-workaround patterns. Module-level so the
# regex compilation is amortised across calls and so the policy is
# visibly a single-source-of-truth list, not a per-call decision.
_DIRTY_WORKAROUND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ``.git/info/exclude`` and its linked-worktree form. Any mention
    # of this path in a bash command is an attempt to hide untracked
    # files from the dirty-tree guard; agents have no legitimate
    # reason to read or write it.
    (
        re.compile(r"\.git/(worktrees/[^/\s]+/)?info/exclude\b"),
        "BLOCKED: editing .git/info/exclude hides untracked files "
        "from the dirty-tree guard." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # Shell redirection (``>`` / ``>>``) to *any* ``.gitignore`` —
    # subdirectory forms and absolute paths included. Reading
    # (``cat``/``grep``/``less``/``head``) and observation commands
    # (``git check-ignore``/``ls-files``/``status``) remain allowed
    # because they don't match this pattern.
    (
        re.compile(r">>?\s*(\S*/)?\.gitignore\b"),
        "BLOCKED: writing to .gitignore to hide files is not "
        "allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``tee`` is the other common "append to file" shell idiom. Any
    # ``tee`` where ``.gitignore`` appears as an argument is a write
    # attempt regardless of flags (``-a``, ``--append``, or plain
    # overwrite).
    (
        re.compile(r"(?:^|[;&|\s])tee\b[^\n|;&]*\s(\S*/)?\.gitignore\b"),
        "BLOCKED: writing to .gitignore via `tee` to hide files is not "
        "allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``sed -i`` on ``.gitignore``, flag- and token-order tolerant.
    # GNU's bare ``-i``, BSD/macOS's ``-i ''``, and ``-i.bak`` backup-
    # suffix form all begin with the literal ``-i`` token. Agents also
    # commonly pass ``-e '<expr>'`` before ``-i``; the bounded lazy
    # match ``[^|;&\n]*?`` lets arbitrary intervening tokens appear
    # while staying inside a single shell command (not crossing pipes,
    # command separators, or newlines into unrelated commands).
    (
        re.compile(
            r"sed\b[^|;&\n]*?\s-i\S*[^|;&\n]*?\s(?:\S*/)?\.gitignore(?:\s|$)"
        ),
        "BLOCKED: editing .gitignore in place to hide files is "
        "not allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``git update-index --assume-unchanged`` / ``--skip-worktree``
    # mark tracked files invisible to ``git status`` without a commit.
    # Both are guard-hiding; ``git ls-files -v`` (observation) is
    # unaffected because it doesn't mutate the index.
    (
        re.compile(
            r"git\s+update-index\b[^\n]*(?:--assume-unchanged|--skip-worktree)(?:\s|$)"
        ),
        "BLOCKED: `git update-index --assume-unchanged` and "
        "`--skip-worktree` hide tracked files from the dirty-tree "
        "guard." + _DIRTY_WORKAROUND_SUFFIX,
    ),
]

_STATIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"git\s+(commit|push).*--no-verify"),
        "BLOCKED: --no-verify is forbidden. Pre-push hooks must run.",
    ),
    (re.compile(r"git\s+--no-verify"), "BLOCKED: --no-verify is forbidden."),
    (
        re.compile(r"git\s+commit.*\s-n\s"),
        "BLOCKED: -n (--no-verify) is forbidden.",
    ),
    (
        re.compile(r"git\s+-c\s+core\.hooksPath=/dev/null"),
        "BLOCKED: Disabling hooks via core.hooksPath is forbidden.",
    ),
    (
        re.compile(r"git\s+config\b[^\n]*\bcore\.hooksPath\b[^\n]*/dev/null\b"),
        "BLOCKED: Disabling hooks via core.hooksPath=/dev/null is forbidden.",
    ),
    (
        re.compile(r"gh\s+pr\s+merge"),
        "BLOCKED: Agents cannot merge PRs. Only humans can merge.",
    ),
    (
        re.compile(r"gh\s+api\s+.*pulls/[0-9]+/merge"),
        "BLOCKED: Agents cannot merge PRs via API. Only humans can merge.",
    ),
    *_DIRTY_WORKAROUND_PATTERNS,
]


def evaluate_command(command: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate a command string and return allow/deny decision.

    Blocks three categories of forbidden bash commands:

    1. Git-guardrail bypass: ``--no-verify``, ``core.hooksPath=/dev/null``.
    2. GitHub action boundary: ``gh pr merge``, merge via ``gh api``.
    3. ``coding-done`` dirty-tree-guard workarounds: edits to
       ``.git/info/exclude``, writes to ``.gitignore`` (shell
       redirection, ``tee``, or ``sed -i``), and
       ``git update-index --assume-unchanged`` / ``--skip-worktree``.

    Category 3 exists because Claude Code's native sensitive-file gate
    blocks the underlying edits *interactively* and silently hangs the
    session for 90 minutes; firing this hook first converts the hang
    into a fail-fast rejection with a clear next step (the shared
    escalation suffix — see ``_DIRTY_WORKAROUND_SUFFIX``).
    """
    if not command:
        return HookDecision(True, "")

    cwd = cwd or Path.cwd()

    if is_dry_run_no_verify_push(command) and _allow_flag_present(cwd):
        return HookDecision(True, "")

    for pattern, reason in _STATIC_PATTERNS:
        if pattern.search(command):
            return HookDecision(False, reason)

    return HookDecision(True, "")


def evaluate_raw_input(raw: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate raw hook JSON input and return allow/deny decision."""
    command = extract_command_from_input(raw)
    if raw and not command:
        return HookDecision(
            False,
            "BLOCKED: unable to extract command from hook input. Input may be malformed.",
        )
    return evaluate_command(command, cwd=cwd)


def format_cursor_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permission": "allow"})
    return json.dumps({"permission": "deny", "userMessage": decision.reason})


def format_copilot_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permissionDecision": "allow"})
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": decision.reason}
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("claude", "cursor", "gemini", "copilot"), required=True
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    decision = evaluate_raw_input(raw, cwd=Path.cwd())

    if args.mode in ("claude", "gemini"):
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
        return decision.exit_code

    if args.mode == "cursor":
        print(format_cursor_response(decision))
        return 0

    if args.mode == "copilot":
        print(format_copilot_response(decision))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
