from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config

from .conftest import SCRIPTS_DIR, build_config, build_orchestrator, run_until


def script(name: str, *, prompt: bool = False) -> str:
    command = f"bash {SCRIPTS_DIR / name}"
    if prompt:
        return f"{command} {{prompt}}"
    return command


@dataclass(frozen=True)
class ScenarioContext:
    orch: object
    repo_host: object
    events: object
    timeline_reader: object
    config: object
    runner: object
    repo_root: Path
    issue_number: int
    event_baseline: int
    timeline_baseline: int

    @property
    def worktree(self) -> Path | None:
        history = getattr(self.orch.state, "session_history", [])
        if not history:
            active = getattr(self.orch.state, "active_sessions", [])
            if active:
                worktree = getattr(active[0], "worktree_path", None)
                if worktree:
                    return Path(worktree)
            pending = getattr(self.orch.state, "pending_validation_retries", [])
            if pending:
                return Path(pending[0].worktree_path)
            return None
        return history[0].worktree_path

    def events_since_baseline(self) -> list:
        return list(self.events.events[self.event_baseline:])

    def timeline_since_baseline(self) -> list:
        stream = self.timeline_reader.read(self.issue_number)
        return list(stream.events[self.timeline_baseline:])

    def restart(self) -> "ScenarioContext":
        orch, repo_host, events, timeline_reader = build_orchestrator(
            self.repo_root,
            list(self.repo_host.issues),
            self.config,
            repo_host=self.repo_host,
            runner=self.runner,
        )
        return ScenarioContext(
            orch=orch,
            repo_host=repo_host,
            events=events,
            timeline_reader=timeline_reader,
            config=self.config,
            runner=self.runner,
            repo_root=self.repo_root,
            issue_number=self.issue_number,
            event_baseline=len(events.events),
            timeline_baseline=len(timeline_reader.read(self.issue_number).events),
        )


Expectation = Callable[[ScenarioContext], None]


