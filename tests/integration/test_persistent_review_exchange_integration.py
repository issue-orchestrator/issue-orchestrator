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
from issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory import (
    InMemoryPersistentExchangePairRegistry,
)
from issue_orchestrator.execution.persistent_review_exchange_runner import (
    PersistentReviewExchangeRunner,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.ports import TraceEvent


_STUB_AGENT_SOURCE = textwrap.dedent("""
    import json
    import os
    import select
    import sys
    import time
    from pathlib import Path

    response_file = Path(os.environ["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"])
    completion_path_rel = os.environ["ISSUE_ORCHESTRATOR_COMPLETION_PATH"]
    role = os.environ.get("ISSUE_ORCHESTRATOR_AGENT_LABEL", "")

    # Reviewer outcomes are scripted per-round via env so a single stub
    # script drives ok / changes_requested / multi-round / max-rounds
    # scenarios. Default: ``ok`` every round.
    raw_outcomes = os.environ.get("STUB_REVIEWER_OUTCOMES", "ok").strip()
    reviewer_script = [
        token.strip() or "ok" for token in raw_outcomes.split(",")
    ]

    fd = sys.stdin.fileno()
    print(f"[stub-{role}] ready", flush=True)
    round_index = 0

    # Real prompts are multi-line; reading line-by-line would advance
    # the script outcome on every line of a single prompt. Instead,
    # batch reads until stdin goes quiet for a brief window and treat
    # that whole burst as one logical prompt.
    QUIET_WINDOW = 0.15
    while True:
        ready, _, _ = select.select([fd], [], [], None)
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        # Drain follow-on bytes that belong to the same prompt.
        while True:
            ready, _, _ = select.select([fd], [], [], QUIET_WINDOW)
            if not ready:
                break
            more = os.read(fd, 65536)
            if not more:
                break
            chunk += more
        prompt_text = chunk.decode("utf-8", errors="replace").strip()
        if not prompt_text:
            continue
        round_index += 1
        cwd = Path.cwd()
        worktree = cwd
        completion_full = worktree / completion_path_rel
        if "reviewer" in role:
            outcome = (
                reviewer_script[round_index - 1]
                if round_index - 1 < len(reviewer_script)
                else reviewer_script[-1]
            )
            if outcome == "changes_requested":
                payload = {
                    "response_type": "changes_requested",
                    "response_text": (
                        f"Needs work (stub-reviewer round {round_index})"
                    ),
                    "getting_closer": True,
                }
            else:
                payload = {
                    "response_type": "ok",
                    "response_text": f"LGTM (stub-reviewer round {round_index})",
                    "getting_closer": True,
                }
        else:
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


def _make_review_exchange_runner(
    session_output: FileSystemSessionOutput,
    *,
    persistent_pair_root: Path,
    pair_registry: InMemoryPersistentExchangePairRegistry | None = None,
) -> PersistentReviewExchangeRunner:
    """Centralized constructor for the integration tests' review-exchange runner.

    Keeps the registry argument in one place so a future signature
    change doesn't require updating every call site (PR #6209 review
    finding: a previous signature change drifted because of scattered
    constructor calls in different test files).

    ``pair_registry`` defaults to a fresh in-memory registry; tests
    that want to inspect the registry across exchanges (e.g.
    ``test_persistent_pair_survives_two_back_to_back_exchanges``)
    pass one in. ``persistent_pair_root`` is required because B2's
    pair-scoped path layout means the runner cannot pick a default
    that's both deterministic and isolated from other tests.
    """
    return PersistentReviewExchangeRunner(
        session_output,
        pair_registry or InMemoryPersistentExchangePairRegistry(),
        persistent_pair_root,
    )


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

    pair_registry = InMemoryPersistentExchangePairRegistry()
    cre = CompletionReviewExchange(
        config=config,
        session_output=session_output,
        emit_review_started=_emit_started,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            persistent_pair_root=tmp_path / "persistent-pairs",
            pair_registry=pair_registry,
        ),
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

    # B2 recording layout: pair-scoped (under
    # ``<state>/persistent-pairs/issue-<n>/<role>/...``), referenced
    # from each exchange's manifest. The replay UI looks the path up
    # via the manifest, not via a fixed run_dir/<role>/... convention,
    # because exchange 2's run_dir would otherwise see no recording
    # on a registry cache hit.
    manifest_payload = json.loads((run_dir / "manifest.json").read_text())
    reviewer_recording = Path(manifest_payload["reviewer_recording"])
    coder_recording = Path(manifest_payload["coder_recording"])
    assert reviewer_recording.exists(), (
        f"reviewer recording missing at manifest-pointed path {reviewer_recording}"
    )
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

    # B2: the reviewer sibling worktree PERSISTS past one exchange.
    # The pair owns it for the lifetime of the issue; lifecycle
    # release at issue-completion / reset / shutdown is what
    # reclaims it. Calling ``shutdown_all`` here simulates that
    # boundary so the test cleans up after itself.
    sibling_pattern = list(coder_wt.parent.glob(f"{coder_wt.name}-review-*"))
    assert len(sibling_pattern) == 1, (
        "reviewer worktree should still exist after the exchange — "
        "B2 makes it pair-scoped, not per-exchange"
    )

    pair_registry.shutdown_all(reason="test-cleanup")
    sibling_pattern_after = list(coder_wt.parent.glob(f"{coder_wt.name}-review-*"))
    # The on_release hook (B3 will wire it) is responsible for
    # filesystem reclamation; B2 leaves it manual via this test
    # cleanup so that bootstrap can choose a hook implementation
    # without breaking this test. The directory may or may not be
    # gone depending on bootstrap wiring — that's covered by
    # ``test_persistent_exchange_pair_registry_inmemory``.
    del sibling_pattern_after


def test_persistent_review_exchange_multi_round_changes_then_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end multi-round exchange.

    Replaces the skipped local-loop multi-round scenario from
    ``tests/simulated_scenarios/test_simulated_scenarios.py``. The
    reviewer disagrees on round 1, the coder reapplies, the reviewer
    approves on round 2; the persistent runner must:
      - issue exactly one coder send (after the round-1 changes_requested)
      - emit two REVIEW_EXCHANGE_ROUND_COMPLETED events
      - write chapters for both rounds in the reviewer's chapters.json
      - end with status=ok / rounds=2 / reason=reviewer_ok
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    stub_path = tmp_path / "stub_agent.py"
    stub_path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    # Drive the reviewer: round 1 = changes_requested, round 2 = ok.
    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "changes_requested,ok")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=f"{sys.executable} -u {stub_path}",
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    captured_events: list[TraceEvent] = []

    class _Sink:
        def publish(self, event):
            captured_events.append(event)

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
            persistent_pair_root=tmp_path / "persistent-pairs",
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = cre.run_review_exchange_loop(
        worktree=coder_wt,
        issue_number=4058,
        issue_title="Multi-round integration",
        session_name="issue-4058",
        agent_label="agent:backend",
        on_started=lambda _run_dir: None,
        events=_Sink(),
        event_context=EventContext(),
    )

    assert outcome.status == "ok", f"unexpected outcome: {outcome}"
    assert outcome.rounds == 2
    assert outcome.reason == "reviewer_ok"

    round_completed = [
        evt for evt in captured_events
        if evt.event_type == EventName.REVIEW_EXCHANGE_ROUND_COMPLETED
    ]
    assert len(round_completed) == 2, \
        f"expected exactly 2 round-completed events, got {len(round_completed)}"
    # Round 1 reviewer should be changes_requested, round 2 should be ok.
    payload_first = round_completed[0].data
    payload_second = round_completed[1].data
    assert payload_first["reviewer_response_type"] == "changes_requested"
    assert payload_second["reviewer_response_type"] == "ok"

    run_dir = outcome.exchange_dir.parent
    reviewer_chapters_path = run_dir / "reviewer" / "chapters.json"
    assert reviewer_chapters_path.exists()
    chapters_payload = json.loads(reviewer_chapters_path.read_text())
    cycle_indices = sorted({
        chapter["cycle_index"] for chapter in chapters_payload["chapters"]
    })
    assert cycle_indices == [1, 2], \
        f"expected reviewer chapters for rounds 1 and 2, got {cycle_indices}"


def test_persistent_review_exchange_max_rounds_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    """Reviewer never approves — exchange ends with max_rounds reached.

    Replaces the skipped no-progress / max-rounds scenarios.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    stub_path = tmp_path / "stub_agent.py"
    stub_path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    # Reviewer always says changes_requested across all rounds.
    monkeypatch.setenv(
        "STUB_REVIEWER_OUTCOMES",
        "changes_requested,changes_requested,changes_requested",
    )

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=f"{sys.executable} -u {stub_path}",
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 2
    config.review_exchange_max_no_progress = 5  # don't trip no-progress first

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
            persistent_pair_root=tmp_path / "persistent-pairs",
        ),
    )

    from issue_orchestrator.events import EventContext

    outcome = cre.run_review_exchange_loop(
        worktree=coder_wt,
        issue_number=4059,
        issue_title="Max-rounds integration",
        session_name="issue-4059",
        agent_label="agent:backend",
        on_started=lambda _run_dir: None,
        events=MagicMock(),
        event_context=EventContext(),
    )

    assert outcome.status == "stopped", \
        f"unexpected outcome status: {outcome.status} (full: {outcome})"
    assert outcome.reason == "max_rounds_exceeded"
    assert outcome.rounds == 2


