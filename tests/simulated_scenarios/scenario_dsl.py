from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName

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

    @property
    def worktree(self) -> Path | None:
        history = getattr(self.orch.state, "session_history", [])
        if not history:
            return None
        return history[0].worktree_path


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

    _expectations: list[Expectation] = field(default_factory=list, init=False)
    _run_predicate: Callable[[object], bool] | None = field(default=None, init=False)

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

    def wait_for(self, predicate: Callable[[object], bool], *, max_ticks: int | None = None) -> Scenario:
        self._run_predicate = predicate
        if max_ticks is not None:
            self.max_ticks = max_ticks
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
        def _assert(ctx: ScenarioContext) -> None:
            assert any(e.name == name for e in ctx.events.events)
        return self._add_expectation(_assert)

    def expect_no_event(self, name: EventName) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert all(e.name != name for e in ctx.events.events)
        return self._add_expectation(_assert)

    def expect_review_exchange_reason(self, reason: str) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            payload = _latest_event_payload(ctx.events, EventName.REVIEW_EXCHANGE_COMPLETED)
            assert payload is not None
            assert payload.get("reason") == reason
        return self._add_expectation(_assert)

    def expect_review_exchange_round_response(
        self,
        *,
        reviewer_response_type: str | None = None,
        coder_response_type: str | None = None,
    ) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            payloads = _event_payloads(ctx.events, EventName.REVIEW_EXCHANGE_ROUND_COMPLETED)
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

    def expect_pending_validation_retries(self, count: int) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert len(ctx.orch.state.pending_validation_retries) == count
        return self._add_expectation(_assert)

    def expect_session_history_status(self, expected: set[str]) -> Scenario:
        def _assert(ctx: ScenarioContext) -> None:
            assert any(
                entry.status_reason in expected or entry.status in expected
                for entry in ctx.orch.state.session_history
            )
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
        issue = Issue(
            number=self.issue_number,
            title=self.issue_title,
            labels=self.issue_labels,
        )
        orch, repo_host, events = build_orchestrator(self.repo_root, [issue], config)
        predicate = self._run_predicate or (lambda o: not o.state.active_sessions)
        run_until(orch, lambda: predicate(orch), max_ticks=self.max_ticks)
        ctx = ScenarioContext(orch=orch, repo_host=repo_host, events=events)
        for expectation in self._expectations:
            expectation(ctx)
        return ctx

    def _add_expectation(self, expectation: Expectation) -> Scenario:
        self._expectations.append(expectation)
        return self


def scenario(name: str, repo_root: Path) -> Scenario:
    return Scenario(name=name, repo_root=repo_root)


def _event_payloads(events, name: EventName) -> list[dict]:
    return [e.data for e in events.events if e.name == name]


def _latest_event_payload(events, name: EventName) -> dict | None:
    payloads = _event_payloads(events, name)
    return payloads[-1] if payloads else None


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
