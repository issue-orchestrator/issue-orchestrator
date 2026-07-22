"""Tech Lead decision artifact pair loading and validation.

Tech Lead sessions complete by writing ``tech-lead-decision.json`` +
``tech-lead-report.md`` into their run's ``tech-lead-data`` directory (ADR-0031).
Both files are agent-authored and therefore **untrusted input**: this module
is the ONE entry point for parsing the pair, mirroring
``completion_record_validation.load_completion_record_result`` — a per-file
size gate runs BEFORE ``json.load``, and every failure maps to a typed
reason so callers can distinguish a genuinely missing artifact from one
that was present but rejected. ``load_tech_lead_artifact_pair`` never raises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..domain.tech_lead_artifacts import (
    TECH_LEAD_DECISION_FILENAME,
    TECH_LEAD_REPORT_FILENAME,
    TechLeadDecision,
    validate_tech_lead_report_links,
)

logger = logging.getLogger(__name__)

# Hard cap on either artifact file's size before we read it. Real decisions
# and reports are a few KB; anything approaching this cap is almost certainly
# abusive or broken. Matches the completion-record gate rationale in
# ``completion_record_validation._MAX_COMPLETION_FILE_BYTES``: check the file
# size first so a hostile agent cannot exhaust memory/CPU by writing a huge
# blob and forcing the parser to walk it.
_MAX_TECH_LEAD_ARTIFACT_BYTES = 2 * 1024 * 1024


class TechLeadDecisionLoadFailure(str, Enum):
    """Typed reason a tech_lead artifact pair could not be loaded."""

    MISSING_DECISION = "missing_decision"
    MISSING_REPORT = "missing_report"
    TOO_LARGE = "too_large"
    INVALID_JSON = "invalid_json"
    CONTRACT_VIOLATION = "contract_violation"


@dataclass(frozen=True)
class TechLeadArtifactLoadResult:
    """Result of parsing an untrusted tech_lead artifact pair."""

    decision: TechLeadDecision | None = None
    failure: TechLeadDecisionLoadFailure | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.decision is not None


def load_tech_lead_artifact_pair_for_run(run_dir: Path) -> TechLeadArtifactLoadResult:
    """Load the artifact pair from a session run dir's ``tech-lead-data`` directory."""
    data_dir = run_dir / "tech-lead-data"
    return load_tech_lead_artifact_pair(
        data_dir / TECH_LEAD_DECISION_FILENAME,
        data_dir / TECH_LEAD_REPORT_FILENAME,
    )


def _missing_or_empty(path: Path) -> bool:
    try:
        return not path.exists() or path.stat().st_size == 0
    except OSError:
        return True


def _oversized(path: Path) -> bool:
    return path.stat().st_size > _MAX_TECH_LEAD_ARTIFACT_BYTES


def load_tech_lead_artifact_pair(
    decision_path: Path, report_path: Path
) -> TechLeadArtifactLoadResult:
    """Load and validate the tech_lead decision + report artifact pair.

    Both files must exist and be non-empty. The decision JSON is parsed
    via ``TechLeadDecision.from_agent_payload`` (field-level bounds), then
    ``validate_tech_lead_report_links`` requires the report to mention every
    finding/action id. Any contract ``ValueError`` becomes a typed
    ``CONTRACT_VIOLATION`` with the message as detail. Never raises.
    """
    if _missing_or_empty(decision_path):
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.MISSING_DECISION,
            detail=f"tech_lead decision missing or empty: {decision_path}",
        )
    if _missing_or_empty(report_path):
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.MISSING_REPORT,
            detail=f"tech_lead report missing or empty: {report_path}",
        )
    for path in (decision_path, report_path):
        if _oversized(path):
            return TechLeadArtifactLoadResult(
                failure=TechLeadDecisionLoadFailure.TOO_LARGE,
                detail=(
                    f"tech_lead artifact {path.name} is {path.stat().st_size} bytes,"
                    f" exceeds max {_MAX_TECH_LEAD_ARTIFACT_BYTES}"
                ),
            )

    try:
        with open(decision_path) as f:
            payload = json.load(f)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON in tech_lead decision %s: %s", decision_path, exc)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.INVALID_JSON,
            detail=f"Invalid JSON: {exc}",
        )
    except OSError as exc:
        logger.error("Could not read tech_lead decision %s: %s", decision_path, exc)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.MISSING_DECISION,
            detail=f"Could not read tech_lead decision: {exc}",
        )

    try:
        report_text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("Could not read tech_lead report %s: %s", report_path, exc)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.MISSING_REPORT,
            detail=f"Could not read tech_lead report: {exc}",
        )

    try:
        decision = TechLeadDecision.from_agent_payload(payload)
        validate_tech_lead_report_links(decision, report_text)
    except ValueError as exc:
        logger.error("Tech Lead decision contract violation %s: %s", decision_path, exc)
        return TechLeadArtifactLoadResult(
            failure=TechLeadDecisionLoadFailure.CONTRACT_VIOLATION,
            detail=str(exc),
        )

    logger.info(
        "Loaded tech_lead decision: findings=%d proposed_actions=%d path=%s",
        len(decision.findings),
        len(decision.proposed_actions),
        decision_path,
    )
    return TechLeadArtifactLoadResult(decision=decision)
