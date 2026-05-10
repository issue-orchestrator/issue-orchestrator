"""Evidence-based run audit artifacts for demand-driven session postmortems."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..domain.artifact_contracts import (
    ValidationFailed,
    ValidationPassed,
    ValidationRetry,
)
from ..domain.run_manifest import RunManifest

RUN_AUDIT_FILENAME = "run-audit.json"


@dataclass(frozen=True)
class RunAuditResult:
    """Structured run audit result persisted beside the run manifest."""

    path: Path
    summary: str
    dominant_time_bucket: str


def write_run_audit(
    run_dir: Path,
    *,
    issue_labels: Sequence[str],
    trigger_source: str,
    trigger_label: str | None = None,
    completion_label: str | None = None,
    trigger_threshold_minutes: int | None = None,
    processing_errors: Sequence[str] | None = None,
) -> RunAuditResult:
    """Write a run-audit.json artifact for a completed run."""
    manifest = RunManifest.load(run_dir)
    review_exchange = _load_review_exchange(run_dir)
    summary, dominant_bucket, findings = _summarize_run(manifest, review_exchange)

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "issue_number": manifest.issue_number,
        "session_name": manifest.session_name,
        "run_id": manifest.run_id,
        "run_dir": str(run_dir),
        "outcome": manifest.outcome,
        "runtime_minutes": manifest.runtime_minutes,
        "summary": summary,
        "dominant_time_bucket": dominant_bucket,
        "findings": findings,
        "issue_labels": list(issue_labels),
        "trigger_source": trigger_source,
        "trigger_label": trigger_label,
        "completion_label": completion_label,
        "trigger_threshold_minutes": trigger_threshold_minutes,
        "processing_errors": list(processing_errors or ()),
        "validation": _validation_audit_payload(manifest),
        "review_exchange": review_exchange,
        "evidence_paths": {
            "manifest": str(run_dir / "manifest.json"),
            "terminal_recording": manifest.log_path,
            "review_exchange_summary": review_exchange.get("summary_path") if review_exchange else None,
        },
    }

    path = run_dir / RUN_AUDIT_FILENAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return RunAuditResult(path=path, summary=summary, dominant_time_bucket=dominant_bucket)


def _summarize_run(
    manifest: RunManifest,
    review_exchange: dict[str, Any] | None,
) -> tuple[str, str, list[str]]:
    findings: list[str] = []
    runtime = manifest.runtime_minutes
    dominant_bucket = "unknown"
    summary = "Audit captured the completed run."

    segments = _extract_segments(review_exchange)
    findings.extend(_review_exchange_findings(review_exchange))

    if segments:
        dominant_bucket, minutes, label = max(segments, key=lambda item: item[1])
        summary = f"Longest observed segment was {label.lower()} ({minutes:.1f} min)."
        findings.append(summary)
    elif isinstance(runtime, (int, float)):
        summary = f"No finer-grained timing artifact was available; total observed runtime was {float(runtime):.1f} min."
        findings.append(summary)

    dominant_bucket = _apply_validation_findings(manifest, findings, dominant_bucket)
    _append_runtime_findings(manifest, findings)

    if not findings:
        findings.append("No dominant delay segment could be inferred from persisted artifacts.")

    return summary, dominant_bucket, findings


def _extract_segments(
    review_exchange: dict[str, Any] | None,
) -> list[tuple[str, float, str]]:
    if not review_exchange:
        return []
    segments: list[tuple[str, float, str]] = []
    for round_entry in review_exchange.get("rounds", []):
        _append_segment(
            segments,
            round_entry.get("coder_rework_minutes"),
            "coder_rework",
            f"Coder rework after review round {round_entry['round_index']}",
        )
        _append_segment(
            segments,
            round_entry.get("reviewer_follow_up_minutes"),
            "reviewer_follow_up",
            f"Reviewer follow-up for review round {round_entry['round_index'] + 1}",
        )
    return segments


def _append_segment(
    segments: list[tuple[str, float, str]],
    minutes: Any,
    bucket: str,
    label: str,
) -> None:
    if isinstance(minutes, (int, float)):
        segments.append((bucket, float(minutes), label))


def _review_exchange_findings(review_exchange: dict[str, Any] | None) -> list[str]:
    if not review_exchange:
        return []
    completed_rounds = review_exchange.get("completed_rounds")
    if not isinstance(completed_rounds, int) or completed_rounds <= 1:
        return []
    return [
        "Review exchange required "
        f"{completed_rounds} rounds before reaching `{review_exchange.get('status', 'unknown')}`."
    ]


def _apply_validation_findings(
    manifest: RunManifest,
    findings: list[str],
    dominant_bucket: str,
) -> str:
    # Branch off the typed outcome so the typed status (not a stale
    # legacy reason) drives the finding text. ``ValidationPassed`` /
    # ``None`` / ``ValidationRetry`` don't need a finding here — only
    # terminal failures.
    outcome = manifest.validation_outcome
    if not isinstance(outcome, ValidationFailed):
        return dominant_bucket
    findings.append(
        f"Validation failed after the run completed: {outcome.reason}."
    )
    if dominant_bucket == "unknown":
        return "validation_retry"
    return dominant_bucket


def _validation_audit_payload(manifest: RunManifest) -> dict[str, Any]:
    """Project the validation outcome + artifact paths into the audit payload.

    Routes the (passed, status, reason) triple through the typed outcome
    so the audit JSON cannot ship an inconsistent triple even if the
    on-disk manifest had one (legacy pre-#6302 manifests).
    """
    outcome = manifest.validation_outcome
    if isinstance(outcome, ValidationPassed):
        passed: bool | None = True
        status: str | None = "passed"
        reason: str | None = None
    elif isinstance(outcome, ValidationFailed):
        passed = False
        status = "failed"
        reason = outcome.reason
    elif isinstance(outcome, ValidationRetry):
        passed = False
        status = "retry"
        reason = outcome.reason
    else:
        passed = None
        status = None
        reason = None
    return {
        "passed": passed,
        "status": status,
        "reason": reason,
        "record_path": manifest.validation_record_path,
        "stdout_path": manifest.validation_stdout,
        "stderr_path": manifest.validation_stderr,
    }


def _append_runtime_findings(manifest: RunManifest, findings: list[str]) -> None:
    runtime = manifest.runtime_minutes
    if manifest.outcome == "completed" and isinstance(runtime, (int, float)) and float(runtime) >= 20:
        findings.append("This exceeded the normal short coding-path runtime and warranted audit capture.")


def _load_review_exchange(run_dir: Path) -> dict[str, Any] | None:
    exchange_dir = run_dir / "review-exchange"
    summary_path = exchange_dir / "summary.json"
    if not summary_path.exists():
        return None

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    rounds: list[dict[str, Any]] = []
    previous_coder_at: datetime | None = None
    for round_path in sorted(exchange_dir.glob("round-*.json")):
        try:
            payload = json.loads(round_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        reviewer = payload.get("reviewer") if isinstance(payload.get("reviewer"), dict) else None
        coder = payload.get("coder") if isinstance(payload.get("coder"), dict) else None
        reviewer_at = _parse_iso(reviewer.get("timestamp")) if reviewer else None
        coder_at = _parse_iso(coder.get("timestamp")) if coder else None
        round_index = int(payload.get("reviewer", {}).get("round_index") or payload.get("coder", {}).get("round_index") or len(rounds) + 1)

        round_entry: dict[str, Any] = {
            "round_index": round_index,
            "reviewer_response_type": reviewer.get("response_type") if reviewer else None,
            "coder_response_type": coder.get("response_type") if coder else None,
        }
        if reviewer_at and coder_at and coder_at >= reviewer_at:
            round_entry["coder_rework_minutes"] = round((coder_at - reviewer_at).total_seconds() / 60.0, 1)
        if previous_coder_at and reviewer_at and reviewer_at >= previous_coder_at:
            round_entry["reviewer_follow_up_minutes"] = round((reviewer_at - previous_coder_at).total_seconds() / 60.0, 1)
        if coder_at:
            previous_coder_at = coder_at
        rounds.append(round_entry)

    return {
        "summary_path": str(summary_path),
        "completed_rounds": summary.get("completed_rounds"),
        "status": summary.get("status"),
        "response_text": summary.get("response_text"),
        "rounds": rounds,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
