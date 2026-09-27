"""How each exam case configures the engine it runs.

A case's engine config is three layers: :data:`EXAM_BASE_OVERLAY`, the
config ``OrchestratorProcess`` generates from :func:`exam_config`, and the
case's own YAML overlay. Both the live run and the unit gate
(``tests/unit/testing/exam/test_exam_engine_config.py``) build it here and
write it through :meth:`ExamEngine.write_config`, so the gate loads exactly
the file the live engine would start with. The charter (#7330) turned
``reset_retry: execute`` into a startup error that only a live Case B run
found; this keeps that class of drift in the unit gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from issue_orchestrator.infra.config import Config

from tests.e2e.exam.agents import CODER_LABEL
from tests.e2e.exam.engine import ExamEngine, exam_config
from tests.e2e.exam.engine_checkout import EngineCheckout
from tests.e2e.exam.seeding import E2E_DATA_LABEL

#: Production tech-lead authority for every act-level action (the operator's
#: io/porchpin configs), so a remedy is executed — and graded — as it would
#: be. ``reset_retry`` is destructive, so the charter (#7330) only allows it
#: to be proposed; the engine refuses to start on ``execute``. Proposing it
#: changes no grade: the remedy grader marks any ``reset_retry`` concerning
#: the item WRONG whatever its disposition, and destruction is read from
#: executed receipts and GitHub state. Filing is proposed too: an executed
#: follow-up is worked by the exam's scripted coder (the first Case B run
#: grew a second investigation that way), and a case file is a real issue
#: other engines' case-file readers could pick up. Escalation is on the
#: charter's floor and always executes. ``release_withheld_review`` executes
#: as production's does: the ceiling is open and the charter's default flow
#: role executes, so Case B's right remedy actually runs (#7399).
EXAM_TECH_LEAD_AUTHORITY: Mapping[str, str] = {
    "reset_retry": "propose",
    "kill_hung_session": "execute",
    "request_rework": "execute",
    "recover_validated_work": "execute",
    "release_withheld_review": "execute",
    "create_issue": "propose",
    "flag_pattern": "propose",
}


@dataclass(frozen=True)
class CaseEngine:
    """One case's engine: the fault its reviewer plants, whether a real tech
    lead runs, and the YAML overlay over the generated config."""

    reviewer_exchange_fault: str
    overlay: Mapping[str, Any]
    tech_lead: bool = False
    release_file: Path | None = None
    """Hold work mid-flight until this file exists (``exam_config``)."""

    def config(
        self,
        base: Config,
        *,
        checkout: EngineCheckout,
        run_label: str,
        tech_lead_model: str | None = None,
    ) -> Config:
        if self.tech_lead and not tech_lead_model:
            raise ValueError("this case runs a real tech lead; pass its model")
        if not self.tech_lead and tech_lead_model:
            raise ValueError("this case runs no tech lead; a model would be ignored")
        return exam_config(
            base,
            checkout=checkout,
            run_label=run_label,
            reviewer_exchange_fault=self.reviewer_exchange_fault,
            tech_lead_model=tech_lead_model,
            release_file=self.release_file,
        )

    def engine(self, config: Config, checkout: EngineCheckout) -> ExamEngine:
        return ExamEngine(config, checkout, overlay=self.overlay)


def case_a_engine() -> CaseEngine:
    """A review exchange whose reviewer exits without a verdict, bounded so
    the engine halts it after three rounds."""
    return CaseEngine(
        reviewer_exchange_fault="exit-silently",
        overlay={
            "review": {
                "exchange": {
                    "mode": "via-local-loop",
                    "loop": {"max_rounds": 3, "max_no_progress": 2, "require_validation": True},
                },
                "max_consecutive_review_exchange_failures": 3,
            }
        },
    )


def case_b_engine(authority: Mapping[str, str] = EXAM_TECH_LEAD_AUTHORITY) -> CaseEngine:
    """A real tech lead, reached through the stuck sweep (the path porchpin
    took, #7293), with production authority."""
    return CaseEngine(
        reviewer_exchange_fault="none",
        tech_lead=True,
        overlay={
            "review": {
                "tech_lead_follow_up_agent": CODER_LABEL,
                "tech_lead_review_on_failure": True,
            },
            "tech_lead": {
                "max_concurrent": 1,
                "explicit_labels": [E2E_DATA_LABEL],
                "inherit_labels": [E2E_DATA_LABEL],
                "authority": dict(authority),
                "findings": {"promote": "off"},
                "health_review": {"interval_minutes": 0},
                # Production sweeps every 240 minutes; the exam cannot wait
                # that long. Its scan is scoped by filtering.label, so it only
                # ever sees this run's issues.
                "stuck_sweep": {"enabled": True, "interval_minutes": 1, "max_recovery_attempts": 3},
            },
        },
    )


def case_c_engine() -> CaseEngine:
    """The default engine: Case C plants its fault in labels, not config."""
    return CaseEngine(reviewer_exchange_fault="none", overlay={})


def case_u_engine(release_file: Path) -> CaseEngine:
    """Work held mid-flight until ``release_file`` exists, reviewed AFTER it
    is published (``via-draft-pr``) so the review is its own session.

    The overlay is YAML both the base and the candidate accept; the same file
    starts both engines (an upgrade does not change the operator's config).
    """
    return CaseEngine(
        reviewer_exchange_fault="none",
        overlay={"review": {"exchange": {"mode": "via-draft-pr"}}},
        release_file=release_file,
    )
