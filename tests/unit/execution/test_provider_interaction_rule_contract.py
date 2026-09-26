"""The dog that didn't bark: rules that match the commands we actually build.

`builtin_session_interaction_rules` matched NOTHING for the command the codex
provider really emits, and had done since the isolated `CODEX_HOME` prefix was
introduced. Nothing failed. Nothing was logged. A rule that never fires is
indistinguishable from a rule with nothing to do, so the codex trust-worktree
prompt simply went unanswered forever and the only symptom was somebody else's
timeout.

Both sides of that boundary were tested in isolation and both were green:
the provider had tests for the argv it builds, and the matcher had tests for
the shapes it recognises — using hand-written command strings that nobody had
checked against the real ones. The gap was between them.

So these tests join the two: take the argv a provider ACTUALLY builds, hand it
to the matcher, and assert the rules come out. No hand-written command strings
here on purpose — a literal in this file could drift from the provider exactly
the way the old matcher tests did.
"""

from __future__ import annotations

import base64
import shlex
import tempfile
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.session_interactions import (
    builtin_session_interaction_rules,
)


@pytest.fixture
def working_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="rule-contract-"))


def _rule_names(command: list[str]) -> set[str]:
    return {rule.name for rule in builtin_session_interaction_rules(shlex.join(command))}


def test_the_interactive_codex_command_matches_its_trust_rule(
    working_directory: Path,
) -> None:
    """The regression itself.

    The provider runs codex through an isolated home:

        env CODEX_HOME=<runtime> codex ...

    which defeated the matcher twice over — a leading `env` COMMAND that was
    never trimmed, and an assignment whose value is a path, rejected by a
    guard that refused any token containing a slash.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )

    assert "codex-trust-worktree" in _rule_names(command), (
        "the codex trust prompt has no rule for the command we actually "
        f"build, so it will never be answered: {shlex.join(command)[:200]}"
    )


def test_the_codex_command_still_starts_with_the_env_prefix(
    working_directory: Path,
) -> None:
    """Pins the shape the test above exists to defend.

    If the provider stops wrapping codex in `env`, the assertion above starts
    passing for a reason unrelated to the matcher, and the regression it
    guards could return unnoticed under a different prefix.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )

    assert command[0] == "env"
    assert command[1].startswith("CODEX_HOME=")
    assert "/" in command[1], "the path-valued assignment is the tricky case"


