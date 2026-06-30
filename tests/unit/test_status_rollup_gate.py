"""Unit tests for the StatusRollupGate capability/backoff owner."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.status_rollup_gate import (
    StatusRollupGate,
    rollup_is_decisive,
)
from issue_orchestrator.domain.models import StatusRollupCapability
from issue_orchestrator.ports.pull_request_tracker import (
    PRInfo,
    StatusCheckRollupRead,
)


def _pr(mergeable_state: str | None, *, rollup: str | None = None) -> PRInfo:
    return PRInfo(
        number=318,
        title="t",
        url="https://github.com/owner/repo/pull/318",
        branch="b",
        body="",
        state="open",
        labels=["code-reviewed"],
        mergeable_state=mergeable_state,
        status_check_rollup=rollup,  # type: ignore[arg-type]
    )


def _gate(repository_host: MagicMock, *, now: float = 1000.0, backoff: float = 3600.0):
    return StatusRollupGate(
        repository_host,
        repo="owner/repo",
        clock=lambda: now,
        backoff_seconds=backoff,
    )


def _read(
    gate: StatusRollupGate, capability: StatusRollupCapability
) -> StatusCheckRollupRead:
    return gate.read(
        capability,
        pr_number=318,
        issue_number=228,
        issue_key="228",
    )


def test_ok_read_passes_through_and_does_not_arm_backoff() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="PENDING", capability="ok"
    )
    capability = StatusRollupCapability()

    read = _read(_gate(repository_host), capability)

    assert read == StatusCheckRollupRead(state="PENDING", capability="ok")
    assert capability.permission_denied_since is None


def test_permission_denied_arms_backoff_and_is_recorded() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="permission_denied", primary_source_denied=True
    )
    capability = StatusRollupCapability()

    read = _read(_gate(repository_host, now=1000.0), capability)

    assert read.permission_denied is True
    assert capability.permission_denied_since == 1000.0


def test_within_backoff_window_skips_graphql_but_reads_rest_fallback() -> None:
    repository_host = MagicMock()
    # The REST fallback now classifies a failure GraphQL could not read.
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="FAILURE", capability="ok", primary_source_denied=True
    )
    # Already denied 100s ago; backoff is 3600s.
    capability = StatusRollupCapability(permission_denied_since=900.0)

    read = _read(_gate(repository_host, now=1000.0), capability)

    # The wasted GraphQL probe is skipped, but the REST fallback IS still read,
    # so a now-readable failure is surfaced rather than masked by the backoff.
    repository_host.read_pr_status_check_rollup.assert_called_once_with(
        318, skip_primary_source=True
    )
    assert read.state == "FAILURE"
    assert read.capability == "ok"
    # The backoff window is left intact: a fallback-only read cannot prove the
    # GraphQL source recovered.
    assert capability.permission_denied_since == 900.0


def test_ok_via_fallback_with_primary_denied_still_arms_backoff() -> None:
    repository_host = MagicMock()
    # GraphQL was denied but the REST fallback produced a usable answer.
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="FAILURE", capability="ok", primary_source_denied=True
    )
    capability = StatusRollupCapability()

    read = _read(_gate(repository_host, now=1000.0), capability)

    # Even though THIS read succeeded via the fallback, the wasted GraphQL probe
    # is backed off repo-wide so future ticks stop re-probing it.
    assert read.state == "FAILURE"
    assert capability.permission_denied_since == 1000.0


def test_after_backoff_window_re_probes() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="permission_denied", primary_source_denied=True
    )
    capability = StatusRollupCapability(permission_denied_since=1000.0)

    # Exactly at the boundary the window has elapsed (>= backoff).
    _read(_gate(repository_host, now=1000.0 + 3600.0), capability)

    # A full re-probe (GraphQL included), NOT the skip_primary_source path.
    repository_host.read_pr_status_check_rollup.assert_called_once_with(318)


def test_successful_re_probe_clears_a_prior_denial() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="SUCCESS", capability="ok"
    )
    capability = StatusRollupCapability(permission_denied_since=1000.0)

    read = _read(_gate(repository_host, now=1000.0 + 4000.0), capability)

    assert read == StatusCheckRollupRead(state="SUCCESS", capability="ok")
    # Token can read rollups again — the backoff is cleared (self-heal).
    assert capability.permission_denied_since is None


def test_transient_error_neither_suppresses_nor_arms_backoff() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="transient_error"
    )
    capability = StatusRollupCapability()

    read = _read(_gate(repository_host), capability)

    assert read.capability == "transient_error"
    assert capability.permission_denied_since is None
    repository_host.read_pr_status_check_rollup.assert_called_once_with(318)


# ---------------------------------------------------------------------------
# resolve_decisive: eligibility + read + escalation signal in one owner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decisive", ["unstable", "blocked"])
def test_rollup_is_decisive_only_for_unstable_or_blocked(decisive: str) -> None:
    assert rollup_is_decisive(decisive) is True


@pytest.mark.parametrize("nondecisive", ["clean", "dirty", "behind", "", None])
def test_rollup_is_not_decisive_for_other_states(nondecisive: str | None) -> None:
    assert rollup_is_decisive(nondecisive) is False


def test_resolve_decisive_skips_read_for_non_decisive_state() -> None:
    repository_host = MagicMock()
    capability = StatusRollupCapability()

    resolution = _gate(repository_host).resolve_decisive(
        capability, pr=_pr("clean"), issue_number=228, issue_key="228"
    )

    repository_host.read_pr_status_check_rollup.assert_not_called()
    assert resolution.permission_denied is False
    assert resolution.rollup_state is None


def test_resolve_decisive_reads_and_returns_state_for_decisive_state() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state="PENDING", capability="ok"
    )
    capability = StatusRollupCapability()

    resolution = _gate(repository_host).resolve_decisive(
        capability, pr=_pr("unstable"), issue_number=228, issue_key="228"
    )

    assert resolution.rollup_state == "PENDING"
    assert resolution.permission_denied is False


def test_resolve_decisive_permission_denied_carries_actionable_reason() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="permission_denied", primary_source_denied=True
    )
    capability = StatusRollupCapability()

    resolution = _gate(repository_host).resolve_decisive(
        capability, pr=_pr("blocked"), issue_number=228, issue_key="228"
    )

    assert resolution.permission_denied is True
    assert resolution.rollup_state is None
    assert "statusCheckRollup" in resolution.reason
    assert "scope" in resolution.reason
    # Reason names the actual merge state for operator context.
    assert "blocked" in resolution.reason


def test_resolve_decisive_transient_error_is_pending_equivalent() -> None:
    repository_host = MagicMock()
    repository_host.read_pr_status_check_rollup.return_value = StatusCheckRollupRead(
        state=None, capability="transient_error"
    )
    capability = StatusRollupCapability()

    resolution = _gate(repository_host).resolve_decisive(
        capability, pr=_pr("unstable"), issue_number=228, issue_key="228"
    )

    assert resolution.permission_denied is False
    assert resolution.rollup_state is None
