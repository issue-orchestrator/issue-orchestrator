"""Scripted review outcomes behind the runner port; coder receipts remain real."""

import json
from pathlib import Path
from contextvars import ContextVar
from issue_orchestrator.domain.review_exchange_run import ReviewExchangeRunAssets
from issue_orchestrator.ports.completion_intake import CompletionIntakeRuntime
from issue_orchestrator.ports.review_exchange_runner import ReviewExchangeRunner
from tests.simulated_scenarios.exchange_coder import scripted_coder_attempts
from issue_orchestrator.execution.command_runner import LocalCommandRunner

review_outcomes: ContextVar[dict[str, object]] = ContextVar("scenario_review_outcomes")


def _validation_passed(run_dir: Path) -> bool:
    """Mirror :func:`persistent_session_exchange._validation_passed`.

    Existence of ``validation-record.json`` is not enough — the runner
    parses it and requires ``passed: true``. Invalid JSON or
    ``{"passed": false}`` must read as not-passed so a failed seeded
    record cannot let a reviewer ``ok`` slip through.
    """
    record_path = run_dir / "validation-record.json"
    if not record_path.exists():
        return False
    try:
        data = json.loads(record_path.read_text())
    except json.JSONDecodeError:
        return False
    return bool(data.get("passed"))


def build_scripted_review_runner(
    intake: CompletionIntakeRuntime,
) -> ReviewExchangeRunner:
    overrides = review_outcomes.get()
    from datetime import datetime, timezone

    from issue_orchestrator.domain.review_exchange import (
        ReviewExchangeOutcome,
        ReviewExchangeResponse,
    )
    from issue_orchestrator.domain.review_exchange_summary import (
        ReviewExchangeSummaryV1,
    )
    from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
    from issue_orchestrator.events import EventName

    default_reviewer_responses = [
        {
            "response_type": "ok",
            "response_text": "LGTM (stubbed scenario response)",
            "getting_closer": True,
        }
    ]
    reviewer_responses_raw = overrides.get(
        "reviewer_responses",
        default_reviewer_responses,
    )
    coder_response_type = overrides.get("coder_response_type")
    rounds_override = overrides.get("rounds")
    status_override = overrides.get("status")
    reason_override = overrides.get("reason")
    # Mirror the reviewer_ok_with_validation.sh fixture: when a test
    # opts in via ``write_validation_record_passed=True``, the stub
    # writes a passing validation-record.json into the run dir before
    # the reviewer round so the runner's require_validation guard
    # accepts the reviewer-ok outcome.
    write_validation_record_passed = bool(
        overrides.get("write_validation_record_passed", False)
    )
    # Counterpart for the negative-seeded-record case: writes
    # ``{"passed": false}`` so the require_validation guard flips a
    # reviewer ``ok`` even though the file exists. Without this, the
    # simulated coverage would treat file-existence as success and
    # miss a failed/corrupt seeded record.
    write_validation_record_failed = bool(
        overrides.get("write_validation_record_failed", False)
    )

    def _stub_run(
        self,
        *,
        exchange_run,
        completion_capability,
        coder_worktree,
        issue_number,
        issue_title,
        coder_label,
        reviewer_label,
        coder_agent,
        reviewer_agent,
        runtime_config,
        max_rounds,
        max_no_progress,
        require_validation,
        initial_validation_evidence=None,
        approval_gate=None,
        web_port=None,
        nit_policy="surface",
        events=None,
        event_context=None,
    ):
        with scripted_coder_attempts(
            intake,
            exchange_run.session_run,
            completion_capability,
            coder_agent,
            runtime_config,
            web_port,
            LocalCommandRunner(),
        ) as coder_attempt:
            assert isinstance(runtime_config, RuntimeConfigReference)
            session_name = exchange_run.session_name
            run_dir = exchange_run.assets.run_dir
            exchange_dir = exchange_run.assets.exchange_dir
            exchange_dir.mkdir(parents=True, exist_ok=True)

            # Mirror the real runner: when an initial validation record was
            # passed in (cache hit / pre-seeded scenarios), copy it into the
            # exchange's run_dir before the require_validation guard fires.
            # Without this, scenarios named after the cache/seeding path
            # (cache_requires_validation, cache_invalid_validation_reruns)
            # silently fall through the production path they claim to test.
            if (
                initial_validation_evidence is not None
            ):
                seed_target = run_dir / "validation-record.json"
                if not seed_target.exists():
                    seed_target.write_bytes(initial_validation_evidence.result_bytes)

            if write_validation_record_passed:
                (run_dir / "validation-record.json").write_text(
                    json.dumps({"passed": True}),
                    encoding="utf-8",
                )
            if write_validation_record_failed:
                (run_dir / "validation-record.json").write_text(
                    json.dumps({"passed": False}),
                    encoding="utf-8",
                )

            def _emit(name, payload):
                if events is None or event_context is None:
                    return
                from issue_orchestrator.ports import make_trace_event

                enriched = dict(payload)
                enriched["run_dir"] = str(run_dir)
                enriched["session_run_id"] = exchange_run.run_id
                events.publish(make_trace_event(name, event_context.enrich(enriched)))

            _emit(
                EventName.REVIEW_EXCHANGE_STARTED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "exchange_dir": str(exchange_dir),
                },
            )

            # Walk the scripted reviewer responses, capping at max_rounds.
            # If validation is required and no record exists, the runner's
            # contract is to flip a reviewer "ok" into "changes_requested"
            # with reason "validation missing" — mirror that here so tests
            # that exercise the validation gate see the same shape.
            assert isinstance(reviewer_responses_raw, list)
            assert all(
                isinstance(response, dict) for response in reviewer_responses_raw
            )
            responses = list(reviewer_responses_raw)
            rounds_run = 0
            last_reviewer: ReviewExchangeResponse | None = None
            no_progress_streak = 0
            terminating_status: str | None = None
            terminating_reason: str | None = None

            for round_index in range(1, max_rounds + 1):
                if not responses:
                    break
                if status_override != "error":
                    coder_attempt(round_index)
                entry = responses.pop(0) if len(responses) > 1 else responses[0]
                response_type = str(entry.get("response_type", "ok"))
                getting_closer = bool(entry.get("getting_closer", True))
                response_text = str(entry.get("response_text", "stub-reviewer"))

                if (
                    response_type == "ok"
                    and require_validation
                    and not _validation_passed(run_dir)
                ):
                    response_type = "changes_requested"
                    response_text = "Validation record missing or failed"
                    getting_closer = False

                if response_type == "ok" and approval_gate is not None:
                    rejection_reason = approval_gate.rejection_reason()
                    if rejection_reason is not None:
                        response_type = "changes_requested"
                        response_text = f"{rejection_reason} Address it and continue."
                        getting_closer = False

                reviewer = ReviewExchangeResponse(
                    response_type=response_type,
                    response_text=response_text,
                    getting_closer=getting_closer,
                )
                last_reviewer = reviewer
                rounds_run = round_index
                _emit(
                    EventName.REVIEW_EXCHANGE_ROUND_COMPLETED,
                    {
                        "issue_number": issue_number,
                        "session_name": session_name,
                        "round_index": round_index,
                        "reviewer_response_type": reviewer.response_type,
                        "reviewer_response_text": reviewer.response_text,
                        "coder_response_type": coder_response_type,
                        "review_nit_policy": nit_policy,
                        "review_abstraction_status": "no_issues",
                    },
                )

                if response_type == "ok":
                    terminating_status = "ok"
                    terminating_reason = "reviewer_ok"
                    break
                if not getting_closer:
                    no_progress_streak += 1
                else:
                    no_progress_streak = 0
                if max_no_progress > 0 and no_progress_streak >= max_no_progress:
                    terminating_status = "stopped"
                    terminating_reason = "reviewer_reports_no_progress"
                    break
            else:
                terminating_status = "stopped"
                terminating_reason = "max_rounds_exceeded"

            if terminating_status is None:
                terminating_status = "ok"
                terminating_reason = "reviewer_ok"

            # Marker-level overrides win — useful for protocol-error/error
            # scenarios that the round loop above can't naturally produce.
            if status_override is not None:
                terminating_status = str(status_override)
            if reason_override is not None:
                terminating_reason = str(reason_override)
            if rounds_override is not None:
                assert isinstance(rounds_override, int)
                rounds_run = rounds_override

            _emit(
                EventName.REVIEW_EXCHANGE_COMPLETED,
                {
                    "issue_number": issue_number,
                    "session_name": session_name,
                    "rounds": rounds_run,
                    "status": terminating_status,
                    "reason": terminating_reason,
                    "review_nit_policy": nit_policy,
                    "review_abstraction_status": "no_issues",
                },
            )
            summary = ReviewExchangeSummaryV1.from_payload(
                {
                    "completed_rounds": rounds_run,
                    "status": terminating_status,
                    "response_text": last_reviewer.response_text
                    if last_reviewer
                    else None,
                    "reason": terminating_reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            # The real runner persists summary.json atomically into
            # exchange_dir; the orchestration logic reads it on the next
            # tick to decide cache-hit / advance / halt. Mirror that here so
            # scenario tests that walk the run-dir layout match production.
            from issue_orchestrator.infra.atomic_io import atomic_write_json

            atomic_write_json(exchange_dir / "summary.json", summary.to_payload())
            return ReviewExchangeOutcome(
                status=summary.status,
                rounds=rounds_run,
                reason=summary.reason,
                run_assets=ReviewExchangeRunAssets.from_exchange_dir(exchange_dir),
                reviewer_response=last_reviewer,
                summary=summary,
            )

    class ScriptedRunner:
        run = _stub_run

        def job_timeout_seconds(self, *, coder_agent, reviewer_agent, max_rounds):
            return None

    return ScriptedRunner()