def test_a_one_shot_exec_command_is_not_treated_as_interactive(
    working_directory: Path,
) -> None:
    """The rules are for the TUI. `codex exec` has no trust prompt to answer.

    Guards the other direction: a matcher loosened until it matches the real
    command must not start matching every codex invocation.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
        execution_mode="exec",
    )

    assert "codex-trust-worktree" not in _rule_names(command)


# The exact screen codex-cli 0.156.1 drew for an io reviewer on 2026-09-25
# (porchpin review-376, terminal recording at 1278 ms; the worktree path is
# anonymised). It sat unanswered until the review timed out 46 minutes later.
_CODEX_0_156_FOLDER_ACCESS_FRAME = base64.b64decode(
    "G1s/MjAyNmgbWzM5bRtbNDltG1swbRtbOTszSBtbPzI1aBtbPzIwMjZsG1s/MjAyNmgbWzM5bRtb"
    "NDltG1swbRtbOTszSBtbPzI1aBtbPzIwMjZsG1s/MjAyNmgbWzM5bRtbNDltG1swbRtbOTszSBtb"
    "PzI1aBtbPzIwMjZsG1s/MjAyNmgbWzM5bRtbNDltG1swbRtbOTszSBtbPzI1aBtbPzIwMjZsG1s/"
    "MjAyNmgbWzE7MUgbW0obWz8yNWwbWzI7MUgbWzFtICBGb2xkZXIgYWNjZXNzG1szOzNIG1syMm0b"
    "WzJtG1sybS90bXAvd29ya3RyZWUvcHJvamVjdC0zMjAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAbWzU7M0gbWzIybUNvbmZpZywb"
    "WzU7MTFIaG9va3MsG1s1OzE4SGFuZBtbNTsyMkhleGVjG1s1OzI3SHBvbGljaWVzG1s1OzM2SGZy"
    "b20bWzU7NDFIdW50cnVzdGVkG1s1OzUxSGZvbGRlcnMbWzU7NTlIc3RheRtbNTs2NEhkaXNhYmxl"
    "ZC4bWzU7NzRIVHJ1c3RlZBtbNTs4Mkhwcm9qZWN0G1s1OzkwSGZvbGRlcnMbWzU7OThIY2FuG1s1"
    "OzEwMkhzdGlsbBtbNTsxMDhIY29udHJpYnV0ZRtbNjszSHNldHRpbmdzLhtbNjsxM0hTa2lsbHMb"
    "WzY7MjBIc3RpbGwbWzY7MjZIbG9hZCwbWzY7MzJIYW5kG1s2OzM2SHRvb2xzG1s2OzQySGZvbGxv"
    "dxtbNjs0OUh5b3VyG1s2OzU0SHBlcm1pc3Npb24bWzY7NjVIc2V0dGluZ3MuG1s2Ozc1SE9wZW5p"
    "bmcbWzY7ODNId2lsbBtbNjs4OEhub3QbWzY7OTJIY2hhbmdlG1s2Ozk5SHNhdmVkG1s2OzEwNUh0"
    "cnVzdC4bWzg7MUgbWzdtG1sxbeKAuiAxLiBPcGVuIHJlc3RyaWN0ZWQgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgG1s5OzNIG1syN20bWzIybTIuG1s5OzZIUXVpdBtb"
    "MTE7M0gbWzFtZW50ZXIbWzIybRtbMm0bWzJtIGNvbnRpbnVlIMK3IBtbMjJtG1sxbWVzYxtbMjJt"
    "G1sybRtbMm0gcXVpdBtbMzltG1s0OW0bWzBt"
)



class _ManualTimers:
    """Timers the test fires by hand, so "the screen went quiet" is an event."""

    def __init__(self) -> None:
        self.live: list[list] = []

    def __call__(self, seconds, callback):
        timers = self
        entry = [seconds, callback, False]

        class _Timer:
            def start(self) -> None:
                timers.live.append(entry)

            def cancel(self) -> None:
                entry[2] = True

        return _Timer()

    def settle(self) -> None:
        for seconds, callback, cancelled in list(self.live):
            if not cancelled:
                callback()


def _codex_handler(working_directory: Path, timers: _ManualTimers):
    from issue_orchestrator.execution.session_interactions import (
        SessionInteractionHandler,
    )

    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )
    handler = SessionInteractionHandler(
        session_name="review-376",
        rules=builtin_session_interaction_rules(shlex.join(command)),
        timer_factory=timers,
    )
    sent: list[str] = []
    handler.bind_sender(lambda response: sent.append(response) or True)
    return handler, sent


def test_the_recorded_folder_access_screen_is_answered_once_it_settles(
    working_directory: Path,
) -> None:
    """#7287: Enter selects the highlighted "1. Open restricted", never "2. Quit".

    Not on the frame that drew it: codex 0.156.1 dropped an Enter sent then
    every time, and took one sent 0.3 s later every time.
    """
    timers = _ManualTimers()
    handler, sent = _codex_handler(working_directory, timers)

    handler.on_output(_CODEX_0_156_FOLDER_ACCESS_FRAME)

    assert sent == [], "answered while codex was still drawing the choice"
    timers.settle()
    assert sent == [""]
    assert handler.all_rules_fired, "one prompt variant must satisfy the startup wait"


def test_more_output_restarts_the_settle_wait(working_directory: Path) -> None:
    timers = _ManualTimers()
    handler, sent = _codex_handler(working_directory, timers)

    handler.on_output(_CODEX_0_156_FOLDER_ACCESS_FRAME)
    first = timers.live[-1]
    handler.on_output(b"\x1b[?2026h\x1b[?2026l")  # a pure redraw

    assert first[2], "the first wait must be cancelled by further output"
    timers.settle()
    assert sent == [""]


def test_managed_codex_never_checks_for_updates_at_startup(
    working_directory: Path,
) -> None:
    """A newer codex release opens on "Update available", whose Enter runs
    ``npm install -g``. An unattended session must never reach that screen,
    and no interaction rule may answer it.
    """
    command = CodexProvider().build_command(
        "review this",
        working_directory=working_directory,
        approval_mode="full-auto",
    )

    flag = command.index("check_for_update_on_startup=false")
    assert command[flag - 1] == "-c"
    for rule in builtin_session_interaction_rules(shlex.join(command)):
        markers = (rule.required_substrings, *rule.alternatives)
        assert not any("update" in m.casefold() for group in markers for m in group)
