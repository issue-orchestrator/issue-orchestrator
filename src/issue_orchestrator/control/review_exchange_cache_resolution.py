"""Typed review-exchange cache resumption owner."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.review_exchange import ReviewExchangeCacheMetadata
from ..domain.review_exchange_resume import (
    ResumeDecision,
    ResumeFacts,
    decide,
)
from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..ports.session_output import ReviewExchangeSummary, SessionOutput

if TYPE_CHECKING:
    from ..domain.review_exchange import ReviewExchangeOutcome

logger = logging.getLogger(__name__)


def _summary_str(summary: Mapping[str, object], key: str) -> str | None:
    value = summary.get(key)
    return value if isinstance(value, str) else None


def _summary_int(summary: Mapping[str, object], key: str) -> int | None:
    value = summary.get(key)
    return value if isinstance(value, int) else None


def _summary_bool(summary: Mapping[str, object], key: str) -> bool | None:
    value = summary.get(key)
    return value if isinstance(value, bool) else None


def _cached_validation_result(
    cached: ReviewExchangeSummary,
    cached_validation_passed: Callable[[Path | None], bool],
) -> bool | None:
    embedded = _summary_bool(cached.summary, "validation_passed")
    if embedded is not None:
        return embedded
    record_path = cached.validation_record_path
    return cached_validation_passed(record_path) if record_path.exists() else None


@dataclass(frozen=True)
class CachedReviewExchange:
    """Typed cached review-exchange payload approved for reuse."""

    outcome: ReviewExchangeOutcome
    run_assets: ReviewExchangeRunAssets
    cache_metadata: ReviewExchangeCacheMetadata


@dataclass(frozen=True)
class ResumeResolution:
    """Named review-exchange cache decision."""

    decision: ResumeDecision

    @classmethod
    def no_cache(cls) -> "ResumeResolution":
        return cls(decision=ResumeDecision.NO_CACHE)


@dataclass(frozen=True)
class ReuseResumeResolution(ResumeResolution):
    """Resume decision variant that carries a complete cached exchange."""

    cached: CachedReviewExchange

    def __post_init__(self) -> None:
        if self.decision not in (
            ResumeDecision.REUSE_APPROVAL,
            ResumeDecision.REUSE_HALT,
        ):
            raise ValueError(f"{self.decision.value} cannot carry cached exchange")


@dataclass(frozen=True)
class CurrentReviewSubject:
    """The exact worktree state a cached summary must cover."""

    head_sha: str | None
    validation_failed: bool


@dataclass(frozen=True)
class ReviewExchangeCacheResolver:
    """Owns review-exchange cache reuse decisions and outcome reconstruction."""

    session_output: SessionOutput
    validation_head_sha: Callable[[Path | None], str | None]
    current_validation_failed: Callable[[Path | None], bool]
    cached_validation_passed: Callable[[Path | None], bool]

    def decide_review_exchange_resumption(
        self,
        worktree: Path,
        session_name: str | None,
        *,
        require_validation: bool,
        current_validation_record_path: Path | None = None,
        current_head_sha: str | None = None,
        not_before_started_at: str | None = None,
    ) -> ResumeResolution:
        """Return the exact cache action for a review-exchange summary."""
        if not session_name:
            return ResumeResolution.no_cache()
        cached = self.session_output.load_review_exchange_summary(
            worktree,
            session_name,
            not_before_started_at=not_before_started_at,
        )
        facts, cache_metadata = self._build_resume_facts(
            cached=cached,
            current_validation_record_path=current_validation_record_path,
            current_head_sha=current_head_sha,
            require_validation=require_validation,
        )
        decision = decide(facts)
        logger.info(
            "[REVIEW_EXCHANGE] resume decision=%s session=%s summary=%s "
            "status=%s reason=%s cached_head_sha=%s current_head_sha=%s "
            "cached_validation_passed=%s current_validation_failed=%s "
            "require_validation=%s boundary=%s",
            decision.value,
            session_name,
            cached.summary_path if cached else "(none)",
            facts.status or "(none)",
            facts.reason or "(none)",
            facts.cached_head_sha or "(none)",
            facts.current_head_sha or "(none)",
            facts.cached_validation_passed,
            facts.current_validation_failed,
            require_validation,
            not_before_started_at or "(none)",
        )
        if decision in (ResumeDecision.REUSE_APPROVAL, ResumeDecision.REUSE_HALT):
            if cached is None or cache_metadata is None:
                raise RuntimeError(
                    "review exchange resume reuse decision requires cached summary metadata"
                )
            outcome = self._cached_outcome_from_summary(cached, cache_metadata)
            if outcome is None:
                logger.warning(
                    "[REVIEW_EXCHANGE] decide() returned %s but summary "
                    "could not be reconstituted; treating as INVALID_SUMMARY: "
                    "session=%s summary=%s",
                    decision.value,
                    session_name,
                    cached.summary_path if cached else "(none)",
                )
                return ResumeResolution(decision=ResumeDecision.INVALID_SUMMARY)
            return ReuseResumeResolution(
                decision=decision,
                cached=CachedReviewExchange(
                    outcome=outcome,
                    run_assets=cached.run_assets,
                    cache_metadata=cache_metadata,
                ),
            )
        return ResumeResolution(decision=decision)

    def _build_resume_facts(
        self,
        *,
        cached: ReviewExchangeSummary | None,
        current_validation_record_path: Path | None,
        current_head_sha: str | None,
        require_validation: bool,
    ) -> tuple[ResumeFacts, ReviewExchangeCacheMetadata | None]:
        current_subject = self._resolve_current_review_subject(
            current_head_sha=current_head_sha,
            current_validation_record_path=current_validation_record_path,
        )
        if cached is None:
            return (
                ResumeFacts(
                    status=None,
                    reason=None,
                    cached_head_sha=None,
                    cached_validation_passed=None,
                    current_head_sha=current_subject.head_sha,
                    current_validation_failed=current_subject.validation_failed,
                    no_completion_count=0,
                    require_validation=require_validation,
                ),
                None,
            )
        cached_status = _summary_str(cached.summary, "status")
        cached_reason = _summary_str(cached.summary, "reason")
        embedded_head_sha = cached.summary.get("head_sha")
        if isinstance(embedded_head_sha, str) and embedded_head_sha:
            cached_head_sha: str | None = embedded_head_sha
        else:
            cached_head_sha = self.validation_head_sha(cached.validation_record_path)
        cached_validation_passed = _cached_validation_result(
            cached,
            self.cached_validation_passed,
        )
        cache_metadata = ReviewExchangeCacheMetadata(
            summary_path=cached.summary_path,
            validation_record_path=cached.validation_record_path,
            head_sha=cached_head_sha or "",
        )
        return (
            ResumeFacts(
                status=cached_status,
                reason=cached_reason,
                cached_head_sha=cached_head_sha,
                cached_validation_passed=cached_validation_passed,
                current_head_sha=current_subject.head_sha,
                current_validation_failed=current_subject.validation_failed,
                no_completion_count=0,
                require_validation=require_validation,
            ),
            cache_metadata,
        )

    def _resolve_current_review_subject(
        self,
        *,
        current_head_sha: str | None,
        current_validation_record_path: Path | None,
    ) -> CurrentReviewSubject:
        explicit_head_sha = self._normalize_head_sha(current_head_sha)
        record_head_sha = self.validation_head_sha(current_validation_record_path)
        record_failed = self.current_validation_failed(current_validation_record_path)
        if explicit_head_sha:
            return CurrentReviewSubject(
                head_sha=explicit_head_sha,
                validation_failed=record_failed if record_head_sha == explicit_head_sha else False,
            )
        return CurrentReviewSubject(
            head_sha=record_head_sha,
            validation_failed=record_failed,
        )

    @staticmethod
    def _normalize_head_sha(head_sha: str | None) -> str | None:
        if not isinstance(head_sha, str):
            return None
        normalized = head_sha.strip()
        return normalized or None

    @staticmethod
    def _cached_outcome_from_summary(
        cached: ReviewExchangeSummary,
        cache_metadata: ReviewExchangeCacheMetadata,
    ) -> ReviewExchangeOutcome | None:
        response_kind = _summary_str(cached.summary, "status")
        rounds = _summary_int(cached.summary, "completed_rounds")
        if response_kind is None or rounds is None:
            return None
        cached_detail = _summary_str(cached.summary, "reason")
        reason = cached_detail or "cached_summary"
        from ..domain.review_exchange import ReviewExchangeOutcome, ReviewExchangeResponse

        return ReviewExchangeOutcome(
            status=response_kind,
            rounds=rounds,
            reason=reason,
            reviewer_response=ReviewExchangeResponse(
                response_type=response_kind,
                response_text=cached.summary.get("response_text") or "",
            ),
            run_assets=cached.run_assets,
            summary=dict(cached.summary),
            cache_metadata=cache_metadata,
        )
