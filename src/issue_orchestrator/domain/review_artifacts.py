"""Review artifact pair contract.

Reviewer agents produce two related artifacts:

* ``review-report.md`` — human-readable review narrative.
* ``review-decision.json`` — strict orchestration/audit data.

The JSON is authoritative for workflow. The markdown explains the decision for
humans. Stable item IDs tie the two together without making markdown parsing a
policy dependency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


NitPolicy = Literal["ignore", "surface", "address"]
ReviewVerdict = Literal["approved", "changes_requested", "disagree"]
AbstractionReviewStatus = Literal["no_issues", "changes_requested", "deferred"]

REVIEW_REPORT_ARTIFACT = "review_report"
REVIEW_DECISION_ARTIFACT = "review_decision"
REVIEW_REPORT_FILENAME = "review-report.md"
REVIEW_DECISION_FILENAME = "review-decision.json"
_VALID_NIT_POLICIES = {"ignore", "surface", "address"}
_VALID_ABSTRACTION_REVIEW_STATUSES = {"no_issues", "changes_requested", "deferred"}


@dataclass(frozen=True)
class ReviewItem:
    """One blocking finding or nit in the machine decision."""

    id: str
    title: str
    location: str | None = None
    rationale: str | None = None
    suggested_change: str | None = None

    @classmethod
    def from_mapping(cls, data: Any, *, fallback_prefix: str, index: int) -> "ReviewItem":
        if not isinstance(data, dict):
            text = str(data).strip() or "Unspecified review item"
            return cls(id=f"{fallback_prefix}{index}", title=text)
        raw_id = data.get("id")
        item_id = str(raw_id).strip() if isinstance(raw_id, str) and raw_id.strip() else f"{fallback_prefix}{index}"
        raw_title = data.get("title") or data.get("summary") or data.get("rationale")
        title = str(raw_title).strip() if raw_title is not None else "Unspecified review item"
        return cls(
            id=item_id,
            title=title,
            location=_optional_str(data.get("location")),
            rationale=_optional_str(data.get("rationale")),
            suggested_change=_optional_str(data.get("suggested_change")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "title": self.title}
        if self.location:
            payload["location"] = self.location
        if self.rationale:
            payload["rationale"] = self.rationale
        if self.suggested_change:
            payload["suggested_change"] = self.suggested_change
        return payload


@dataclass(frozen=True)
class AbstractionReview:
    """Reviewer assessment of owner/port/command abstraction fit."""

    status: AbstractionReviewStatus = "no_issues"
    findings: tuple[ReviewItem, ...] = ()
    rationale: str = ""
    follow_up_issue_url: str | None = None

    @classmethod
    def from_mapping(cls, data: Any, *, required: bool = False) -> "AbstractionReview":
        if not isinstance(data, dict):
            if required:
                raise ValueError("decision.abstraction_review is required")
            return cls()
        status = _coerce_abstraction_review_status(data.get("status"))
        findings = _items(data.get("findings"), fallback_prefix="A")
        rationale = _optional_str(data.get("rationale")) or ""
        follow_up_issue_url = _optional_str(data.get("follow_up_issue_url"))
        if status == "changes_requested" and not findings:
            findings = (
                ReviewItem(
                    id="A1",
                    title=rationale or "Abstraction issue requires changes",
                ),
            )
        review = cls(
            status=status,
            findings=findings,
            rationale=rationale,
            follow_up_issue_url=follow_up_issue_url,
        )
        review.validate()
        return review

    def validate(self) -> None:
        if self.status not in _VALID_ABSTRACTION_REVIEW_STATUSES:
            raise ValueError(f"invalid abstraction_review.status: {self.status}")
        if self.status == "no_issues" and self.findings:
            raise ValueError("no_issues abstraction review must not carry findings")
        if self.status == "deferred" and not self.follow_up_issue_url:
            raise ValueError("deferred abstraction review requires follow_up_issue_url")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "findings": [item.to_dict() for item in self.findings],
        }
        if self.rationale:
            payload["rationale"] = self.rationale
        if self.follow_up_issue_url:
            payload["follow_up_issue_url"] = self.follow_up_issue_url
        return payload


@dataclass(frozen=True)
class ReviewDecision:
    """Machine-readable reviewer decision.

    This records reviewer intent, not every orchestrator policy action. In
    particular, ``nit_policy="address"`` can make an approved decision with
    nits continue as a synthetic ``changes_requested`` exchange response so
    the coder handles nits before PR creation. The persisted decision keeps
    ``verdict="approved"`` in that case.
    """

    verdict: ReviewVerdict
    risk: str
    blocking_findings: tuple[ReviewItem, ...] = ()
    nits: tuple[ReviewItem, ...] = ()
    tests_reviewed: tuple[str, ...] = ()
    abstraction_review: AbstractionReview = field(default_factory=AbstractionReview)
    nit_policy: NitPolicy = "surface"
    response_text: str = ""
    report_path: str | None = None
    report_sha256: str | None = None
    schema_version: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_agent_payload(
        cls,
        payload: dict[str, Any] | None,
        *,
        response_type: str,
        response_text: str,
        nit_policy: NitPolicy,
    ) -> "ReviewDecision":
        raw_decision = payload.get("decision") if isinstance(payload, dict) else None
        has_structured_decision = isinstance(raw_decision, dict)
        data = raw_decision
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}

        verdict = _coerce_verdict(data.get("verdict"), response_type)
        blocking = _items(data.get("blocking_findings"), fallback_prefix="F")
        nits = _items(data.get("nits"), fallback_prefix="N")
        if verdict == "changes_requested" and not blocking:
            blocking = (
                ReviewItem(
                    id="F1",
                    title=response_text or "Reviewer requested changes",
                ),
            )
        risk = _coerce_risk(data.get("risk"), verdict)
        tests = _string_tuple(data.get("tests_reviewed"))
        abstraction_review = AbstractionReview.from_mapping(
            data.get("abstraction_review"),
            required=has_structured_decision or "abstraction_review" in data,
        )
        raw_policy = data.get("nit_policy") or data.get("nit_policy_applied")
        resolved_policy = _coerce_nit_policy(raw_policy, default=nit_policy)
        decision = cls(
            verdict=verdict,
            risk=risk,
            blocking_findings=blocking,
            nits=nits,
            tests_reviewed=tests,
            abstraction_review=abstraction_review,
            nit_policy=resolved_policy,
            response_text=response_text,
            extra={
                key: value
                for key, value in data.items()
                if key
                not in {
                    "schema_version",
                    "verdict",
                    "risk",
                    "blocking_findings",
                    "nits",
                    "tests_reviewed",
                    "abstraction_review",
                    "nit_policy",
                    "nit_policy_applied",
                    "report_path",
                    "report_sha256",
                }
            },
        )
        decision.validate()
        return decision

    def with_report(self, *, report_path: Path, report_sha256: str) -> "ReviewDecision":
        return ReviewDecision(
            verdict=self.verdict,
            risk=self.risk,
            blocking_findings=self.blocking_findings,
            nits=self.nits,
            tests_reviewed=self.tests_reviewed,
            abstraction_review=self.abstraction_review,
            nit_policy=self.nit_policy,
            response_text=self.response_text,
            report_path=str(report_path),
            report_sha256=report_sha256,
            schema_version=self.schema_version,
            extra=dict(self.extra),
        )

    def validate(self) -> None:
        if self.verdict == "approved" and self.blocking_findings:
            raise ValueError("approved review decision must not carry blocking_findings")
        if self.verdict == "changes_requested" and not self.blocking_findings:
            raise ValueError("changes_requested review decision requires blocking_findings")
        if self.verdict == "approved" and self.abstraction_review.status == "changes_requested":
            raise ValueError(
                "approved review decision must not carry abstraction changes_requested"
            )
        if self.nit_policy not in _VALID_NIT_POLICIES:
            raise ValueError(f"invalid nit_policy: {self.nit_policy}")
        self.abstraction_review.validate()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "risk": self.risk,
            "blocking_findings": [item.to_dict() for item in self.blocking_findings],
            "nits": [item.to_dict() for item in self.nits],
            "tests_reviewed": list(self.tests_reviewed),
            "abstraction_review": self.abstraction_review.to_dict(),
            "nit_policy": self.nit_policy,
            "response_text": self.response_text,
            "report_path": self.report_path,
            "report_sha256": self.report_sha256,
        }
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class ReviewArtifactPair:
    """Persisted review report + decision JSON."""

    report_path: Path
    decision_path: Path
    decision: ReviewDecision

    def to_event_artifacts(self) -> list[dict[str, str]]:
        return [
            {
                "type": REVIEW_REPORT_ARTIFACT,
                "label": "Review report",
                "value": str(self.report_path),
                "render_mode": "markdown",
            },
            {
                "type": REVIEW_DECISION_ARTIFACT,
                "label": "Decision JSON",
                "value": str(self.decision_path),
                "render_mode": "json",
            },
        ]


def persist_review_artifact_pair(
    *,
    report_path: Path,
    decision_path: Path,
    decision: ReviewDecision,
    authored_report_path: Path | None,
) -> ReviewArtifactPair:
    """Write the paired review artifacts and validate ID linkage."""
    report_text = _read_authored_report(authored_report_path)
    if report_text is None:
        report_text = render_review_report(decision)
    _validate_report_links(decision, report_text)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text.rstrip() + "\n", encoding="utf-8")
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    final_decision = decision.with_report(
        report_path=report_path,
        report_sha256=digest,
    )
    final_decision.validate()
    decision_path.write_text(
        json.dumps(final_decision.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return ReviewArtifactPair(
        report_path=report_path,
        decision_path=decision_path,
        decision=final_decision,
    )


def render_review_report(decision: ReviewDecision) -> str:
    """Render a readable fallback report when the reviewer omitted markdown."""
    lines = [
        "# Review Report",
        "",
        f"**Verdict:** {decision.verdict}",
        f"**Risk:** {decision.risk}",
        "",
        "## Summary",
        "",
        decision.response_text or "No reviewer summary was provided.",
        "",
        "## Findings",
        "",
    ]
    if decision.blocking_findings:
        for item in decision.blocking_findings:
            lines.extend(_render_item(item))
    else:
        lines.append("No blocking findings.")
    lines.extend(["", "## Nits", ""])
    if decision.nits:
        for item in decision.nits:
            lines.extend(_render_item(item))
    else:
        lines.append("No nits.")
    lines.extend(["", "## Tests Reviewed", ""])
    if decision.tests_reviewed:
        lines.extend(f"- {test}" for test in decision.tests_reviewed)
    else:
        lines.append("Not specified.")
    lines.extend(_render_abstraction_review(decision.abstraction_review))
    return "\n".join(lines)


def review_requires_nit_rework(decision: ReviewDecision) -> bool:
    """Return True when approval should continue the normal rework loop for nits."""
    return (
        decision.verdict == "approved"
        and decision.nit_policy == "address"
        and bool(decision.nits)
    )


def review_artifacts_from_summary(summary: Any) -> list[dict[str, str]]:
    """Return validated review artifact refs from an exchange summary."""
    if not isinstance(summary, dict):
        return []
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    result: list[dict[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_type = artifact.get("type")
        label = artifact.get("label")
        value = artifact.get("value")
        if not (
            isinstance(artifact_type, str)
            and artifact_type
            and isinstance(label, str)
            and label
            and isinstance(value, str)
            and value
        ):
            continue
        normalized = {
            key: raw_value
            for key, raw_value in artifact.items()
            if isinstance(key, str) and isinstance(raw_value, str)
        }
        result.append(normalized)
    return result


def review_artifacts_from_exchange_result(exchange_result: Any) -> list[dict[str, str]]:
    """Return validated review artifact refs from a ReviewExchangeOutcome-like object."""
    return review_artifacts_from_summary(getattr(exchange_result, "summary", None))


def _validate_report_links(decision: ReviewDecision, report_text: str) -> None:
    missing = [
        item.id
        for item in (
            *decision.blocking_findings,
            *decision.nits,
            *decision.abstraction_review.findings,
        )
        if item.id and item.id not in report_text
    ]
    if missing:
        raise ValueError(
            "review-report.md must mention every review-decision item id: "
            + ", ".join(missing)
        )


def _read_authored_report(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def _render_item(item: ReviewItem) -> list[str]:
    lines = [f"### {item.id}. {item.title}", ""]
    if item.location:
        lines.append(f"- Location: `{item.location}`")
    if item.rationale:
        lines.append(f"- Rationale: {item.rationale}")
    if item.suggested_change:
        lines.append(f"- Suggested change: {item.suggested_change}")
    if len(lines) == 2:
        lines.append("No additional detail.")
    lines.append("")
    return lines


def _render_abstraction_review(review: AbstractionReview) -> list[str]:
    lines = ["", "## Abstraction Review", "", f"Status: {review.status}"]
    if review.follow_up_issue_url:
        lines.append(f"Follow-up issue: {review.follow_up_issue_url}")
    if review.rationale:
        lines.extend(["", review.rationale])
    if review.findings:
        lines.append("")
        for item in review.findings:
            lines.extend(_render_item(item))
    elif review.status == "no_issues":
        lines.append("No abstraction issues found.")
    elif review.status == "deferred":
        lines.append("Deferred to follow-up issue.")
    return lines


def _coerce_verdict(value: Any, response_type: str) -> ReviewVerdict:
    if value == "approved":
        return "approved"
    if value == "changes_requested":
        return "changes_requested"
    if value == "disagree":
        return "disagree"
    if response_type == "ok":
        return "approved"
    if response_type == "changes_requested":
        return "changes_requested"
    return "disagree"


def _coerce_risk(value: Any, verdict: ReviewVerdict) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "low" if verdict == "approved" else "medium"


def _coerce_nit_policy(value: Any, *, default: NitPolicy) -> NitPolicy:
    if isinstance(value, str) and value in _VALID_NIT_POLICIES:
        return value  # type: ignore[return-value]
    return default


def _coerce_abstraction_review_status(value: Any) -> AbstractionReviewStatus:
    if isinstance(value, str) and value in _VALID_ABSTRACTION_REVIEW_STATUSES:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid abstraction_review.status: {value}")


def _items(value: Any, *, fallback_prefix: str) -> tuple[ReviewItem, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        value = [value]
    return tuple(
        ReviewItem.from_mapping(item, fallback_prefix=fallback_prefix, index=index)
        for index, item in enumerate(value, start=1)
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