@dataclass
class Scenario:
    name: str
    repo_root: Path
    issue_number: int = 1
    issue_title: str = "Simulated scenario issue"
    issue_labels: list[str] = field(default_factory=lambda: ["simulated-scenario", "agent:coder"])

    coder_command: str | None = None
    reviewer_command: str | None = None
    validation_cmd: str | None = None
    review_exchange_mode: str = "via-local-loop"
    review_exchange_require_validation: bool = False
    review_exchange_max_rounds: int = 5
    review_exchange_max_no_progress: int = 2
    max_validation_retries: int = 0
    max_ticks: int = 6
    reconcile: bool = False
    fresh_labels: dict[int, set[str]] | None = None

    _expectations: list[Expectation] = field(default_factory=list, init=False)
    _run_predicate: Callable[[object], bool] | None = field(default=None, init=False)
    _wait_for_events: set[EventName] = field(default_factory=set, init=False)
    _expected_events: set[EventName] = field(default_factory=set, init=False)
    _expected_timeline_events: set[EventName] = field(default_factory=set, init=False)
    _config_overrides: list[Callable[[Config], None]] = field(default_factory=list, init=False)
    _repo_host_mutator: Callable[[object], None] | None = field(default=None, init=False)
    _working_copy_override: object | None = field(default=None, init=False)
    _lease_renewer_override: object | None = field(default=None, init=False)
    _runner_override: object | None = field(default=None, init=False)

    def issue(self, *, number: int | None = None, title: str | None = None, labels: list[str] | None = None) -> Scenario:
        if number is not None:
            self.issue_number = number
        if title is not None:
            self.issue_title = title
        if labels is not None:
            self.issue_labels = labels
        return self

    def coder(self, command: str) -> Scenario:
        self.coder_command = command
        return self

    def reviewer(self, command: str) -> Scenario:
        self.reviewer_command = command
        return self

    def validation(self, *, cmd: str | None, max_retries: int | None = None) -> Scenario:
        self.validation_cmd = cmd
        if max_retries is not None:
            self.max_validation_retries = max_retries
        return self

    def reconciliation(self, *, enabled: bool = True, fresh_labels: dict[int, set[str]] | None = None) -> Scenario:
        self.reconcile = enabled
        if fresh_labels is not None:
            self.fresh_labels = fresh_labels
        return self

    def review_exchange(
        self,
        *,
        mode: str | None = None,
        require_validation: bool | None = None,
        max_rounds: int | None = None,
        max_no_progress: int | None = None,
    ) -> Scenario:
        if mode is not None:
            self.review_exchange_mode = mode
        if require_validation is not None:
            self.review_exchange_require_validation = require_validation
        if max_rounds is not None:
            self.review_exchange_max_rounds = max_rounds
        if max_no_progress is not None:
            self.review_exchange_max_no_progress = max_no_progress
        return self

    def configure(self, mutator: Callable[[Config], None]) -> Scenario:
        self._config_overrides.append(mutator)
        return self

    def configure_repo_host(self, mutator: Callable[[object], None]) -> Scenario:
        self._repo_host_mutator = mutator
        return self

    def use_working_copy(self, working_copy: object) -> Scenario:
        self._working_copy_override = working_copy
        return self

    def use_lease_renewer(self, lease_renewer: object) -> Scenario:
        self._lease_renewer_override = lease_renewer
        return self

    def use_runner(self, runner: object) -> Scenario:
        self._runner_override = runner
        return self

    def wait_for(self, predicate: Callable[[object], bool], *, max_ticks: int | None = None) -> Scenario:
        self._run_predicate = predicate
        if max_ticks is not None:
            self.max_ticks = max_ticks
        return self

    def wait_for_event(self, name: EventName) -> Scenario:
        self._wait_for_events.add(name)
        return self

    def expect_pr(self, *, created: bool = True, draft: bool | None = None, number: int = 100) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            pr = ctx.repo_host.get_pr(number)
            if created:
                assert pr is not None
                if draft is not None:
                    assert pr.draft is draft
            else:
                assert pr is None
        return self._add_expectation(_assert)

    def expect_event(self, name: EventName) -> Scenario:
        self._expected_events.add(name)
        def _assert(ctx: ScenarioContext) -> None:
            assert any(e.name == name for e in ctx.events_since_baseline())
        return self._add_expectation(_assert)

    def expect_timeline_event(self, name: EventName) -> Scenario:
        self._expected_timeline_events.add(name)
        def _assert(ctx: ScenarioContext) -> None:
            assert any(
                (e.source_event or e.event) == name.value
                for e in ctx.timeline_since_baseline()
            )
        return self._add_expectation(_assert)

    def expect_latest_timeline_event(
        self,
        name: EventName,
        *,
        predicate: Callable[[object], bool] | None = None,
    ) -> Scenario:
        self._expected_timeline_events.add(name)
        def _assert(ctx: ScenarioContext) -> None:
            latest = _latest_timeline_event(ctx.timeline_since_baseline(), {name.value})
            assert latest is not None, f"Expected latest timeline event {name} not found"
            if predicate is not None:
                assert predicate(latest)
        return self._add_expectation(_assert)


    def expect_no_event(self, name: EventName) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert all(e.name != name for e in ctx.events_since_baseline())
        return self._add_expectation(_assert)

    def expect_no_timeline_event(self, name: EventName) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert all(
                (e.source_event or e.event) != name.value
                for e in ctx.timeline_since_baseline()
            ), f"Unexpected timeline event {name.value} found"
        return self._add_expectation(_assert)

    def expect_latest_event(
        self,
        name: EventName,
        *,
        predicate: Callable[[dict], bool] | None = None,
    ) -> Scenario:
        self._expected_events.add(name)
        def _assert(ctx: ScenarioContext) -> None:
            latest = _latest_event(ctx.events_since_baseline(), {name})
            assert latest is not None, f"Expected latest event {name} not found"
            assert latest.name == name
            if predicate is not None:
                assert predicate(latest.data)
        return self._add_expectation(_assert)

    def expect_review_exchange_reason(self, reason: str) -> Scenario:
        """Assert ``REVIEW_EXCHANGE_COMPLETED.reason`` matches ``reason``.

        Lenient: only asserts the event side. Cache-hit / draft-PR
        paths complete without the runner writing ``summary.json``,
        so this expectation does not require one. Use
        :meth:`expect_review_exchange_terminal_state` for scenarios
        where the summary is mandatory (e.g. error termination).
        """
        def _assert(ctx: ScenarioContext) -> None:
            payload = _latest_event_payload(ctx.events_since_baseline(), EventName.REVIEW_EXCHANGE_COMPLETED)
            assert payload is not None
            assert payload.get("reason") == reason
        return self._add_expectation(_assert)

    def expect_review_exchange_status(self, status: str) -> Scenario:
        """Assert ``REVIEW_EXCHANGE_COMPLETED.status`` matches ``status``.

        Lenient counterpart to :meth:`expect_review_exchange_reason`.
        For scenarios that must verify the summary contract, use
        :meth:`expect_review_exchange_terminal_state`.
        """
        def _assert(ctx: ScenarioContext) -> None:
            payload = _latest_event_payload(ctx.events_since_baseline(), EventName.REVIEW_EXCHANGE_COMPLETED)
            assert payload is not None
            assert payload.get("status") == status
        return self._add_expectation(_assert)

    def expect_review_exchange_terminal_state(
        self, *, status: str, reason: str,
    ) -> Scenario:
        """Strict terminal-contract check: event AND ``summary.json`` agree.

        Fails if no ``summary.json`` was written. The runner's contract
        is that every exchange that reaches a terminal state writes a
        summary whose status/reason match the
        ``REVIEW_EXCHANGE_COMPLETED`` payload. Error scenarios use this
        so a regression that drops the summary would surface here
        instead of silently passing.
        """
        def _assert(ctx: ScenarioContext) -> None:
            payload = _latest_event_payload(ctx.events_since_baseline(), EventName.REVIEW_EXCHANGE_COMPLETED)
            assert payload is not None, "REVIEW_EXCHANGE_COMPLETED event missing"
            assert payload.get("status") == status, (
                f"event status {payload.get('status')!r} != expected {status!r}"
            )
            assert payload.get("reason") == reason, (
                f"event reason {payload.get('reason')!r} != expected {reason!r}"
            )
            worktree = ctx.worktree
            assert worktree is not None
            summary = _latest_review_exchange_summary(worktree)
            assert summary is not None, "review-exchange summary.json missing"
            assert summary.get("status") == status, (
                f"summary.json status {summary.get('status')!r} != "
                f"event status {status!r}"
            )
            assert summary.get("reason") == reason, (
                f"summary.json reason {summary.get('reason')!r} != "
                f"event reason {reason!r}"
            )
        return self._add_expectation(_assert)

    def expect_review_exchange_round_response(
        self,
        *,
        reviewer_response_type: str | None = None,
        coder_response_type: str | None = None,
    ) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            payloads = _event_payloads(ctx.events_since_baseline(), EventName.REVIEW_EXCHANGE_ROUND_COMPLETED)
            assert payloads
            latest = payloads[-1]
            if reviewer_response_type is not None:
                assert latest.get("reviewer_response_type") == reviewer_response_type
            if coder_response_type is not None:
                assert latest.get("coder_response_type") == coder_response_type
        return self._add_expectation(_assert)

    def expect_review_exchange_rounds(self, expected_max: int) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            worktree = ctx.worktree
            assert worktree is not None
            rounds = _summary_rounds(worktree)
            assert rounds, "review-exchange summary missing completed_rounds"
            assert max(rounds) == expected_max
        return self._add_expectation(_assert)

    def expect_validation_status(self, status: str) -> Scenario:
        status_map = {
            "passed": EventName.SESSION_VALIDATION_PASSED,
            "failed": EventName.SESSION_VALIDATION_FAILED,
            "retry": EventName.SESSION_VALIDATION_RETRY_NEEDED,
        }
        if status not in status_map:
            raise AssertionError(f"Unknown validation status: {status}")

        def _assert(ctx: ScenarioContext) -> None:
            last = _latest_event(
                ctx.events_since_baseline(),
                {
                    EventName.SESSION_VALIDATION_PASSED,
                    EventName.SESSION_VALIDATION_FAILED,
                    EventName.SESSION_VALIDATION_RETRY_NEEDED,
                },
            )
            assert last is not None, "validation result event not emitted"
            assert last.name == status_map[status]
        return self._add_expectation(_assert)

    def expect_validation_result(self, passed: bool) -> Scenario:
        return self.expect_validation_status("passed" if passed else "failed")

    def expect_validation_artifacts(self, passed: bool, *, exit_code: int | None = None, timed_out: bool | None = None) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            worktree = ctx.worktree
            assert worktree is not None
            record_path = _latest_validation_record(worktree)
            assert record_path is not None, "validation-record.json not found"
            record = json.loads(record_path.read_text())
            assert record.get("passed") is passed
            if exit_code is not None:
                assert record.get("exit_code") == exit_code
            else:
                if passed:
                    assert record.get("exit_code") == 0
                else:
                    assert record.get("exit_code") != 0 or record.get("timed_out") is True
            if timed_out is not None:
                assert record.get("timed_out") is timed_out
            stdout_path = record.get("stdout_path")
            stderr_path = record.get("stderr_path")
            assert stdout_path, "validation stdout_path missing"
            assert stderr_path, "validation stderr_path missing"
            stdout_file = (worktree / stdout_path).resolve() if not Path(stdout_path).is_absolute() else Path(stdout_path)
            stderr_file = (worktree / stderr_path).resolve() if not Path(stderr_path).is_absolute() else Path(stderr_path)
            assert stdout_file.exists(), "validation stdout file missing"
            assert stderr_file.exists(), "validation stderr file missing"
        return self._add_expectation(_assert)

    def expect_issue_label(self, label: str) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            labels = ctx.repo_host.get_issue_labels(ctx.issue_number)
            assert label in labels
        return self._add_expectation(_assert)

    def expect_issue_lacks_label(self, label: str) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            labels = ctx.repo_host.get_issue_labels(ctx.issue_number)
            assert label not in labels
        return self._add_expectation(_assert)

    def expect_issue_comment_contains(self, text: str, *, number: int | None = None) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            target = number if number is not None else ctx.issue_number
            comments = [c for c in getattr(ctx.repo_host, "comments", []) if c.get("number") == target]
            assert comments, f"No comments found for issue {target}"
            assert any(text in c.get("body", "") for c in comments), f"Expected comment containing '{text}'"
        return self._add_expectation(_assert)

    def expect_pending_validation_retries(self, count: int) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert len(ctx.orch.state.pending_validation_retries) == count
        return self._add_expectation(_assert)

    def expect_active_validation_retry(self, *, retry_count: int | None = None) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            matches = [
                session
                for session in ctx.orch.state.active_sessions
                if getattr(session.issue, "number", None) == ctx.issue_number
                and getattr(session, "validation_retry_count", 0) > 0
            ]
            assert matches, "Expected an active validation retry session"
            if retry_count is not None:
                assert any(
                    session.validation_retry_count == retry_count
                    for session in matches
                )
        return self._add_expectation(_assert)

    def expect_pending_reviews(self, count: int) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert len(ctx.orch.state.pending_reviews) == count
        return self._add_expectation(_assert)

    def expect_pending_reworks(self, count: int) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert len(ctx.orch.state.pending_reworks) == count
        return self._add_expectation(_assert)

    def expect_pr_label(self, label: str, *, pr_number: int = 100) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            pr = ctx.repo_host.get_pr(pr_number)
            assert pr is not None
            assert label in pr.labels
        return self._add_expectation(_assert)

    def expect_pr_lacks_label(self, label: str, *, pr_number: int = 100) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            pr = ctx.repo_host.get_pr(pr_number)
            assert pr is not None
            assert label not in pr.labels
        return self._add_expectation(_assert)

    def expect_review_feedback_written(self) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            worktree = ctx.worktree
            assert worktree is not None
            feedback = _latest_review_feedback(worktree)
            assert feedback is not None, "reviewer-feedback.json not found"
        return self._add_expectation(_assert)

    def expect_session_history_status(self, expected: set[str]) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert any(
                entry.status_reason in expected or entry.status in expected
                for entry in ctx.orch.state.session_history
            )
        return self._add_expectation(_assert)

    def expect_run_manifest(
        self,
        *,
        require_keys: list[str] | None = None,
        expected_fields: dict[str, object] | None = None,
        session_name_prefix: str | None = None,
    ) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            worktree = ctx.worktree
            assert worktree is not None
            manifest = _latest_run_manifest(worktree, session_name_prefix=session_name_prefix)
            assert manifest is not None, "run manifest.json not found"
            if require_keys:
                for key in require_keys:
                    assert key in manifest and manifest[key] not in ("", None), f"run manifest missing {key}"
            if expected_fields:
                for key, value in expected_fields.items():
                    assert manifest.get(key) == value, f"run manifest {key} != {value}"
        return self._add_expectation(_assert)

    def run(self) -> ScenarioContext:
        if not self.coder_command or not self.reviewer_command:
            raise AssertionError("coder and reviewer commands must be set")
        config = build_config(
            self.repo_root,
            coder_command=self.coder_command,
            reviewer_command=self.reviewer_command,
            review_exchange_mode=self.review_exchange_mode,
            review_exchange_require_validation=self.review_exchange_require_validation,
            review_exchange_max_rounds=self.review_exchange_max_rounds,
            review_exchange_max_no_progress=self.review_exchange_max_no_progress,
            validation_cmd=self.validation_cmd,
            max_validation_retries=self.max_validation_retries,
        )
        for mutator in self._config_overrides:
            mutator(config)
        issue = Issue(
            number=self.issue_number,
            title=self.issue_title,
            labels=self.issue_labels,
        )
        orch, repo_host, events, timeline_reader = build_orchestrator(
            self.repo_root,
            [issue],
            config,
            reconcile=self.reconcile,
            fresh_labels=self.fresh_labels,
            working_copy=self._working_copy_override,
            lease_renewer=self._lease_renewer_override,
            runner=self._runner_override,
        )
        if self._repo_host_mutator is not None:
            self._repo_host_mutator(repo_host)
        baseline = len(events.events)
        timeline_baseline = len(timeline_reader.read(self.issue_number).events)
        expected_events = set(self._wait_for_events) | set(self._expected_events)
        expected_timeline_events = set(self._expected_timeline_events)
        def _predicate() -> bool:
            if self._run_predicate is not None:
                base = self._run_predicate(orch)
            elif self._wait_for_events:
                base = True
            else:
                base = not orch.state.active_sessions
            if expected_events:
                events_since = events.events[baseline:]
                have_events = all(any(e.name == name for e in events_since) for name in expected_events)
            else:
                have_events = True
            if expected_timeline_events:
                timeline_since = timeline_reader.read(self.issue_number).events[timeline_baseline:]
                have_timeline = all(
                    any((e.source_event or e.event) == name.value for e in timeline_since)
                    for name in expected_timeline_events
                )
            else:
                have_timeline = True
            return base and have_events and have_timeline

        run_until(orch, _predicate, max_ticks=self.max_ticks)
        ctx = ScenarioContext(
            orch=orch,
            repo_host=repo_host,
            events=events,
            timeline_reader=timeline_reader,
            config=config,
            runner=self._runner_override,
            repo_root=self.repo_root,
            issue_number=self.issue_number,
            event_baseline=baseline,
            timeline_baseline=timeline_baseline,
        )
        for expectation in self._expectations:
            expectation(ctx)
        close = getattr(orch, "close", None)
        if callable(close):
            close()
        else:
            _close_goal_pilot_store(orch)
        return ctx

    def _add_expectation(self, expectation: Expectation) -> Scenario:
        self._expectations.append(expectation)
        return self


