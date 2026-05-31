"""Tests for LabelManager - central label registry and query service."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from issue_orchestrator.control.label_manager import LabelManager


# ---------------------------------------------------------------------------
# Minimal config stub — avoids importing the full Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class _ProviderCircuitBreakerConfig:
    label: str = "blocked:provider-unavailable"


@dataclass
class _ProviderResilienceConfig:
    circuit_breaker: _ProviderCircuitBreakerConfig = field(
        default_factory=_ProviderCircuitBreakerConfig,
    )


@dataclass
class _StubConfig:
    label_in_progress: str = "in-progress"
    label_blocked: str = "blocked"
    label_needs_human: str = "needs-human"
    label_needs_rework: str = "needs-rework"
    label_validation_failed: str = "validation-failed"
    label_prefix: str | None = None
    provider_resilience: _ProviderResilienceConfig = field(
        default_factory=_ProviderResilienceConfig,
    )
    review_keep_current_approach_label: str = "reviewer-keep-current-approach"
    code_review_label: str | None = None
    code_reviewed_label: str | None = None
    retrospective_review_trigger_label: str = "retrospective-review"
    retrospective_reviewed_label: str = "retrospective-reviewed"
    retrospective_changes_requested_label: str = "retrospective-changes-requested"


@pytest.fixture
def cfg() -> _StubConfig:
    return _StubConfig()


@pytest.fixture
def prefixed_cfg() -> _StubConfig:
    return _StubConfig(label_prefix="bot")


@pytest.fixture
def lm(cfg: _StubConfig) -> LabelManager:
    return LabelManager(cfg)  # type: ignore[arg-type]


@pytest.fixture
def plm(prefixed_cfg: _StubConfig) -> LabelManager:
    return LabelManager(prefixed_cfg)  # type: ignore[arg-type]


# ===================================================================
# Named properties — no prefix
# ===================================================================

class TestNamedProperties:
    def test_in_progress(self, lm: LabelManager) -> None:
        assert lm.in_progress == "in-progress"

    def test_pr_pending(self, lm: LabelManager) -> None:
        assert lm.pr_pending == "pr-pending"

    def test_reset_retry_scratch_pending(self, lm: LabelManager) -> None:
        assert lm.reset_retry_scratch_pending == "reset-retry-scratch-pending"

    def test_retrospective_review(self, lm: LabelManager) -> None:
        assert lm.retrospective_review == "retrospective-review"
        assert lm.retrospective_reviewed == "retrospective-reviewed"
        assert lm.retrospective_changes_requested == "retrospective-changes-requested"

    def test_blocked(self, lm: LabelManager) -> None:
        assert lm.blocked == "blocked"

    def test_blocked_failed(self, lm: LabelManager) -> None:
        assert lm.blocked_failed == "blocked-failed"

    def test_publish_failed(self, lm: LabelManager) -> None:
        assert lm.publish_failed == "publish-failed"

    def test_needs_human(self, lm: LabelManager) -> None:
        assert lm.needs_human == "needs-human"

    def test_blocked_cross_milestone(self, lm: LabelManager) -> None:
        assert lm.blocked_cross_milestone == "blocked-cross-milestone"

    def test_needs_rework(self, lm: LabelManager) -> None:
        assert lm.needs_rework == "needs-rework"

    def test_validation_failed(self, lm: LabelManager) -> None:
        assert lm.validation_failed == "validation-failed"

    def test_provider_unavailable(self, lm: LabelManager) -> None:
        assert lm.provider_unavailable == "blocked:provider-unavailable"

    def test_io_claimed(self, lm: LabelManager) -> None:
        assert lm.io_claimed == "io:claimed"

    def test_blocked_claim_lost(self, lm: LabelManager) -> None:
        assert lm.blocked_claim_lost == "blocked:claim-lost"

    def test_blocked_stale_claim(self, lm: LabelManager) -> None:
        assert lm.blocked_stale_claim == "blocked:stale-claim"

    def test_blocked_pr_closed(self, lm: LabelManager) -> None:
        assert lm.blocked_pr_closed == "blocked:pr-closed"

    def test_needs_reconcile(self, lm: LabelManager) -> None:
        assert lm.needs_reconcile == "needs-reconcile"

    def test_review_keep_approach(self, lm: LabelManager) -> None:
        assert lm.review_keep_approach == "reviewer-keep-current-approach"

    def test_run_audit_labels(self, lm: LabelManager) -> None:
        assert lm.run_audit_requested == "needs-run-audit"
        assert lm.run_audit_completed == "run-audit-complete"


# ===================================================================
# Named properties — with prefix
# ===================================================================

class TestNamedPropertiesPrefixed:
    def test_in_progress(self, plm: LabelManager) -> None:
        assert plm.in_progress == "bot:in-progress"

    def test_blocked_failed(self, plm: LabelManager) -> None:
        assert plm.blocked_failed == "bot:blocked-failed"

    def test_publish_failed(self, plm: LabelManager) -> None:
        assert plm.publish_failed == "bot:publish-failed"

    def test_reset_retry_scratch_pending(self, plm: LabelManager) -> None:
        assert plm.reset_retry_scratch_pending == "bot:reset-retry-scratch-pending"

    def test_retrospective_review(self, plm: LabelManager) -> None:
        assert plm.retrospective_review == "bot:retrospective-review"
        assert plm.retrospective_reviewed == "bot:retrospective-reviewed"
        assert plm.retrospective_changes_requested == "bot:retrospective-changes-requested"

    def test_io_claimed(self, plm: LabelManager) -> None:
        assert plm.io_claimed == "bot:io:claimed"

    def test_blocked_claim_lost(self, plm: LabelManager) -> None:
        assert plm.blocked_claim_lost == "bot:blocked:claim-lost"

    def test_blocked_pr_closed(self, plm: LabelManager) -> None:
        assert plm.blocked_pr_closed == "bot:blocked:pr-closed"

    def test_provider_unavailable(self, plm: LabelManager) -> None:
        assert plm.provider_unavailable == "bot:blocked:provider-unavailable"

    def test_run_audit_labels(self, plm: LabelManager) -> None:
        assert plm.run_audit_requested == "bot:needs-run-audit"
        assert plm.run_audit_completed == "bot:run-audit-complete"


# ===================================================================
# resolve()
# ===================================================================

class TestResolve:
    def test_no_prefix(self, lm: LabelManager) -> None:
        assert lm.resolve("foo") == "foo"

    def test_with_prefix(self, plm: LabelManager) -> None:
        assert plm.resolve("foo") == "bot:foo"


# ===================================================================
# is_ours()
# ===================================================================

class TestIsOurs:
    def test_known_label(self, lm: LabelManager) -> None:
        assert lm.is_ours("in-progress") is True

    def test_rework_cycle(self, lm: LabelManager) -> None:
        assert lm.is_ours("rework-cycle-3") is True

    def test_unknown(self, lm: LabelManager) -> None:
        assert lm.is_ours("enhancement") is False

    def test_prefixed_known(self, plm: LabelManager) -> None:
        assert plm.is_ours("bot:in-progress") is True

    def test_prefixed_rework_cycle(self, plm: LabelManager) -> None:
        assert plm.is_ours("bot:rework-cycle-7") is True

    def test_prefixed_unknown(self, plm: LabelManager) -> None:
        assert plm.is_ours("enhancement") is False


# ===================================================================
# get_ours()
# ===================================================================

class TestGetOurs:
    def test_filters(self, lm: LabelManager) -> None:
        labels = ["bug", "in-progress", "enhancement", "blocked-failed", "rework-cycle-2"]
        assert lm.get_ours(labels) == ["in-progress", "blocked-failed", "rework-cycle-2"]

    def test_prefixed(self, plm: LabelManager) -> None:
        labels = ["bug", "bot:in-progress", "bot:rework-cycle-1"]
        assert plm.get_ours(labels) == ["bot:in-progress", "bot:rework-cycle-1"]


# ===================================================================
# Blocking queries
# ===================================================================

class TestBlocking:
    def test_blocked_exact(self, lm: LabelManager) -> None:
        assert lm.is_blocking("blocked") is True

    def test_blocked_dash_prefix(self, lm: LabelManager) -> None:
        assert lm.is_blocking("blocked-failed") is True

    def test_blocked_colon_prefix(self, lm: LabelManager) -> None:
        assert lm.is_blocking("blocked:claim-lost") is True
        assert lm.is_blocking("blocked:pr-closed") is True

    def test_non_blocking(self, lm: LabelManager) -> None:
        assert lm.is_blocking("in-progress") is False

    def test_legacy_needs_human(self, lm: LabelManager) -> None:
        assert lm.is_blocking("needs-human") is True

    def test_legacy_failed(self, lm: LabelManager) -> None:
        assert lm.is_blocking("failed") is True

    def test_publish_failed_is_blocking(self, lm: LabelManager) -> None:
        assert lm.is_blocking("publish-failed") is True

    def test_prefixed_blocking(self, plm: LabelManager) -> None:
        """Fixes the latent bug: bot:blocked-failed was not detected as blocking."""
        assert plm.is_blocking("bot:blocked-failed") is True

    def test_prefixed_blocked_colon(self, plm: LabelManager) -> None:
        assert plm.is_blocking("bot:blocked:claim-lost") is True
        assert plm.is_blocking("bot:blocked:pr-closed") is True

    def test_is_blocking_any(self, lm: LabelManager) -> None:
        assert lm.is_blocking_any(["in-progress", "blocked-failed"]) is True
        assert lm.is_blocking_any(["in-progress", "pr-pending"]) is False

    def test_get_blocking(self, lm: LabelManager) -> None:
        labels = ["in-progress", "blocked-failed", "needs-rework", "blocked:claim-lost"]
        assert lm.get_blocking(labels) == ["blocked-failed", "blocked:claim-lost"]


# ===================================================================
# Strip helpers
# ===================================================================

class TestStrip:
    def test_strip_all(self, lm: LabelManager) -> None:
        labels = ["bug", "in-progress", "blocked-failed", "enhancement", "rework-cycle-3"]
        assert lm.strip_all(labels) == ["bug", "enhancement"]

    def test_strip_blocking(self, lm: LabelManager) -> None:
        labels = ["in-progress", "blocked-failed", "pr-pending"]
        assert lm.strip_blocking(labels) == ["in-progress", "pr-pending"]


# ===================================================================
# State queries
# ===================================================================

class TestStateQueries:
    def test_is_in_progress(self, lm: LabelManager) -> None:
        assert lm.is_in_progress(["in-progress", "bug"]) is True
        assert lm.is_in_progress(["bug"]) is False

    def test_is_in_progress_prefixed(self, plm: LabelManager) -> None:
        assert plm.is_in_progress(["bot:in-progress"]) is True
        assert plm.is_in_progress(["in-progress"]) is False

    def test_is_pr_pending(self, lm: LabelManager) -> None:
        assert lm.is_pr_pending(["pr-pending"]) is True
        assert lm.is_pr_pending(["in-progress"]) is False

    def test_requires_human(self, lm: LabelManager) -> None:
        assert lm.requires_human("needs-human") is True
        assert lm.requires_human("blocked-failed") is False
        assert lm.requires_human("in-progress") is False

    def test_requires_human_prefixed(self, plm: LabelManager) -> None:
        assert plm.requires_human("bot:needs-human") is True
        assert plm.requires_human("needs-human") is True  # base name still matches after strip

    def test_requires_human_custom_label(self) -> None:
        """Custom label_needs_human config value is recognized."""
        cfg = _StubConfig(label_needs_human="human-needed")
        lm = LabelManager(cfg)  # type: ignore[arg-type]
        assert lm.requires_human("human-needed") is True
        assert lm.requires_human("needs-human") is False  # default no longer matches
        assert lm.requires_human_any(["human-needed", "bug"]) is True
        assert lm.requires_human_any(["needs-human", "bug"]) is False

    def test_requires_human_custom_label_prefixed(self) -> None:
        """Custom label_needs_human with prefix is recognized."""
        cfg = _StubConfig(label_needs_human="human-needed", label_prefix="bot")
        lm = LabelManager(cfg)  # type: ignore[arg-type]
        assert lm.requires_human("bot:human-needed") is True
        assert lm.requires_human("human-needed") is True  # base name matches after strip
        assert lm.requires_human("needs-human") is False  # old default doesn't match

    def test_requires_human_any(self, lm: LabelManager) -> None:
        assert lm.requires_human_any(["needs-human", "bug"]) is True
        assert lm.requires_human_any(["blocked-failed", "bug"]) is False


# ===================================================================
# Rework cycle helpers
# ===================================================================

class TestReworkCycle:
    def test_rework_cycle_label(self, lm: LabelManager) -> None:
        assert lm.rework_cycle(3) == "rework-cycle-3"

    def test_rework_cycle_prefixed(self, plm: LabelManager) -> None:
        assert plm.rework_cycle(5) == "bot:rework-cycle-5"

    def test_extract_rework_cycle(self, lm: LabelManager) -> None:
        assert lm.extract_rework_cycle(["rework-cycle-2", "rework-cycle-5"]) == 5

    def test_extract_rework_cycle_none(self, lm: LabelManager) -> None:
        assert lm.extract_rework_cycle(["in-progress"]) is None

    def test_extract_rework_cycle_prefixed(self, plm: LabelManager) -> None:
        assert plm.extract_rework_cycle(["bot:rework-cycle-3"]) == 3


# ===================================================================
# Publish-fail-count helpers
# ===================================================================

class TestPublishFailCount:
    def test_publish_fail_count_label(self, lm: LabelManager) -> None:
        assert lm.publish_fail_count_label(2) == "publish-fail-count-2"

    def test_publish_fail_count_label_prefixed(self, plm: LabelManager) -> None:
        assert plm.publish_fail_count_label(3) == "bot:publish-fail-count-3"

    def test_extract_publish_fail_count(self, lm: LabelManager) -> None:
        assert lm.extract_publish_fail_count(["publish-fail-count-2", "in-progress"]) == 2

    def test_extract_publish_fail_count_highest(self, lm: LabelManager) -> None:
        assert lm.extract_publish_fail_count(["publish-fail-count-1", "publish-fail-count-3"]) == 3

    def test_extract_publish_fail_count_none(self, lm: LabelManager) -> None:
        assert lm.extract_publish_fail_count(["in-progress"]) == 0

    def test_extract_publish_fail_count_prefixed(self, plm: LabelManager) -> None:
        assert plm.extract_publish_fail_count(["bot:publish-fail-count-2"]) == 2

    def test_is_ours_publish_fail_count(self, lm: LabelManager) -> None:
        assert lm.is_ours("publish-fail-count-1") is True

    def test_is_ours_publish_fail_count_prefixed(self, plm: LabelManager) -> None:
        assert plm.is_ours("bot:publish-fail-count-5") is True


# ===================================================================
# describe()
# ===================================================================

class TestDescribe:
    def test_known_label(self, lm: LabelManager) -> None:
        assert lm.describe("blocked-failed") == "Failed run"

    def test_rework_cycle(self, lm: LabelManager) -> None:
        assert lm.describe("rework-cycle-4") == "Rework cycle 4"

    def test_unknown_label(self, lm: LabelManager) -> None:
        assert lm.describe("custom-label") == "custom label"

    def test_prefixed_label(self, plm: LabelManager) -> None:
        assert plm.describe("bot:blocked-failed") == "Failed run"


# ===================================================================
# pick_blocking()
# ===================================================================

class TestPickBlocking:
    def test_default(self, lm: LabelManager) -> None:
        assert lm.pick_blocking() == "blocked"

    def test_failed(self, lm: LabelManager) -> None:
        assert lm.pick_blocking(failed=True) == "blocked-failed"

    def test_needs_human(self, lm: LabelManager) -> None:
        assert lm.pick_blocking(needs_human=True) == "needs-human"

    def test_needs_human_takes_precedence(self, lm: LabelManager) -> None:
        assert lm.pick_blocking(failed=True, needs_human=True) == "needs-human"

    def test_prefixed(self, plm: LabelManager) -> None:
        assert plm.pick_blocking(failed=True) == "bot:blocked-failed"


# ===================================================================
# to_label_config_dict()
# ===================================================================

class TestToLabelConfigDict:
    def test_keys(self, lm: LabelManager) -> None:
        d = lm.to_label_config_dict()
        assert set(d.keys()) == {
            "blocked", "needs_human", "code_reviewed",
            "needs_rework", "code_review", "in_progress",
        }

    def test_values_no_prefix(self, lm: LabelManager) -> None:
        d = lm.to_label_config_dict()
        assert d["blocked"] == "blocked"
        assert d["in_progress"] == "in-progress"

    def test_values_prefixed(self, plm: LabelManager) -> None:
        d = plm.to_label_config_dict()
        assert d["blocked"] == "bot:blocked"
        assert d["in_progress"] == "bot:in-progress"
        assert d["code_reviewed"] == "bot:code-reviewed"

    def test_custom_review_labels(self) -> None:
        """Custom code_review_label and code_reviewed_label are used."""
        cfg = _StubConfig(
            code_review_label="review-me",
            code_reviewed_label="reviewed-ok",
        )
        lm = LabelManager(cfg)  # type: ignore[arg-type]
        d = lm.to_label_config_dict()
        assert d["code_review"] == "review-me"
        assert d["code_reviewed"] == "reviewed-ok"

    def test_custom_review_labels_prefixed(self) -> None:
        """Custom review labels with prefix are resolved correctly."""
        cfg = _StubConfig(
            code_review_label="review-me",
            code_reviewed_label="reviewed-ok",
            label_prefix="bot",
        )
        lm = LabelManager(cfg)  # type: ignore[arg-type]
        d = lm.to_label_config_dict()
        assert d["code_review"] == "bot:review-me"
        assert d["code_reviewed"] == "bot:reviewed-ok"
