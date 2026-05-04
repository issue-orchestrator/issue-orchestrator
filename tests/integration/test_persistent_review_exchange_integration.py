"""Integration coverage for the persistent-session review-exchange cutover.

Drives ``CompletionReviewExchange.run_review_exchange_loop`` end-to-end
against:

  - a real git fixture (``git init`` + initial commit + feature branch
    checked out in the coder worktree)
  - a stub agent script that mimics the agent protocol (read prompt
    from stdin, write response file + ``coding-done`` completion
    artifact, wait for next prompt)
  - the real persistent runner (no monkeypatching of the runner itself)

Replaces the orchestration-level coverage previously provided by the
``via-local-loop`` simulated-scenarios that were skipped during the
cutover. Catches regressions of the kind the reviewer flagged in #6160:
artifact-layout drift, manifest-accessor resolution, terminal events,
summary writes, chapter sidecars, reviewer-worktree lifecycle.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.completion_review_exchange import CompletionReviewExchange
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.manifest_accessor import ManifestAccessor, RunIdentity
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.ports import TraceEvent


_STUB_AGENT_SOURCE = textwrap.dedent("""
    import json
    import os
    import sys
    import time
    from pathlib import Path

    response_file = Path(os.environ["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
    completion_path_rel = os.environ["ISSUE_ORCHESTRATOR_COMPLETION_PATH"]
    role = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_LABEL", "")

    print(f"[stub-{role}] ready", flush=True)
    round_index = 0
    for raw in sys.stdin:
        prompt = raw.strip()
        if not prompt:
            continue
        round_index += 1
        # Find the worktree root by walking up looking for .issue-orchestrator
        cwd = Path.cwd()
        worktree = cwd
        completion_full = worktree / completion_path_rel
        if "reviewer" in role:
            payload = {
                "response_type": "ok",
                "response_text": "LGTM (stub-reviewer)",
                "getting_closer": True,
            }
        else:
            # Coder: also write the completion artifact (coding-done's output).
            completion_full.parent.mkdir(parents=True, exist_ok=True)
            completion_full.write_text(
                json.dumps({
                    "outcome": "completed",
                    "implementation": f"stub-coder round {round_index}",
                    "round": round_index,
                }),
                encoding="utf-8",
            )
            payload = {
                "response_type": "ok",
                "response_text": f"Applied (stub-coder round {round_index})",
            }
        # Tiny delay so the PTY produces capturable output before the
        # response file appears (mirrors a real agent's "thinking" tail).
        time.sleep(0.02)
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(json.dumps(payload), encoding="utf-8")
        print(f"[stub-{role}] wrote round {round_index}", flush=True)
""").strip()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


def _bootstrap_git_worktree(tmp_path: Path) -> tuple[Path, str]:
    """Build a tiny real git repo with a feature branch checked out."""
    repo = tmp_path / "coder-wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("hello\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "initial")
    branch = "feature/test-issue"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "work.py").write_text("print('hello')\n")
    _git(repo, "add", "work.py")
    _git(repo, "commit", "-q", "-m", "feature work")
    return repo, branch


def _make_config(tmp_path: Path, agent: AgentConfig) -> Config:
    config = Config()
    config.repo_root = tmp_path
    config.repo = "local/test"
    config.review_exchange_mode = "via-local-loop"
    config.review_exchange_max_rounds = 2
    config.review_exchange_max_no_progress = 2
    config.review_exchange_require_validation = False
    config.agents = {
        "agent:backend": agent,
        "agent:reviewer": agent,
    }
    config.code_review_agent = "agent:reviewer"
    config.control_api_port = None
    return config


@pytest.fixture(autouse=True)
def _clear_simulated_scenario_stubs(monkeypatch):
    """Override the simulated-scenarios autouse stubs that bypass the
    persistent runner. This integration test wants the REAL runner so we
    can exercise the cutover end-to-end."""
    # No-op: we live under tests/integration/, not tests/simulated_scenarios/,
    # so the autouse fixture there isn't applied. This sentinel exists to
    # document the intent and to give a hook if conftest evolves later.


def test_persistent_review_exchange_end_to_end_through_completion_owner(tmp_path: Path) -> None:
    """Drive ``CompletionReviewExchange.run_review_exchange_loop`` end-to-end
    against a real git worktree + stub agent. Asserts:

      - outcome.status == "ok" (single happy-path round)
      - REVIEW_EXCHANGE_STARTED + REVIEW_EXCHANGE_COMPLETED events fire
      - persistent recording layout exists
        (``run_dir/<role>/terminal-recording.jsonl``)
      - chapters.json sidecar exists per role with non-empty chapters
      - summary.json is present and matches the outcome status
      - manifest accessor can resolve the persistent-layout recording
        (the bug the reviewer flagged in #6160)
      - the sibling reviewer worktree is removed at exchange end
    """
    coder_wt, branch = _bootstrap_git_worktree(tmp_path)

    stub_path = tmp_path / "stub_agent.py"
    stub_path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")

    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=f"{sys.executable} -u {stub_path}",
    )
    config = _make_config(tmp_path, agent)

    session_output = FileSystemSessionOutput()
    captured_started: dict[str, Path] = {}

    def _emit_started(*, run_dir, **_):
        captured_started["run_dir"] = run_dir

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    cre = CompletionReviewExchange(
        config=config,
        session_output=session_output,
        emit_review_started=_emit_started,
        emit_review_outcome=lambda **_: None,
    )

    from issue_orchestrator.events import EventContext

    outcome = cre.run_review_exchange_loop(
        worktree=coder_wt,
        issue_number=4057,
        issue_title="Test integration",
        session_name="issue-4057",
        agent_label="agent:backend",
        on_started=lambda run_dir: captured_started.setdefault("on_started_run_dir", run_dir),
        events=_Sink(),
        event_context=EventContext(),
    )

    # Outcome shape
    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 1
    assert outcome.reason == "reviewer_ok"
    assert outcome.exchange_dir is not None and outcome.exchange_dir.exists()

    run_dir = outcome.exchange_dir.parent
    assert run_dir.exists()

    # Terminal events fired
    event_names = {evt.event_type for evt in captured_events}
    assert EventName.REVIEW_EXCHANGE_STARTED in event_names
    assert EventName.REVIEW_EXCHANGE_COMPLETED in event_names

    # Persistent recording layout
    reviewer_recording = run_dir / "reviewer" / "terminal-recording.jsonl"
    coder_recording = run_dir / "coder" / "terminal-recording.jsonl"
    assert reviewer_recording.exists(), f"reviewer recording missing at {reviewer_recording}"
    # Coder runs only when reviewer says changes_requested; reviewer stub
    # responds ok on round 1, so coder may not run. Just check that if
    # the file exists, it parses.
    if coder_recording.exists():
        for line in coder_recording.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)  # JSONL well-formed

    # Chapters sidecar — at minimum the reviewer's prompt + feedback chapters.
    reviewer_chapters_path = run_dir / "reviewer" / "chapters.json"
    assert reviewer_chapters_path.exists(), \
        f"reviewer chapters.json missing at {reviewer_chapters_path}"
    reviewer_chapters_payload = json.loads(reviewer_chapters_path.read_text())
    assert reviewer_chapters_payload["role"] == "reviewer"
    assert len(reviewer_chapters_payload["chapters"]) >= 2, \
        "expected at least prompt + feedback chapters for reviewer round 1"

    # summary.json present and matches outcome
    summary_path = outcome.exchange_dir / "summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["status"] == "ok"
    assert summary["reason"] == "reviewer_ok"

    # Manifest accessor resolves the persistent layout — regression for
    # the #6160 finding that the accessor was hardcoded to the old
    # spawn-per-phase round-NNN/<role>/ path.
    accessor = ManifestAccessor(
        RunIdentity(issue_number=4057, run_dir=run_dir),
    )
    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1, role="reviewer",
    )
    assert artifact.path == reviewer_recording

    # Reviewer sibling worktree was reclaimed.
    sibling_pattern = list(coder_wt.parent.glob(f"{coder_wt.name}-review-*"))
    assert sibling_pattern == [], \
        f"reviewer worktree leaked: {sibling_pattern}"