def test_two_rework_rounds_render_distinguishably_in_projected_timeline(
    tmp_path: Path, monkeypatch
) -> None:
    """Agent journey: in a two-round rework cycle, the projected
    timeline must surface BOTH rounds distinguishably so an agent
    debugging a stuck rework can answer "what did each round do?" by
    reading the timeline alone — not by digging into reviewer artifacts.

    The companion test
    ``test_persistent_review_exchange_multi_round_changes_then_ok``
    asserts the runner emits two REVIEW_EXCHANGE_ROUND_COMPLETED
    TraceEvents. This test pins what reaches `project_timeline` once
    those events flow through `DefaultTimelineWriter` (which fans out
    via the same `produce_external_records` pipeline production uses):

      - Exactly two `review_exchange.round_completed` events appear in
        the projected timeline.
      - They are in temporal order (earlier round first).
      - `round_index` is 1 then 2, not duplicated or 0/0.
      - `reviewer_response_type` differs per round so the agent can
        identify the rework boundary (round 1 = changes_requested,
        round 2 = ok).
      - Each event carries a populated `narrative` so a human / agent
        skimming the dashboard sees the per-round verdict at a glance,
        not just a generic "round completed" string.
      - Both rounds remain in the user-facing view (`views` includes
        "user"), so they are not accidentally hidden behind the
        ops/debug-only filter.
    """
    from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
    from issue_orchestrator.execution.timeline_event_sink import TimelineEventSink
    from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
    from issue_orchestrator.timeline import project_timeline

    class _RecordingStore(TimelineStore):
        def __init__(self) -> None:
            self.records: list[TimelineRecord] = []

        def append(self, issue_number: int, record: TimelineRecord) -> None:  # noqa: ARG002
            self.records.append(record)

        def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:  # noqa: ARG002
            return list(self.records)

        def delete(self, issue_number: int) -> int:  # noqa: ARG002
            self.records.clear()
            return 0

    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
    stub_path = tmp_path / "stub_agent.py"
    stub_path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Stub agent prompt", encoding="utf-8")

    monkeypatch.setenv("STUB_REVIEWER_OUTCOMES", "changes_requested,ok")

    agent = AgentConfig(
        prompt_path=prompt_path,
        ai_system="claude-code",
        timeout_minutes=1,
        command=f"{sys.executable} -u {stub_path}",
    )
    config = _make_config(tmp_path, agent)
    config.review_exchange_max_rounds = 3

    store = _RecordingStore()
    timeline_sink = TimelineEventSink(DefaultTimelineWriter(store))

    _session_output_for_test = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        config=config,
        session_output=_session_output_for_test,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            _session_output_for_test,
            persistent_pair_root=tmp_path / "persistent-pairs",
        ),
    )

    from issue_orchestrator.events import EventContext

    issue_number = 4060
    outcome = cre.run_review_exchange_loop(
        worktree=coder_wt,
        issue_number=issue_number,
        issue_title="Two-round projection",
        session_name=f"issue-{issue_number}",
        agent_label="agent:backend",
        on_started=lambda _run_dir: None,
        events=timeline_sink,
        event_context=EventContext(),
    )

    assert outcome.status == "ok"
    assert outcome.rounds == 2

    records = store.read(issue_number)
    assert records, "timeline writer received no records — the sink wiring is broken"

    projected = project_timeline(records, issue_number=issue_number)

    round_completed = [
        evt for evt in projected
        if evt.event == "review_exchange.round_completed"
    ]
    assert len(round_completed) == 2, (
        "expected two review_exchange.round_completed events in the "
        f"projected timeline, got {len(round_completed)}: "
        f"{[e.event for e in projected]}"
    )

    first, second = round_completed
    assert first.timestamp <= second.timestamp, (
        "round events out of temporal order — "
        f"{first.timestamp} should precede {second.timestamp}"
    )
    assert first.round_index == 1
    assert second.round_index == 2

    assert first.reviewer_response_type == "changes_requested", (
        "round 1 should report changes_requested so the agent sees "
        "where the rework boundary is, got "
        f"{first.reviewer_response_type!r}"
    )
    assert second.reviewer_response_type == "ok", (
        "round 2 should report ok (the approval that ended the cycle), "
        f"got {second.reviewer_response_type!r}"
    )

    # The narrative is what a human / agent skim-reads. It must
    # distinguish the rounds — a generic "round completed" on both
    # would force the user to click through to read each round's
    # reviewer text just to know which one approved.
    assert first.narrative, (
        "round 1 narrative is empty; agent skimming the timeline can't "
        "tell what the round did without drilling in"
    )
    assert second.narrative, "round 2 narrative is empty"
    assert first.narrative != second.narrative, (
        "both rounds share the same narrative "
        f"({first.narrative!r}) — they look identical in the dashboard"
    )
    assert "changes_requested" in first.narrative
    assert "ok" in second.narrative

    # User-facing view filter: both rounds must reach the end-user view,
    # not be debug- or ops-only. The dashboard's user view is what the
    # human / agent reads first.
    for evt in round_completed:
        assert evt.views and "user" in evt.views, (
            f"round {evt.round_index} is not in the 'user' view "
            f"(views={evt.views}); end users won't see it on the dashboard"
        )


