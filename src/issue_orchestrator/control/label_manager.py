"""Central label registry and query service.

LabelManager is the single source of truth for every label the orchestrator
creates. It is prefix-aware, so ``is_blocking("bot:blocked-failed")`` works
correctly when ``label_prefix="bot"``.

Construction: ``LabelManager(config)`` in bootstrap.  Thread-safe (immutable).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ..infra.config import Config


class LabelCategory(enum.Enum):
    BLOCKING = "blocking"
    LIFECYCLE = "lifecycle"
    INFORMATIONAL = "informational"
    CLAIM = "claim"


@dataclass(frozen=True)
class LabelEntry:
    """One registered orchestrator label."""

    key: str  # Internal name: "blocked_failed"
    base_name: str  # Unprefixed label: "blocked-failed"
    category: LabelCategory
    description: str  # Human-readable: "failed run"
    pattern: bool = False  # True for rework-cycle-{N}


# Legacy labels that predated the blocked-* convention
_LEGACY_BLOCKING = frozenset({"needs-human", "failed", "publish-failed"})

_REWORK_CYCLE_RE = re.compile(r"^rework-cycle-(\d+)$")
_PUBLISH_FAIL_COUNT_RE = re.compile(r"^publish-fail-count-(\d+)$")


class LabelManager:
    """Central, prefix-aware label service.

    Every label the orchestrator owns is in the registry.  All query methods
    work with resolved (prefixed) label strings so callers never need to
    know whether a prefix is configured.
    """

    def __init__(self, config: "Config") -> None:
        self._prefix: str | None = config.label_prefix

        # Build base_name → resolved mapping from config fields
        self._provider_unavailable_base: str = (
            config.provider_resilience.circuit_breaker.label
        )

        # Triage-reviewed is managed WITHOUT the orchestrator prefix throughout
        # the triage subsystem (triage_manifest_builder, completion_action_planner,
        # cleanup_manager, fact_gatherer all use the raw configured value), so we
        # keep the raw form here for callers that must match the label actually
        # written to PRs. See the `triage_reviewed` property.
        self._triage_reviewed_base: str = (
            config.triage_reviewed_label or "triage-reviewed"
        )

        # Registry keyed by internal key
        self._entries: dict[str, LabelEntry] = {}
        self._build_registry(config)

        # Pre-compute resolved names for O(1) named-property access
        self._resolved: dict[str, str] = {
            e.key: self._resolve_base(e.base_name)
            for e in self._entries.values()
            if not e.pattern
        }

        # Set of all resolved non-pattern labels for fast membership test
        self._resolved_set: frozenset[str] = frozenset(self._resolved.values())

    # ------------------------------------------------------------------
    # Registry construction
    # ------------------------------------------------------------------

    def _build_registry(self, config: "Config") -> None:
        entries = [
            LabelEntry("in_progress", config.label_in_progress, LabelCategory.LIFECYCLE, "In progress"),
            LabelEntry("pr_pending", "pr-pending", LabelCategory.LIFECYCLE, "PR pending merge"),
            LabelEntry("reset_retry_pending", "reset-retry-pending", LabelCategory.LIFECYCLE, "Reset + retry pending launch"),
            LabelEntry(
                "reset_retry_scratch_pending",
                "reset-retry-scratch-pending",
                LabelCategory.LIFECYCLE,
                "Reset + retry from scratch pending launch",
            ),
            LabelEntry(
                "retrospective_review",
                config.retrospective_review_trigger_label,
                LabelCategory.LIFECYCLE,
                "Retrospective review requested",
            ),
            LabelEntry(
                "retrospective_reviewed",
                config.retrospective_reviewed_label,
                LabelCategory.INFORMATIONAL,
                "Retrospective review approved",
            ),
            LabelEntry(
                "retrospective_changes_requested",
                config.retrospective_changes_requested_label,
                LabelCategory.LIFECYCLE,
                "Retrospective review changes requested",
            ),
            LabelEntry("blocked", config.label_blocked, LabelCategory.BLOCKING, "Blocked"),
            LabelEntry("blocked_failed", "blocked-failed", LabelCategory.BLOCKING, "Failed run"),
            LabelEntry("publish_failed", "publish-failed", LabelCategory.BLOCKING, "Publishing failed"),
            LabelEntry("blocked_needs_human", config.label_needs_human, LabelCategory.BLOCKING, "Needs human"),
            LabelEntry("blocked_cross_milestone", "blocked-cross-milestone", LabelCategory.BLOCKING, "Cross-milestone dep"),
            LabelEntry("needs_rework", config.label_needs_rework, LabelCategory.LIFECYCLE, "Needs rework"),
            LabelEntry("validation_failed", config.label_validation_failed, LabelCategory.LIFECYCLE, "Validation failed"),
            LabelEntry("rework_cycle", "rework-cycle-{N}", LabelCategory.INFORMATIONAL, "Rework cycle N", pattern=True),
            LabelEntry("publish_fail_count", "publish-fail-count-{N}", LabelCategory.INFORMATIONAL, "Publish failure count", pattern=True),
            LabelEntry("io_claimed", "io:claimed", LabelCategory.CLAIM, "Claimed by orchestrator"),
            LabelEntry("blocked_claim_lost", "blocked:claim-lost", LabelCategory.BLOCKING, "Claim lost"),
            LabelEntry("blocked_stale_claim", "blocked:stale-claim", LabelCategory.BLOCKING, "Stale claim"),
            LabelEntry("blocked_pr_closed", "blocked:pr-closed", LabelCategory.BLOCKING, "PR closed or missing"),
            LabelEntry("needs_reconcile", "needs-reconcile", LabelCategory.CLAIM, "Needs reconciliation"),
            LabelEntry("provider_unavailable", self._provider_unavailable_base, LabelCategory.BLOCKING, "Provider unavailable"),
            LabelEntry("run_audit_requested", "needs-run-audit", LabelCategory.INFORMATIONAL, "Run audit requested"),
            LabelEntry("run_audit_completed", "run-audit-complete", LabelCategory.INFORMATIONAL, "Run audit completed"),
            LabelEntry("review_keep_approach", config.review_keep_current_approach_label, LabelCategory.INFORMATIONAL, "Keep current approach"),
            LabelEntry("code_review", config.code_review_label or "needs-code-review", LabelCategory.LIFECYCLE, "Needs code review"),
            LabelEntry("code_reviewed", config.code_reviewed_label or "code-reviewed", LabelCategory.LIFECYCLE, "Code reviewed"),
        ]
        for e in entries:
            self._entries[e.key] = e

    # ------------------------------------------------------------------
    # Prefix helpers
    # ------------------------------------------------------------------

    def _resolve_base(self, base_name: str) -> str:
        if self._prefix:
            return f"{self._prefix}:{base_name}"
        return base_name

    def _strip_prefix(self, label: str) -> str:
        """Strip the configured prefix from *label*, returning the base name."""
        if self._prefix and label.startswith(f"{self._prefix}:"):
            return label[len(self._prefix) + 1:]
        return label

    # ------------------------------------------------------------------
    # Named label properties (resolved strings)
    # ------------------------------------------------------------------

    @property
    def in_progress(self) -> str:
        return self._resolved["in_progress"]

    @property
    def pr_pending(self) -> str:
        return self._resolved["pr_pending"]

    @property
    def blocked(self) -> str:
        return self._resolved["blocked"]

    @property
    def reset_retry_pending(self) -> str:
        return self._resolved["reset_retry_pending"]

    @property
    def reset_retry_scratch_pending(self) -> str:
        return self._resolved["reset_retry_scratch_pending"]

    @property
    def retrospective_review(self) -> str:
        return self._resolved["retrospective_review"]

    @property
    def retrospective_reviewed(self) -> str:
        return self._resolved["retrospective_reviewed"]

    @property
    def retrospective_changes_requested(self) -> str:
        return self._resolved["retrospective_changes_requested"]

    @property
    def blocked_failed(self) -> str:
        return self._resolved["blocked_failed"]

    @property
    def publish_failed(self) -> str:
        return self._resolved["publish_failed"]

    @property
    def needs_human(self) -> str:
        return self._resolved["blocked_needs_human"]

    @property
    def blocked_cross_milestone(self) -> str:
        return self._resolved["blocked_cross_milestone"]

    @property
    def needs_rework(self) -> str:
        return self._resolved["needs_rework"]

    @property
    def validation_failed(self) -> str:
        return self._resolved["validation_failed"]

    @property
    def provider_unavailable(self) -> str:
        return self._resolved["provider_unavailable"]

    @property
    def io_claimed(self) -> str:
        return self._resolved["io_claimed"]

    @property
    def blocked_claim_lost(self) -> str:
        return self._resolved["blocked_claim_lost"]

    @property
    def blocked_stale_claim(self) -> str:
        return self._resolved["blocked_stale_claim"]

    @property
    def blocked_pr_closed(self) -> str:
        return self._resolved["blocked_pr_closed"]

    @property
    def needs_reconcile(self) -> str:
        return self._resolved["needs_reconcile"]

    @property
    def code_review(self) -> str:
        return self._resolved["code_review"]

    @property
    def code_reviewed(self) -> str:
        return self._resolved["code_reviewed"]

    @property
    def triage_reviewed(self) -> str:
        """The triage-reviewed label as it is actually applied to PRs.

        Unlike most labels, this one is NOT prefixed: the triage subsystem
        writes and reads ``config.triage_reviewed_label`` (default
        ``triage-reviewed``) in raw form. Callers that gate on the label
        present on a PR — e.g. the merge-queue triage gate — must match that
        raw value, so this property deliberately skips prefix resolution.
        """
        return self._triage_reviewed_base

    @property
    def review_keep_approach(self) -> str:
        return self._resolved["review_keep_approach"]

    @property
    def run_audit_requested(self) -> str:
        return self._resolved["run_audit_requested"]

    @property
    def run_audit_completed(self) -> str:
        return self._resolved["run_audit_completed"]

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, base_name: str) -> str:
        """Apply the configured prefix to *base_name*."""
        return self._resolve_base(base_name)

    # ------------------------------------------------------------------
    # Ownership / membership queries
    # ------------------------------------------------------------------

    def is_ours(self, label: str) -> bool:
        """Return True if *label* is any registered orchestrator label (prefix-aware)."""
        if label in self._resolved_set:
            return True
        # Check pattern-based labels
        base = self._strip_prefix(label)
        if _REWORK_CYCLE_RE.match(base):
            return True
        if _PUBLISH_FAIL_COUNT_RE.match(base):
            return True
        return False

    def get_ours(self, labels: Sequence[str]) -> list[str]:
        """Return only orchestrator-owned labels from *labels*."""
        return [l for l in labels if self.is_ours(l)]

    # ------------------------------------------------------------------
    # Blocking queries (prefix-aware)
    # ------------------------------------------------------------------

    def is_blocking(self, label: str) -> bool:
        """Return True if *label* blocks processing (prefix-aware)."""
        base = self._strip_prefix(label)
        if base == "blocked" or base.startswith("blocked-") or base.startswith("blocked:"):
            return True
        if base in _LEGACY_BLOCKING:
            return True
        return False

    def is_blocking_any(self, labels: Sequence[str]) -> bool:
        return any(self.is_blocking(l) for l in labels)

    def get_blocking(self, labels: Sequence[str]) -> list[str]:
        return [l for l in labels if self.is_blocking(l)]

    # ------------------------------------------------------------------
    # Strip helpers
    # ------------------------------------------------------------------

    def strip_all(self, labels: Sequence[str]) -> list[str]:
        """Remove ALL orchestrator labels, returning what remains."""
        return [l for l in labels if not self.is_ours(l)]

    def strip_blocking(self, labels: Sequence[str]) -> list[str]:
        """Remove only blocking labels, returning what remains."""
        return [l for l in labels if not self.is_blocking(l)]

    # ------------------------------------------------------------------
    # Recovery / completion cleanup
    # ------------------------------------------------------------------

    def is_recovered_workflow_label(self, label: str) -> bool:
        """Return True if *label* is a transient workflow label that should be
        shed once an issue's work has landed (PR merged or issue closed).

        Covers ``pr-pending``, every ``publish-fail-count-N`` counter, and every
        blocking label (``blocked``, ``blocked-*``, ``blocked:*``, plus the
        legacy ``needs-human``/``failed``/``publish-failed`` names). These all
        describe an in-flight or failed workflow state that no longer applies
        after recovery.
        """
        return (
            label == self.pr_pending
            or self.is_blocking(label)
            or self.is_publish_fail_count(label)
        )

    def recovered_workflow_labels(self, labels: Sequence[str]) -> list[str]:
        """Return the subset of *labels* to shed when an issue recovers/completes.

        Order-preserving and de-duplicated. This is the single policy owner for
        the clear-on-merge transition: callers feed it the issue's current
        labels and remove whatever it returns.
        """
        result: list[str] = []
        seen: set[str] = set()
        for label in labels:
            if label in seen:
                continue
            if self.is_recovered_workflow_label(label):
                result.append(label)
                seen.add(label)
        return result

    # ------------------------------------------------------------------
    # Specific-state queries
    # ------------------------------------------------------------------

    def is_in_progress(self, labels: Sequence[str]) -> bool:
        return self.in_progress in labels

    def is_pr_pending(self, labels: Sequence[str]) -> bool:
        return self.pr_pending in labels

    def requires_human(self, label: str) -> bool:
        base = self._strip_prefix(label)
        return base == self._entries["blocked_needs_human"].base_name

    def requires_human_any(self, labels: Sequence[str]) -> bool:
        return any(self.requires_human(l) for l in labels)

    # ------------------------------------------------------------------
    # Rework-cycle helpers
    # ------------------------------------------------------------------

    def rework_cycle(self, n: int) -> str:
        """Return the resolved rework-cycle-N label."""
        return self._resolve_base(f"rework-cycle-{n}")

    def extract_rework_cycle(self, labels: Sequence[str]) -> int | None:
        """Parse and return the highest rework cycle number, or None."""
        best: int | None = None
        for label in labels:
            base = self._strip_prefix(label)
            m = _REWORK_CYCLE_RE.match(base)
            if m:
                val = int(m.group(1))
                if best is None or val > best:
                    best = val
        return best

    # ------------------------------------------------------------------
    # Publish-fail-count helpers
    # ------------------------------------------------------------------

    def publish_fail_count_label(self, n: int) -> str:
        """Return the resolved publish-fail-count-N label."""
        return self._resolve_base(f"publish-fail-count-{n}")

    def is_publish_fail_count(self, label: str) -> bool:
        """Return True if *label* is any publish-fail-count-N counter (prefix-aware)."""
        return _PUBLISH_FAIL_COUNT_RE.match(self._strip_prefix(label)) is not None

    def extract_publish_fail_count(self, labels: Sequence[str]) -> int:
        """Parse and return the highest publish-fail-count number, or 0."""
        best = 0
        for label in labels:
            base = self._strip_prefix(label)
            m = _PUBLISH_FAIL_COUNT_RE.match(base)
            if m:
                val = int(m.group(1))
                if val > best:
                    best = val
        return best

    # ------------------------------------------------------------------
    # Description / display
    # ------------------------------------------------------------------

    def describe(self, label: str) -> str:
        """Human-readable description of *label*."""
        base = self._strip_prefix(label)
        # Check pattern-based labels first
        m = _REWORK_CYCLE_RE.match(base)
        if m:
            return f"Rework cycle {m.group(1)}"
        m = _PUBLISH_FAIL_COUNT_RE.match(base)
        if m:
            return f"Publish failure count {m.group(1)}"
        # Look up in registry by base_name
        for entry in self._entries.values():
            if entry.base_name == base:
                return entry.description
        # Fallback: clean up the base name
        return base.replace("-", " ").replace(":", ": ")

    # ------------------------------------------------------------------
    # Label selection
    # ------------------------------------------------------------------

    def pick_blocking(self, *, failed: bool = False, needs_human: bool = False) -> str:
        """Return the appropriate resolved blocking label."""
        if needs_human:
            return self.needs_human
        if failed:
            return self.blocked_failed
        return self.blocked

    # ------------------------------------------------------------------
    # Config dict for CompletionProcessor
    # ------------------------------------------------------------------

    def to_label_config_dict(self) -> dict[str, str]:
        """Return the label name map that CompletionProcessor expects."""
        return {
            "blocked": self.blocked,
            "needs_human": self.needs_human,
            "code_reviewed": self._resolved["code_reviewed"],
            "needs_rework": self.needs_rework,
            "code_review": self._resolved["code_review"],
            "in_progress": self.in_progress,
        }