def scenario(name: str, repo_root: Path) -> Scenario:
    return Scenario(name=name, repo_root=repo_root)


def _event_payloads(events, name: EventName) -> list[dict]:
    return [e.data for e in events if e.name == name]


def _latest_event_payload(events, name: EventName) -> dict | None:
    payloads = _event_payloads(events, name)
    return payloads[-1] if payloads else None


def _latest_event(events, names: set[EventName]):
    matches = [e for e in events if e.name in names]
    return matches[-1] if matches else None


def _latest_timeline_event(events, names: set[str]):
    matches = [e for e in events if (e.source_event or e.event) in names]
    return matches[-1] if matches else None


def _summary_rounds(worktree: Path) -> list[int]:
    summary_root = worktree / ".issue-orchestrator" / "sessions"
    summary_files = list(summary_root.rglob("review-exchange/summary.json"))
    assert summary_files, "review-exchange summary.json not found"
    rounds = []
    for summary_path in summary_files:
        data = json.loads(summary_path.read_text())
        if "completed_rounds" in data:
            rounds.append(int(data["completed_rounds"]))
    return rounds


def _latest_review_exchange_summary(worktree: Path) -> dict[str, Any] | None:
    """Return the most recent ``review-exchange/summary.json`` payload, if any.

    Returns ``None`` (not an assertion failure) when no summary exists,
    so callers can verify alignment only when a runner actually wrote
    one — avoids false negatives on cache-hit / draft-PR paths.
    """
    summary_root = worktree / ".issue-orchestrator" / "sessions"
    if not summary_root.exists():
        return None
    summary_files = sorted(
        summary_root.rglob("review-exchange/summary.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not summary_files:
        return None
    return json.loads(summary_files[-1].read_text())


def _close_goal_pilot_store(orch: object) -> None:
    deps = getattr(orch, "deps", None)
    if deps is None:
        return
    store = getattr(deps, "goal_pilot_store", None)
    if store is None:
        return
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _latest_validation_record(worktree: Path) -> Path | None:
    root = worktree / ".issue-orchestrator" / "validation"
    records = list(root.rglob("*.json"))
    if not records:
        return None
    records.sort(key=lambda p: p.stat().st_mtime)
    return records[-1]


def _latest_review_feedback(worktree: Path) -> Path | None:
    root = worktree / ".issue-orchestrator" / "sessions"
    records = list(root.rglob("reviewer-feedback.json"))
    if not records:
        return None
    records.sort(key=lambda p: p.stat().st_mtime)
    return records[-1]


def _latest_run_manifest(worktree: Path, *, session_name_prefix: str | None = None) -> dict | None:
    root = worktree / ".issue-orchestrator" / "sessions"
    manifests = list(root.rglob("manifest.json"))
    run_manifests = [p for p in manifests if "__" in p.parent.name]
    if run_manifests:
        manifests = run_manifests
    if session_name_prefix is not None:
        manifests = [p for p in manifests if p.parent.name.split("__", 1)[-1].startswith(session_name_prefix)]
    if not manifests:
        return None
    manifests.sort(key=lambda p: p.stat().st_mtime)
    return json.loads(manifests[-1].read_text())