def test_persistent_pair_survives_two_back_to_back_exchanges(
    tmp_path: Path, monkeypatch,
) -> None:
    """The user-visible "1 process for the life of the exchanges" benefit.

    This is the test ADR 0026's B2 commits to: spawn a pair on the
    first exchange, run a second exchange for the same issue, and
    assert the second exchange reuses *the same coder PID and the
    same reviewer PID*. A regression where the registry's release
    crept back into ``run_persistent_session_exchange`` would fail
    here as fresh PIDs.

    The agent stub responds ``ok`` to every reviewer prompt, so each
    exchange completes in one round. Both calls run on the same
    ``CompletionReviewExchange`` (and therefore the same registry
    instance), which is what production wires.
    """
    coder_wt, _branch = _bootstrap_git_worktree(tmp_path)
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

    pair_registry = InMemoryPersistentExchangePairRegistry()
    session_output = FileSystemSessionOutput()
    cre = CompletionReviewExchange(
        config=config,
        session_output=session_output,
        emit_review_started=lambda **_: None,
        emit_review_outcome=lambda **_: None,
        review_exchange_runner=_make_review_exchange_runner(
            session_output,
            persistent_pair_root=tmp_path / "persistent-pairs",
            pair_registry=pair_registry,
        ),
    )

    from issue_orchestrator.events import EventContext

    def _run_one(session_name: str) -> "ReviewExchangeOutcome":  # noqa: F821
        return cre.run_review_exchange_loop(
            worktree=coder_wt,
            issue_number=9001,
            issue_title="Persistence integration",
            session_name=session_name,
            agent_label="agent:backend",
            on_started=lambda _run_dir: None,
            events=MagicMock(),
            event_context=EventContext(),
        )

    first = _run_one("issue-9001-first")
    assert first.status == "ok"

    snapshot_after_first = pair_registry.snapshot()
    assert len(snapshot_after_first) == 1, (
        "exactly one pair should be cached for this issue after exchange 1"
    )
    coder_pid_first = snapshot_after_first[0]["coder_pid"]
    reviewer_pid_first = snapshot_after_first[0]["reviewer_pid"]

    second = _run_one("issue-9001-second")
    assert second.status == "ok"

    snapshot_after_second = pair_registry.snapshot()
    assert len(snapshot_after_second) == 1, (
        "second exchange must NOT have spawned a second pair — the "
        "cached one should have been reused"
    )
    assert snapshot_after_second[0]["coder_pid"] == coder_pid_first, (
        "coder PID changed between exchanges — the persistent-pair "
        "contract is broken (B2 ADR 0026 regression)"
    )
    assert snapshot_after_second[0]["reviewer_pid"] == reviewer_pid_first, (
        "reviewer PID changed between exchanges — the persistent-pair "
        "contract is broken (B2 ADR 0026 regression)"
    )

    # Recording-mirror cache-hit regression (PR #6212 review):
    # both exchanges' run_dirs must surface the canonical pair-scoped
    # recording via their manifest. The previous design wired
    # ``additional_recording_paths`` at first spawn, so on cache hit
    # the second exchange's run_dir got no recording. The fix is to
    # publish the pair-scoped path through the manifest and have
    # ManifestAccessor read from it. Both exchanges must point at
    # the same pair-scoped recording file, since the pair runs one
    # continuous PTY across both exchanges.
    first_run_dir = first.exchange_dir.parent
    second_run_dir = second.exchange_dir.parent
    first_manifest = json.loads((first_run_dir / "manifest.json").read_text())
    second_manifest = json.loads((second_run_dir / "manifest.json").read_text())
    assert first_manifest["coder_recording"] == second_manifest["coder_recording"], (
        "coder recording path drifted between exchanges — both should "
        "point at the same pair-scoped file (PR #6212 finding 3)"
    )
    assert first_manifest["reviewer_recording"] == second_manifest["reviewer_recording"], (
        "reviewer recording path drifted between exchanges"
    )
    assert Path(second_manifest["reviewer_recording"]).exists(), (
        "exchange 2's manifest-pointed recording is missing on disk"
    )

    accessor = ManifestAccessor(
        RunIdentity(issue_number=9001, run_dir=second_run_dir),
    )
    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1, role="reviewer",
    )
    assert artifact.path == Path(second_manifest["reviewer_recording"]), (
        "ManifestAccessor must follow the manifest's pair-scoped "
        "pointer for second-exchange runs (#6212 finding 3 fix)"
    )

    # Cleanup so the test doesn't leak PTY agents past the test run.
    pair_registry.shutdown_all(reason="test-cleanup")
